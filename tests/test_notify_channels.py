"""Email and push delivery, the failed bucket, and bounded retries (SDD FR-007).

No relay and no push server exist in CI, so SMTP is a recording fake handed to
``EmailChannel`` through its factory hook and push goes through
``httpx.MockTransport``. What the tests pin is the contract: ``sent`` only when
the relay or server accepted the message, ``failed`` with the reason when it did
not, ``not_configured`` naming what is missing until the settings exist -- and
a failed row is never mistaken for a delivered one.
"""

from __future__ import annotations

import datetime as dt
import json
import smtplib

import httpx
import pytest

from chaos.alarms.notify import (
    STATUS_FAILED,
    STATUS_NOT_CONFIGURED,
    STATUS_SENT,
    ChannelResult,
    EmailChannel,
    LogChannel,
    NotificationMessage,
    NotificationResult,
    Notifier,
    PushChannel,
    build_channels,
    notification_detail,
    parse_directory,
)
from chaos.config import Settings
from chaos.models.alarms import Alarm, AlarmDefinition, NotificationLog

T0 = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.UTC)


def at(seconds: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds)


def settings_with(**overrides) -> Settings:
    base = {
        "database_url": "sqlite://",
        "mqtt_enabled": False,
        "notification_backends": "log,email,push,voice",
    }
    base.update(overrides)
    return Settings(**base)


def message(role: str = "operator", severity: str = "major", stage: int = 1) -> NotificationMessage:
    return NotificationMessage(
        subject=f"[{severity.upper()}] Pump overcurrent",
        body="Pump 01 tripped on overcurrent.",
        severity=severity,
        stage=stage,
        reason="escalation stage 1 at +0 s",
        alarm_id="alarm-1",
        recipient_role=role,
    )


# ---------------------------------------------------------------------------
# The recipient directory
# ---------------------------------------------------------------------------


def test_directory_grammar_is_role_equals_addresses_separated_by_semicolons():
    parsed = parse_directory(" Operator = a@x.org , b@x.org ; on_call=c@x.org;; maintainer= ")
    assert parsed == {"operator": ("a@x.org", "b@x.org"), "on_call": ("c@x.org",)}


def test_directory_is_empty_when_unset():
    assert parse_directory("") == {}
    assert parse_directory(None) == {}


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


class FakeSmtp:
    """Records what EmailChannel does to an smtplib.SMTP."""

    def __init__(self, host: str, port: int, timeout: float, *, fail: Exception | None = None) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[tuple] = []
        self.messages: list = []
        self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append(("quit",))
        return False

    def ehlo(self):
        self.calls.append(("ehlo",))

    def starttls(self, context=None):
        self.calls.append(("starttls", context is not None))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, mail):
        if self.fail is not None:
            raise self.fail
        self.calls.append(("send_message",))
        self.messages.append(mail)


class SmtpFactory:
    def __init__(self, fail: Exception | None = None, *, refuse: Exception | None = None) -> None:
        self.clients: list[FakeSmtp] = []
        self.fail = fail
        self.refuse = refuse

    def __call__(self, host: str, port: int, timeout: float) -> FakeSmtp:
        if self.refuse is not None:
            raise self.refuse
        client = FakeSmtp(host, port, timeout, fail=self.fail)
        self.clients.append(client)
        return client


EMAIL_SETTINGS = {
    "notify_smtp_host": "relay.lan",
    "notify_smtp_port": 587,
    "notify_smtp_from": "chaos@homestead.lan",
    "notify_smtp_username": "svc-chaos",
    "notify_smtp_password": "secret",
    "notify_email_recipients": "operator=ops@homestead.lan,second@homestead.lan;on_call=duty@homestead.lan",
}


def test_email_is_not_configured_until_every_setting_exists():
    channel = EmailChannel(settings_with())
    assert channel.available is False
    result = channel.send(message())
    assert result.status == STATUS_NOT_CONFIGURED
    for name in ("CHAOS_NOTIFY_SMTP_HOST", "CHAOS_NOTIFY_SMTP_FROM", "CHAOS_NOTIFY_EMAIL_RECIPIENTS"):
        assert name in result.detail
    assert "No message was sent" in result.detail
    assert result.recipient is None


def test_email_delivers_through_the_relay_to_every_address_of_the_role():
    factory = SmtpFactory()
    channel = EmailChannel(settings_with(**EMAIL_SETTINGS), smtp_factory=factory)
    assert channel.available is True

    result = channel.send(message(role="operator"))
    assert result.status == STATUS_SENT
    assert result.recipient == "ops@homestead.lan, second@homestead.lan"
    assert "relay.lan:587" in result.detail

    (client,) = factory.clients
    assert (client.host, client.port, client.timeout) == ("relay.lan", 587, 10.0)
    assert [call[0] for call in client.calls] == ["ehlo", "starttls", "ehlo", "login", "send_message", "quit"]
    assert ("login", "svc-chaos", "secret") in client.calls
    (mail,) = client.messages
    assert mail["To"] == "ops@homestead.lan, second@homestead.lan"
    assert mail["From"] == "chaos@homestead.lan"
    assert mail["Subject"] == "[MAJOR] Pump overcurrent"
    assert mail["X-CHAOS-Severity"] == "major"
    assert mail["X-CHAOS-Stage"] == "1"
    assert mail["X-CHAOS-Alarm"] == "alarm-1"
    assert "Pump 01 tripped on overcurrent." in mail.get_content()


@pytest.mark.parametrize(
    ("security", "expected"),
    [
        ("tls", ["ehlo", "login", "send_message", "quit"]),
        ("none", ["ehlo", "login", "send_message", "quit"]),
        ("starttls", ["ehlo", "starttls", "ehlo", "login", "send_message", "quit"]),
    ],
)
def test_email_security_modes_drive_the_smtp_conversation(security, expected):
    factory = SmtpFactory()
    channel = EmailChannel(
        settings_with(**EMAIL_SETTINGS, notify_smtp_security=security), smtp_factory=factory
    )
    assert channel.send(message()).status == STATUS_SENT
    assert [call[0] for call in factory.clients[0].calls] == expected


def test_email_skips_login_without_credentials():
    factory = SmtpFactory()
    overrides = dict(EMAIL_SETTINGS, notify_smtp_username=None, notify_smtp_password=None)
    channel = EmailChannel(settings_with(**overrides), smtp_factory=factory)
    assert channel.send(message()).status == STATUS_SENT
    assert all(call[0] != "login" for call in factory.clients[0].calls)


def test_email_without_an_address_for_the_role_is_not_configured():
    factory = SmtpFactory()
    channel = EmailChannel(settings_with(**EMAIL_SETTINGS), smtp_factory=factory)
    result = channel.send(message(role="maintainer"))
    assert result.status == STATUS_NOT_CONFIGURED
    assert "role 'maintainer'" in result.detail
    assert factory.clients == []  # nothing was even opened


def test_email_rejection_by_the_relay_is_failed_not_hidden():
    refused = smtplib.SMTPRecipientsRefused({"ops@homestead.lan": (550, b"no such user")})
    channel = EmailChannel(settings_with(**EMAIL_SETTINGS), smtp_factory=SmtpFactory(fail=refused))
    result = channel.send(message())
    assert result.status == STATUS_FAILED
    assert "SMTPRecipientsRefused" in result.detail
    assert "relay.lan:587" in result.detail
    assert result.recipient == "ops@homestead.lan, second@homestead.lan"


def test_email_connection_failure_is_failed():
    factory = SmtpFactory(refuse=ConnectionRefusedError("relay down"))
    channel = EmailChannel(settings_with(**EMAIL_SETTINGS), smtp_factory=factory)
    result = channel.send(message())
    assert result.status == STATUS_FAILED
    assert "ConnectionRefusedError" in result.detail


def test_an_unknown_smtp_security_mode_falls_back_to_starttls(caplog):
    channel = EmailChannel(settings_with(**EMAIL_SETTINGS, notify_smtp_security="carrier-pigeon"))
    assert channel.security == "starttls"


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------

PUSH_SETTINGS = {
    "notify_push_url": "https://ntfy.lan/",
    "notify_push_token": "tok-123",
    "notify_push_targets": "operator=chaos-ops;on_call=chaos-duty,chaos-backup",
}


def push_channel(handler, **overrides) -> PushChannel:
    return PushChannel(
        settings_with(**{**PUSH_SETTINGS, **overrides}), transport=httpx.MockTransport(handler)
    )


def test_push_is_not_configured_without_url_or_targets():
    channel = PushChannel(settings_with())
    assert channel.available is False
    result = channel.send(message())
    assert result.status == STATUS_NOT_CONFIGURED
    assert "CHAOS_NOTIFY_PUSH_URL" in result.detail
    assert "CHAOS_NOTIFY_PUSH_TARGETS" in result.detail
    assert "No message was sent" in result.detail


def test_push_posts_one_ntfy_json_message_per_topic_of_the_role():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "abc"})

    channel = push_channel(handler)
    assert channel.available is True
    result = channel.send(message(role="on_call", severity="critical", stage=2))
    assert result.status == STATUS_SENT
    assert result.recipient == "chaos-duty, chaos-backup"
    assert "HTTP 200" in result.detail

    assert [json.loads(r.content)["topic"] for r in seen] == ["chaos-duty", "chaos-backup"]
    first = seen[0]
    assert first.method == "POST" and str(first.url) == "https://ntfy.lan/"
    assert first.headers["authorization"] == "Bearer tok-123"
    assert first.headers["content-type"] == "application/json"
    assert json.loads(first.content) == {
        "topic": "chaos-duty",
        "title": "[CRITICAL] Pump overcurrent",
        "message": "Pump 01 tripped on overcurrent.",
        "priority": 5,
        "tags": ["chaos", "critical", "stage-2"],
    }


def test_push_without_a_token_sends_no_authorization_header():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    push_channel(handler, notify_push_token=None).send(message())
    assert "authorization" not in seen[0].headers


def test_push_non_2xx_is_failed_with_the_status():
    channel = push_channel(lambda request: httpx.Response(503, text="server busy"))
    result = channel.send(message())
    assert result.status == STATUS_FAILED
    assert "HTTP 503" in result.detail
    assert "server busy" in result.detail
    assert result.recipient == "chaos-ops"


def test_push_transport_error_is_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = push_channel(handler).send(message())
    assert result.status == STATUS_FAILED
    assert "ConnectError" in result.detail


def test_push_partial_failure_is_failed_but_names_what_got_through():
    def handler(request: httpx.Request) -> httpx.Response:
        topic = json.loads(request.content)["topic"]
        return httpx.Response(200 if topic == "chaos-duty" else 500)

    result = push_channel(handler).send(message(role="on_call"))
    assert result.status == STATUS_FAILED
    assert "chaos-backup: HTTP 500" in result.detail
    assert "delivered: chaos-duty: HTTP 200" in result.detail


def test_push_without_a_target_for_the_role_is_not_configured():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    result = push_channel(handler).send(message(role="maintainer"))
    assert result.status == STATUS_NOT_CONFIGURED
    assert "role 'maintainer'" in result.detail
    assert seen == []


def test_push_priority_follows_severity():
    channel = PushChannel(settings_with(**PUSH_SETTINGS))
    priorities = {
        s: channel.payload(message(severity=s), "t")["priority"] for s in ("info", "warning", "major")
    }
    assert priorities == {"info": 2, "warning": 3, "major": 4}


# ---------------------------------------------------------------------------
# build_channels reports availability honestly
# ---------------------------------------------------------------------------


def test_build_channels_availability_follows_the_settings():
    bare = build_channels(settings_with())
    assert {name: channel.available for name, channel in bare.items()} == {
        "log": True,
        "email": False,
        "push": False,
        "voice": False,
    }
    configured = build_channels(settings_with(**EMAIL_SETTINGS, **PUSH_SETTINGS))
    assert configured["email"].available is True
    assert configured["push"].available is True
    assert configured["voice"].available is False  # CUCM is not commissioned hardware


# ---------------------------------------------------------------------------
# Retries through the Notifier
# ---------------------------------------------------------------------------


class ScriptedChannel:
    """An email channel whose outcomes are decided in advance."""

    name = "email"
    available = True

    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = list(outcomes)
        self.attempts = 0

    def send(self, message: NotificationMessage) -> ChannelResult:
        self.attempts += 1
        status = self.outcomes.pop(0) if self.outcomes else STATUS_SENT
        return ChannelResult(status=status, detail=f"scripted {status}", recipient="ops@homestead.lan")


class ExplodingChannel:
    name = "email"
    available = True

    def send(self, message: NotificationMessage) -> ChannelResult:
        raise RuntimeError("relay exploded")


@pytest.fixture()
def standalone_alarm(db_session) -> Alarm:
    db_session.add(
        AlarmDefinition(
            alarm_key="pump.overcurrent",
            name="Pump overcurrent",
            severity="major",
            escalation_path=[{"stage": 1, "after_s": 0, "channels": ["log", "email"], "role": "operator"}],
            notes=[],
        )
    )
    alarm = Alarm(
        alarm_key="pump.overcurrent",
        asset_id="water.pump.irrigation.01",
        severity="major",
        state="active",
        detected_at=T0,
        activated_at=T0,
        message="Pump overcurrent",
    )
    db_session.add(alarm)
    db_session.commit()
    return alarm


def notifier_for(db_session, channel, **overrides) -> Notifier:
    settings = settings_with(
        notification_backends="log,email", notify_retry_interval_s=300, notify_retry_limit=2, **overrides
    )
    return Notifier(db_session, settings, channels={"log": LogChannel(), "email": channel})


def rows(db_session, channel: str) -> list[NotificationLog]:
    ordered = db_session.query(NotificationLog).order_by(NotificationLog.sent_at.asc()).all()
    return [row for row in ordered if row.channel == channel]


def test_a_failed_delivery_is_retried_after_the_interval_and_then_settles(db_session, standalone_alarm):
    channel = ScriptedChannel([STATUS_FAILED, STATUS_SENT])

    first = notifier_for(db_session, channel).dispatch_pending(at(0))
    assert first.dispatches == 1
    assert [row.status for row in first.sent] == ["sent"]  # the log channel
    assert [row.channel for row in first.failed] == ["email"]
    assert first.as_dict()["failed"] == 1
    assert [row.status for row in rows(db_session, "email")] == ["failed"]

    # Too soon: nothing is retried, and nothing is told twice.
    soon = notifier_for(db_session, channel).dispatch_pending(at(100))
    assert (soon.dispatches, soon.skipped_duplicate, channel.attempts) == (0, 1, 1)

    # After the interval the failed channel alone is retried, and succeeds.
    retry = notifier_for(db_session, channel).dispatch_pending(at(300))
    assert (retry.retried, retry.dispatches, len(retry.sent), channel.attempts) == (1, 1, 1, 2)
    assert [row.status for row in rows(db_session, "email")] == ["failed", "sent"]
    assert len(rows(db_session, "log")) == 1
    assert "retry" in notification_detail(rows(db_session, "email")[-1])["reason"]

    # Settled: the stage has been told on every channel.
    later = notifier_for(db_session, channel).dispatch_pending(at(900))
    assert (later.dispatches, later.skipped_duplicate, channel.attempts) == (0, 1, 2)


def test_retries_are_bounded_by_the_limit(db_session, standalone_alarm):
    channel = ScriptedChannel([STATUS_FAILED] * 10)
    for seconds in (0, 300, 600, 900, 1200, 1500):
        notifier_for(db_session, channel).dispatch_pending(at(seconds))
    # The first attempt plus notify_retry_limit retries, then the log shows why.
    assert channel.attempts == 3
    assert [row.status for row in rows(db_session, "email")] == ["failed"] * 3


def test_not_configured_is_settled_not_retried(db_session, standalone_alarm):
    channel = ScriptedChannel([STATUS_NOT_CONFIGURED])
    notifier_for(db_session, channel).dispatch_pending(at(0))
    notifier_for(db_session, channel).dispatch_pending(at(600))
    assert channel.attempts == 1


def test_a_channel_that_raises_is_a_failed_row(db_session, standalone_alarm):
    result = notifier_for(db_session, ExplodingChannel()).dispatch_pending(at(0))
    assert len(result.failed) == 1
    detail = notification_detail(rows(db_session, "email")[0])
    assert "RuntimeError: relay exploded" in detail["channel_detail"]


def test_result_counts_name_every_bucket():
    assert set(NotificationResult().as_dict()) == {
        "dispatches",
        "sent",
        "failed",
        "not_configured",
        "skipped_duplicate",
        "escalated",
        "retried",
    }
