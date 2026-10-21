"""``/metrics``: the platform's own counters in Prometheus text format.

The exposition is hand-rendered, so these tests hold it to the two promises the
module makes: nothing is invented (a subsystem that is not running contributes
no series, and every count comes from the database or a live counter), and the
output is deterministic.
"""

from __future__ import annotations

import datetime as dt

from chaos.api.metrics import METRICS_CONTENT_TYPE, render_metrics
from chaos.ingest.service import IngestService
from chaos.models.alarms import Alarm, AlarmDefinition, Incident, NotificationLog
from chaos.models.telemetry import IngestDeadLetter
from chaos.runtime import ServiceManager

T0 = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.UTC)


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def test_metrics_is_served_in_the_prometheus_text_format(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == METRICS_CONTENT_TYPE
    body = response.text
    assert "# TYPE chaos_build_info gauge" in body
    assert 'chaos_build_info{node_role="primary",site_id="site.site.primary.01",version=' in body
    assert body.endswith("\n")


def test_every_severity_is_reported_even_when_zero(client):
    lines = _lines(client.get("/metrics").text)
    for severity in ("info", "warning", "major", "critical", "emergency"):
        assert f'chaos_alarms_active{{severity="{severity}"}} 0' in lines
    assert "chaos_incidents_open 0" in lines
    assert "chaos_ingest_dead_letters 0" in lines


def test_counts_come_from_the_database(client, db_session):
    db_session.add(AlarmDefinition(alarm_key="pump.overcurrent", name="Pump overcurrent", severity="critical"))
    db_session.flush()
    incident = Incident(title="Pump incident", severity="critical", opened_at=T0, state="open")
    db_session.add(incident)
    db_session.flush()
    for state in ("active", "acknowledged", "cleared"):
        db_session.add(
            Alarm(
                alarm_key="pump.overcurrent",
                asset_id="water.pump.irrigation.01",
                severity="critical",
                state=state,
                detected_at=T0,
                incident_id=incident.id,
            )
        )
    db_session.add(
        Alarm(
            alarm_key="pump.overcurrent",
            asset_id="water.pump.irrigation.02",
            severity="critical",
            state="active",
            detected_at=T0,
            suppressed=True,
            suppression_reason="symptom: of the incident",
        )
    )
    db_session.add_all(
        [
            NotificationLog(channel="log", status="sent", sent_at=T0),
            NotificationLog(channel="email", status="failed", sent_at=T0),
            NotificationLog(channel="email", status="failed", sent_at=T0),
            NotificationLog(channel="push", status="not_configured", sent_at=T0),
        ]
    )
    db_session.add_all(
        [
            IngestDeadLetter(topic="chaos/x/y/z/w", payload="{}", reason="unresolved_topic: no"),
            IngestDeadLetter(topic="chaos/x/y/z/v", payload="{}", reason="malformed_json: no"),
        ]
    )
    db_session.commit()

    lines = _lines(client.get("/metrics").text)
    assert 'chaos_alarms_active{severity="critical"} 3' in lines  # cleared does not count
    assert "chaos_alarms_active_suppressed 1" in lines
    assert "chaos_incidents_open 1" in lines
    assert "chaos_ingest_dead_letters 2" in lines
    assert 'chaos_notifications_total{channel="email",status="failed"} 2' in lines
    assert 'chaos_notifications_total{channel="log",status="sent"} 1' in lines
    assert 'chaos_notifications_total{channel="push",status="not_configured"} 1' in lines


def test_services_absent_from_this_node_contribute_no_series(client):
    """"Ingest received nothing" and "there is no ingest here" are different facts."""
    body = client.get("/metrics").text
    assert "chaos_ingest_running 0" in body
    assert "chaos_ingest_events_total" not in body
    assert "# ingest: not registered on this node" in body
    assert "# alarm engine: not registered on this node" in body


def test_running_service_counters_are_exported(app, client, session_factory, bus, settings):
    ingest = IngestService(session_factory, bus, settings, auto_sweep=False, clock=lambda: T0)
    ingest.start()
    bus.publish("chaos/energy/nowhere/thing_01/value", b"{}")
    manager = ServiceManager()
    manager.register(ingest)
    app.state.services = manager
    try:
        lines = _lines(client.get("/metrics").text)
    finally:
        ingest.stop()
        app.state.services = None
    assert "chaos_ingest_running 1" in lines
    assert 'chaos_ingest_events_total{event="messages_received"} 1' in lines
    assert 'chaos_ingest_events_total{event="dead_lettered"} 1' in lines


def test_plugin_health_is_a_full_state_matrix(client):
    lines = _lines(client.get("/metrics").text)
    assert 'chaos_plugin_health{plugin="netbotz",state="not_configured"} 1' in lines
    assert 'chaos_plugin_health{plugin="netbotz",state="ok"} 0' in lines
    assert 'chaos_plugin_health{plugin="netbotz",state="degraded"} 0' in lines


def test_the_exposition_is_deterministic(app, client):
    first = client.get("/metrics").text
    second = client.get("/metrics").text
    assert first == second
    assert render_metrics(app) == first


def test_label_values_are_escaped():
    from chaos.api.metrics import _Family

    family = _Family("chaos_test", "gauge", 'help with "quotes"\nand a newline')
    family.add(1, label='a "quoted" value\\with backslash')
    rendered = family.render()
    assert rendered[0] == '# HELP chaos_test help with "quotes"\\nand a newline'
    assert rendered[2] == 'chaos_test{label="a \\"quoted\\" value\\\\with backslash"} 1'
