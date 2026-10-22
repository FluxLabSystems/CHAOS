"""``POST /_supervisor/stop``: the graceful stop the process supervisor calls.

The contract is shared with the .NET supervisor (windows/src/Chaos.Host.Supervisor),
so every clause is pinned here:

* the route does not exist until ``CHAOS_SUPERVISOR_STOP_SECRET`` is set, so a
  container or a development checkout answers 404 like any unknown path;
* it accepts loopback peers only, and the secret is compared in constant time;
* it answers ``202 {"stopping": true}`` and only then triggers the shutdown,
  through the hook ``chaos serve`` registers -- or, with no hook, SIGINT;
* it is unlisted: absent from the OpenAPI schema, and outside every prefix the
  gateway's route-ownership manifest claims, so the gateway never proxies it.
"""

from __future__ import annotations

import signal

import pytest
from fastapi.testclient import TestClient

from chaos import cli
from chaos.api.app import SUPERVISOR_SECRET_HEADER, SUPERVISOR_STOP_PATH, create_app, is_loopback_peer
from chaos.config import Settings
from chaos.mqtt import InMemoryBus

SECRET = "correct-horse-battery-staple"


def _settings(**overrides) -> Settings:
    base = {
        "database_url": "sqlite://",
        "mqtt_enabled": False,
        "ems_enabled": False,
        "alarm_engine_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


def _client(app, peer: tuple[str, int] = ("127.0.0.1", 40000)) -> TestClient:
    return TestClient(app, client=peer)


@pytest.fixture()
def stop_app(engine, session_factory):
    app = create_app(
        _settings(supervisor_stop_secret=SECRET), bus=InMemoryBus(), start_services=False, init_db=False
    )
    app.state.engine = engine
    app.state.session_factory = session_factory
    return app


@pytest.fixture()
def calls(stop_app) -> list[str]:
    recorded: list[str] = []
    stop_app.state.request_shutdown = lambda: recorded.append("hook")
    return recorded


# ---------------------------------------------------------------------------
# The route only exists once a secret is configured
# ---------------------------------------------------------------------------


def test_route_is_absent_without_a_secret(engine, session_factory):
    app = create_app(_settings(), bus=InMemoryBus(), start_services=False, init_db=False)
    app.state.engine = engine
    app.state.session_factory = session_factory
    called: list[str] = []
    app.state.request_shutdown = lambda: called.append("hook")

    with _client(app) as client:
        response = client.post(SUPERVISOR_STOP_PATH, headers={SUPERVISOR_SECRET_HEADER: "anything"})

    assert response.status_code == 404
    assert called == []


def test_an_empty_secret_in_the_environment_means_unset(monkeypatch):
    monkeypatch.setenv("CHAOS_SUPERVISOR_STOP_SECRET", "")
    assert Settings(database_url="sqlite://").supervisor_stop_secret is None


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_wrong_secret_is_refused(stop_app, calls):
    with _client(stop_app) as client:
        response = client.post(SUPERVISOR_STOP_PATH, headers={SUPERVISOR_SECRET_HEADER: "wrong"})
    assert response.status_code == 403
    assert calls == []


def test_missing_secret_header_is_refused(stop_app, calls):
    with _client(stop_app) as client:
        response = client.post(SUPERVISOR_STOP_PATH)
    assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize("peer", [("10.10.20.5", 51000), ("192.168.1.20", 51000), ("testclient", 50000)])
def test_non_loopback_peer_is_refused_even_with_the_right_secret(stop_app, calls, peer):
    with _client(stop_app, peer=peer) as client:
        response = client.post(SUPERVISOR_STOP_PATH, headers={SUPERVISOR_SECRET_HEADER: SECRET})
    assert response.status_code == 403
    assert calls == []


def test_a_relayed_request_presents_the_lan_client_and_is_refused(stop_app, calls):
    """The gateway always sets X-Forwarded-For; uvicorn's proxy_headers makes that the peer."""
    with _client(stop_app, peer=("10.10.20.5", 51000)) as client:
        response = client.post(
            SUPERVISOR_STOP_PATH,
            headers={SUPERVISOR_SECRET_HEADER: SECRET, "X-Forwarded-For": "10.10.20.5"},
        )
    assert response.status_code == 403
    assert calls == []


def test_get_is_not_a_way_in(stop_app, calls):
    with _client(stop_app) as client:
        response = client.get(SUPERVISOR_STOP_PATH, headers={SUPERVISOR_SECRET_HEADER: SECRET})
    assert response.status_code == 405
    assert calls == []


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("peer", [("127.0.0.1", 40000), ("::1", 40000), ("::ffff:127.0.0.1", 40000)])
def test_loopback_peer_with_the_secret_gets_202_and_the_hook_runs(stop_app, calls, peer):
    with _client(stop_app, peer=peer) as client:
        response = client.post(SUPERVISOR_STOP_PATH, headers={SUPERVISOR_SECRET_HEADER: SECRET})
    assert response.status_code == 202
    assert response.json() == {"stopping": True}
    assert calls == ["hook"]


def test_without_a_hook_the_route_falls_back_to_sigint(stop_app, monkeypatch):
    raised: list[int] = []
    monkeypatch.setattr(signal, "raise_signal", lambda signum: raised.append(signum))
    assert not hasattr(stop_app.state, "request_shutdown")

    with _client(stop_app) as client:
        response = client.post(SUPERVISOR_STOP_PATH, headers={SUPERVISOR_SECRET_HEADER: SECRET})

    assert response.status_code == 202
    assert raised == [signal.SIGINT]


def test_the_route_is_unlisted_and_outside_the_proxied_prefixes(stop_app):
    with _client(stop_app) as client:
        schema = client.get("/openapi.json").json()
    assert SUPERVISOR_STOP_PATH not in schema["paths"]
    # The gateway proxies exactly these prefixes (RouteOwnershipDefaults.cs);
    # an unclaimed path is never forwarded.
    for prefix in ("/health", "/host", "/api/v1", "/openapi.json", "/docs", "/redoc"):
        assert not SUPERVISOR_STOP_PATH.startswith(prefix)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("10.10.20.5", False),
        ("::ffff:10.10.20.5", False),
        ("testclient", False),
        ("", False),
        (None, False),
    ],
)
def test_is_loopback_peer(host, expected):
    assert is_loopback_peer(host) is expected


# ---------------------------------------------------------------------------
# `chaos serve` wires the hook to uvicorn's should_exit flag
# ---------------------------------------------------------------------------


def test_serve_registers_a_hook_that_flips_should_exit(monkeypatch):
    import uvicorn

    monkeypatch.setenv("CHAOS_MQTT_ENABLED", "false")
    monkeypatch.setenv("CHAOS_SUPERVISOR_STOP_SECRET", SECRET)
    servers: list[uvicorn.Server] = []

    def fake_run(self, sockets=None):
        servers.append(self)
        self.started = True

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)

    assert (
        cli.main(["--database-url", "sqlite://", "serve", "--host", "127.0.0.1", "--port", "0"])
        == cli.EXIT_OK
    )
    assert len(servers) == 1
    server = servers[0]
    assert server.should_exit is False
    app = server.config.app
    assert callable(app.state.request_shutdown)
    app.state.request_shutdown()
    assert server.should_exit is True
    assert server.config.host == "127.0.0.1"
    assert server.config.port == 0


def test_serve_reports_a_server_that_never_started(monkeypatch, capsys):
    import uvicorn

    monkeypatch.setenv("CHAOS_MQTT_ENABLED", "false")

    def fake_run(self, sockets=None):
        self.started = False

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    assert cli.main(["--database-url", "sqlite://", "serve", "--port", "0"]) == cli.EXIT_ERROR
    assert "did not start" in capsys.readouterr().err


def test_serve_reload_keeps_uvicorns_reloader(monkeypatch):
    """--reload is development only: it goes through uvicorn.run with the factory string."""
    import uvicorn

    monkeypatch.setenv("CHAOS_MQTT_ENABLED", "false")
    seen: dict = {}

    def fake_uvicorn_run(target, **kwargs):
        seen["target"] = target
        seen.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
    assert cli.main(["--database-url", "sqlite://", "serve", "--reload", "--port", "0"]) == cli.EXIT_OK
    assert seen["target"] == "chaos.api.app:create_app"
    assert seen["factory"] is True
    assert seen["reload"] is True
