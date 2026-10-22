"""The Alarms screen's notification delivery log (SDD FR-007).

docs/alarms.md says the delivery log "is on the console" and
docs/troubleshooting.md sends the operator there when an alarm fired and
nobody was told. The view now reads ``GET /notifications`` and the
``notifications`` carried by ``GET /incidents/{id}``. These tests read the
view statically, and check against the API that the status vocabulary the
view draws is the notifier's own and that no row can be shown as more than it
is: ``sent`` is the only success, and ``not_configured`` names what is missing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_alarm_correlation import AC_MAIN, SERVER, UPS, at, load_real_registry, set_state

from chaos.alarms.correlation import CorrelationEngine
from chaos.alarms.definitions import sync_definitions
from chaos.alarms.evaluator import AlarmEvaluator
from chaos.alarms.notify import STATUS_FAILED, STATUS_NOT_CONFIGURED, STATUS_SENT, Notifier

UI = Path("src/chaos/web")


def _alarms() -> str:
    return (UI / "views/alarms.js").read_text(encoding="utf-8")


def test_alarms_is_self_contained_and_uses_the_one_request_layer():
    text = _alarms()
    assert not re.search(r"https?://(?!www\.w3\.org)", text)
    assert "fetch(" not in text and "XMLHttpRequest" not in text
    assert "from '../api.js'" in text


def test_the_delivery_log_is_read_from_the_notification_endpoints():
    text = _alarms()
    api = (UI / "api.js").read_text(encoding="utf-8")
    assert "api.notifications(" in text
    assert "api.incident(" in text
    assert "get(`/notifications${query(params)}`)" in api
    assert "get(`/incidents/${encodeURIComponent(incidentId)}`)" in api


def test_the_status_vocabulary_is_the_notifiers_and_sent_is_the_only_success():
    text = _alarms()
    block = text[
        text.index("const DELIVERY_STATUS = {") : text.index("};", text.index("const DELIVERY_STATUS = {"))
    ]
    for status in (STATUS_SENT, STATUS_FAILED, STATUS_NOT_CONFIGURED):
        assert f"{status}:" in block, status
    assert re.search(r"sent:\s*\{ status: 'ok'", block)
    assert re.search(r"failed:\s*\{ status: 'alarm'", block)
    assert re.search(r"not_configured:\s*\{ status: 'design_only', label: 'not configured'", block)
    # An unknown status is drawn as unknown, never promoted to ok.
    assert "{ status: 'unknown'" in text


def test_the_panel_shows_channel_stage_status_detail_and_time():
    text = _alarms()
    for column in ("'When'", "'Channel'", "'Stage'", "'Status'", "'Detail'"):
        assert column in text, column
    assert "detail.stage" in text
    assert "detail.channel_detail" in text
    assert "sent_at" in text
    assert "filters" in text and "'log', 'email', 'push', 'voice'" in text


def test_each_incident_can_open_its_own_deliveries():
    text = _alarms()
    assert "incidentDeliveries(group.id)" in text
    # Loaded on demand, once, and only for a real incident.
    assert "group.correlated ? incidentDeliveries" in text
    assert "addEventListener('toggle'" in text
    assert "result.data.notifications" in text


@pytest.fixture()
def raise_alarms(db_session, bus, settings):
    """The container-outage cascade from test_alarm_api, correlated and notified."""

    def _raise() -> None:
        load_real_registry(db_session)
        sync_definitions(db_session, strict=True)
        set_state(db_session, AC_MAIN, "energized_state", False)
        set_state(db_session, UPS, "on_battery", True)
        set_state(db_session, SERVER, "availability_state", "offline")
        evaluator = AlarmEvaluator(db_session, bus, settings)
        evaluator.evaluate(at(0))
        evaluator.evaluate(at(600))
        CorrelationEngine(db_session, settings).correlate(at(600))
        Notifier(db_session, settings).dispatch_pending(at(600))
        db_session.commit()

    return _raise


def test_the_delivery_log_is_drawn_from_the_api_rows_only(client, raise_alarms):
    """What the screen would receive: real rows, real statuses, nothing synthesised."""
    raise_alarms()
    payload = client.get("/api/v1/notifications", params={"limit": 50}).json()
    assert payload["count"] >= 1
    statuses = {row["status"] for row in payload["notifications"]}
    assert statuses <= {STATUS_SENT, STATUS_FAILED, STATUS_NOT_CONFIGURED}
    for row in payload["notifications"]:
        assert "stage" in row["detail"]
        assert row["channel"] in ("log", "email", "push", "voice")
        if row["status"] == STATUS_NOT_CONFIGURED:
            assert row["detail"]["channel_detail"], "a not-configured row names what is missing"
    incident_id = next(row["incident_id"] for row in payload["notifications"] if row["incident_id"])
    detail = client.get(f"/api/v1/incidents/{incident_id}").json()
    assert detail["notifications"], "the per-incident panel reads this list"
    assert {n["channel"] for n in detail["notifications"]} >= {"log"}
