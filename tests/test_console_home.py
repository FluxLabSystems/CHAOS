"""The Home screen's daily check: dead letters, stale points and backups.

docs/operations.md § 0 tells the operator to read the dead-letter count and the
stale-point count on Home every day, and docs/troubleshooting.md sends them to
"the dead-letter list" on the console. Until this change nothing under
src/chaos/web mentioned dead letters. The console cannot be rendered here, so
these tests read the view the way ``test_topology.py`` does: the file is
self-contained, it goes through the one request layer, it calls the endpoints
the runbooks name, and the write it offers is gated on the role the API
enforces.

The paging the view relies on -- ``offset`` on ``GET /telemetry/dead-letters``
-- is exercised against the API below.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from chaos.models.telemetry import IngestDeadLetter

UI = Path("src/chaos/web")


def _home() -> str:
    return (UI / "views/home.js").read_text(encoding="utf-8")


def _api() -> str:
    return (UI / "api.js").read_text(encoding="utf-8")


# -------------------------------------------------------------------- ui --


def test_home_is_self_contained_and_uses_the_one_request_layer():
    text = _home()
    assert not re.search(r"https?://(?!www\.w3\.org)", text)
    assert "fetch(" not in text and "XMLHttpRequest" not in text
    assert "from '../api.js'" in text
    assert "resultProblem" in text


def test_home_reads_the_counts_the_daily_check_names():
    text = _home()
    api = _api()
    # Dead letters: the count from /telemetry/stats and the list itself.
    assert "api.telemetryStats()" in text
    assert "api.deadLetters(" in text
    assert "/telemetry/stats" in api and "/telemetry/dead-letters" in api
    assert "dead_letters" in text, "the count must be the database figure, not a page length"
    # Stale points: the API's own not-trusted list, plus the overview's age-based count.
    assert "api.stalePoints(" in text
    assert "/telemetry/stale" in api
    assert "points || {}).stale" in text
    # Each number says where it came from.
    for source in ("GET /telemetry/stats", "GET /telemetry/stale", "GET /telemetry/dead-letters", "GET /overview"):
        assert source in text, source


def test_the_dead_letter_list_shows_what_the_runbook_asks_for_and_pages():
    text = _home()
    for column in ("'When'", "'Category'", "'Topic / point'", "'Reason'"):
        assert column in text, column
    # Category is derived from the writer's reason code, never invented.
    assert "deadLetterCategory" in text and "/^[A-Za-z0-9_]+/" in text
    # The assets screen's pagination pattern: offset state, previous/next.
    assert "DEAD_LETTER_PAGE" in text
    assert "'← Previous'" in text and "'Next →'" in text
    assert "ops.offset" in text


def test_the_backup_action_is_maintainer_gated_confirmed_and_shows_the_result():
    text = _home()
    api = _api()
    assert "api.backups()" in text and "api.createBackup(" in text
    assert "post('/backup'" in api and "get('/backup')" in api
    # Role first, then an explicit confirmation: the endpoint takes no reason.
    gate = text.index("requireRole('maintainer')")
    confirm = text.index("confirmAction(", gate)
    call = text.index("api.createBackup(", confirm)
    assert gate < confirm < call
    assert "include_history" in api
    for field in ("size_bytes", "sha256", "tables", "rows", "created_at", "path"):
        assert field in text, field
    # A missing or stale archive is drawn as a state, never as a healthy zero.
    assert "'No archive'" in text
    assert "BACKUP_STALE_AFTER_H" in text


def test_home_never_shows_a_zero_it_did_not_get():
    text = _home()
    # The dead-letter readout prints the API's figure or a dash, not 0.
    assert "String(total ?? '—')" in text
    # An unreachable endpoint renders resultProblem for each of the three cards.
    assert text.count("resultProblem(result,") >= 4


# ------------------------------------------------------------------- api --


@pytest.fixture()
def letters(db_session):
    base = dt.datetime(2026, 8, 7, 12, 0, tzinfo=dt.UTC)
    for index in range(7):
        db_session.add(
            IngestDeadLetter(
                topic=f"chaos/site/energy/battery/{index}",
                reason=f"unresolved_topic: message {index} is not bound to a point",
                created_at=base + dt.timedelta(minutes=index),
            )
        )
    db_session.commit()


def test_dead_letters_page_newest_first_with_offset(client, letters):
    first = client.get("/api/v1/telemetry/dead-letters", params={"limit": 3}).json()
    second = client.get("/api/v1/telemetry/dead-letters", params={"limit": 3, "offset": 3}).json()
    third = client.get("/api/v1/telemetry/dead-letters", params={"limit": 3, "offset": 6}).json()
    assert [row["topic"][-1] for row in first] == ["6", "5", "4"]
    assert [row["topic"][-1] for row in second] == ["3", "2", "1"]
    assert [row["topic"][-1] for row in third] == ["0"]
    assert client.get("/api/v1/telemetry/dead-letters", params={"offset": 7}).json() == []
    assert client.get("/api/v1/telemetry/dead-letters", params={"offset": -1}).status_code == 422
    # The count the Home card prints alongside the page.
    assert client.get("/api/v1/telemetry/stats").json()["database"]["dead_letters"] == 7
