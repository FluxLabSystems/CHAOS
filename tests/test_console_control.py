"""The Control screen's writes: operating modes, budget leases and the reloads.

docs/operations.md and docs/troubleshooting.md both say the operating mode is
set on the Control screen; before this change the screen only displayed it.
These checks read ``views/control.js`` the way ``test_topology.py`` reads the
topology view, and pin the properties that make the writes safe to offer:
every one is gated on the role the API enforces, every one that the API audits
with a reason collects that reason first, and the two that take no reason are
confirmed explicitly rather than fired on a click.
"""

from __future__ import annotations

import re
from pathlib import Path

from chaos.commands.modes import MODES

UI = Path("src/chaos/web")


def _control() -> str:
    return (UI / "views/control.js").read_text(encoding="utf-8")


def _api() -> str:
    return (UI / "api.js").read_text(encoding="utf-8")


def _app() -> str:
    return (UI / "app.js").read_text(encoding="utf-8")


def test_control_is_self_contained_and_uses_the_one_request_layer():
    text = _control()
    assert not re.search(r"https?://(?!www\.w3\.org)", text)
    assert "fetch(" not in text and "XMLHttpRequest" not in text
    assert "from '../api.js'" in text


def test_the_mode_set_is_the_platforms_mode_set():
    """A console that offered a mode the API rejects would teach the wrong vocabulary."""
    text = _control()
    match = re.search(r"const MODES = \[(.*?)\];", text)
    assert match, "MODES is not declared"
    offered = re.findall(r"'([a-z_]+)'", match.group(1))
    assert tuple(offered) == tuple(MODES)


def test_the_mode_setter_is_operator_gated_asks_why_and_posts_the_api_shape():
    text = _control()
    api = _api()
    setter = text[text.index("function modeSetter(") : text.index("function showModeOutcome(")]
    gate = setter.index("requireRole('operator')")
    reason = setter.index("askReason(", gate)
    call = setter.index("api.setOperatingMode(", reason)
    assert gate < reason < call
    assert "post(`/operating-modes/${encodeURIComponent(scopeId)}`" in api
    # ModeSetBody: mode, reason, condition_clear, scope_type, expires_at.
    for field in ("mode", "reason", "condition_clear", "scope_type", "expires_at"):
        assert field in setter, field
    # Emergency's latch is explained before the request, and a 409 is shown
    # with the API's own `required` list rather than swallowed.
    assert "latched" in setter.lower()
    assert "detail.required" in text


def test_modes_are_settable_from_both_the_picker_and_the_asset_panel():
    text = _control()
    assert "api.operatingModes()" in text
    assert "function modeSetter({" in text
    assert text.count("modeSetter({") - text.count("function modeSetter({") == 2
    assert "{ type: 'asset', id: asset.asset_id" in text
    assert "{ type: 'domain', id: asset.domain" in text
    assert "type: 'site'" in text
    # The literal "site" is what the API resolves to the configured site scope.
    assert "scope.type === 'site' ? 'site' : scope.id" in text


def test_lease_requests_are_operator_gated_and_a_denial_is_an_answer_not_an_error():
    text = _control()
    api = _api()
    console = text[text.index("function leaseConsole(") : text.index("function leaseRow(")]
    gate = console.index("requireRole('operator')")
    reason = console.index("askReason(", gate)
    call = console.index("api.requestLease(", reason)
    assert gate < reason < call
    assert "post('/energy/load-budgets/reservations'" in api
    for field in ("asset_id", "requested_kw", "reason", "priority", "revocable", "duration_s"):
        assert field in console, field
    assert "decision.granted" in console and "'Denied'" in console
    assert "available_kw" in console
    # Revocation goes through DELETE with the audit reason in the query string.
    assert "api.revokeLease(" in text
    assert "del(`/energy/load-budgets/reservations/" in api


def test_reloads_are_maintainer_gated_and_confirmed_because_they_carry_no_reason():
    text = _control()
    api = _api()
    block = text[
        text.index("function maintainerCard(") : text.index(
            "/* ------------------------------------------------------ budget leases"
        )
    ]
    for action, call in (
        ("reloadRegistry", "api.reloadRegistry()"),
        ("reloadAlarms", "api.reloadAlarmDefinitions("),
    ):
        body = block[block.index(f"async function {action}(") :]
        gate = body.index("requireRole('maintainer')")
        confirm = body.index("confirmAction(", gate)
        sent = body.index(call, confirm)
        assert gate < confirm < sent, action
    assert "post('/registry/reload')" in api
    assert "post(`/alarms/definitions/reload" in api
    assert "strict" in block
    # The confirm dialog is the shared one, built on demand so index.html stays the shell's.
    assert "export function confirmAction(" in _app()
    assert "confirm-dialog" not in (UI / "index.html").read_text(encoding="utf-8")


def test_the_api_errors_reach_the_screen():
    text = _control()
    assert "showModeOutcome" in text and "showReload" in text
    for status in ("response.forbidden", "response.missing", "HTTP ${response.status}"):
        assert status in text, status
