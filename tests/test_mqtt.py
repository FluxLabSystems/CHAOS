"""The MQTT bus: TLS material actually reaches the client.

``deploy/mosquitto/README.md`` issues the broker certificate from a private CA.
A client that enables TLS without being told about that CA can never verify the
broker, so these tests pin that ``CHAOS_MQTT_TLS_CA``, ``_CERT`` and ``_KEY`` are
handed to paho, that verification is never relaxed unless asked for by name,
and that a mistyped path fails loudly instead of quietly falling back to the
system trust store.

paho is replaced by a recording fake: nothing here opens a socket.
"""

from __future__ import annotations

import logging
import ssl
from typing import ClassVar

import pytest

from chaos.config import Settings
from chaos.mqtt import PahoBus


class FakeClient:
    """Records the configuration calls ``PahoBus._build_client`` makes."""

    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, *args, **kwargs) -> None:
        self.calls: list[tuple] = []
        self.on_connect = self.on_disconnect = self.on_message = None
        FakeClient.instances.append(self)

    def username_pw_set(self, username, password=None) -> None:
        self.calls.append(("username_pw_set", username, password))

    def tls_set(self, **kwargs) -> None:
        self.calls.append(("tls_set", kwargs))

    def tls_insecure_set(self, value: bool) -> None:
        self.calls.append(("tls_insecure_set", value))

    def named(self, name: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == name]


@pytest.fixture()
def fake_paho(monkeypatch):
    import paho.mqtt.client as mqtt

    FakeClient.instances.clear()
    monkeypatch.setattr(mqtt, "Client", FakeClient)
    return FakeClient


def _settings(**overrides) -> Settings:
    base = {"database_url": "sqlite://", "mqtt_enabled": True}
    base.update(overrides)
    return Settings(**base)


def test_plaintext_bus_never_touches_tls(fake_paho):
    client = PahoBus(_settings(mqtt_tls=False))._build_client()
    assert client.named("tls_set") == []
    assert client.named("tls_insecure_set") == []


def test_private_ca_reaches_the_client_with_verification_required(fake_paho, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("-- not a real certificate; only its presence matters here --")
    client = PahoBus(_settings(mqtt_tls=True, mqtt_tls_ca=str(ca)))._build_client()

    (call,) = client.named("tls_set")
    kwargs = call[1]
    assert kwargs["ca_certs"] == str(ca)
    assert kwargs["certfile"] is None and kwargs["keyfile"] is None
    assert kwargs["cert_reqs"] == ssl.CERT_REQUIRED
    assert kwargs["tls_version"] == ssl.PROTOCOL_TLS_CLIENT
    # Hostname verification is on unless somebody turns it off by name.
    assert client.named("tls_insecure_set") == []


def test_client_certificate_and_key_are_passed_through(fake_paho, tmp_path):
    ca, cert, key = (tmp_path / name for name in ("ca.crt", "client.crt", "client.key"))
    for path in (ca, cert, key):
        path.write_text("placeholder")
    settings = _settings(mqtt_tls=True, mqtt_tls_ca=str(ca), mqtt_tls_cert=str(cert), mqtt_tls_key=str(key))
    client = PahoBus(settings)._build_client()
    kwargs = client.named("tls_set")[0][1]
    assert (kwargs["ca_certs"], kwargs["certfile"], kwargs["keyfile"]) == (str(ca), str(cert), str(key))


def test_a_missing_ca_file_fails_loudly_rather_than_falling_back(fake_paho, tmp_path):
    missing = tmp_path / "does-not-exist.crt"
    with pytest.raises(FileNotFoundError, match="CHAOS_MQTT_TLS_CA"):
        PahoBus(_settings(mqtt_tls=True, mqtt_tls_ca=str(missing)))._build_client()
    # Nothing was configured on the client before the failure surfaced.
    assert all(client.named("tls_set") == [] for client in fake_paho.instances)


def test_tls_without_a_ca_warns_that_a_private_ca_broker_cannot_be_verified(fake_paho, caplog):
    with caplog.at_level(logging.WARNING, logger="chaos.mqtt"):
        client = PahoBus(_settings(mqtt_tls=True))._build_client()
    assert client.named("tls_set")[0][1]["ca_certs"] is None
    assert "CHAOS_MQTT_TLS_CA" in caplog.text


def test_insecure_mode_is_explicit_and_logged(fake_paho, tmp_path, caplog):
    ca = tmp_path / "ca.crt"
    ca.write_text("placeholder")
    settings = _settings(mqtt_tls=True, mqtt_tls_ca=str(ca), mqtt_tls_insecure=True)
    with caplog.at_level(logging.WARNING, logger="chaos.mqtt"):
        client = PahoBus(settings)._build_client()
    assert client.named("tls_insecure_set") == [("tls_insecure_set", True)]
    assert "DISABLED" in caplog.text


def test_insecure_defaults_to_false():
    assert Settings(database_url="sqlite://").mqtt_tls_insecure is False
