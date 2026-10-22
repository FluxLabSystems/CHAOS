"""The platform announces its own availability on the bus (SDD 8.2).

``service_unavailable`` is a class-scoped alarm on ``application_service``
assets watching ``availability_state``; without a will nobody ever published
that state for the API service itself. ``PahoBus`` now registers a retained
``offline`` last will before connecting, publishes ``online`` on connect and
``offline`` on a clean stop. paho is replaced by a recording fake: no socket is
opened here.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from chaos import topics
from chaos.config import PRIMARY_PLATFORM_ASSET_ID, Settings
from chaos.envelope import parse_availability
from chaos.mqtt import AVAILABILITY_SOURCE, InMemoryBus, PahoBus, build_bus

API = "it.application_service.rack_01.digital_twin_api_01"


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []

    def __init__(self, *args, **kwargs) -> None:
        self.calls: list[tuple] = []
        self.on_connect = self.on_disconnect = self.on_message = None
        FakeClient.instances.append(self)

    def username_pw_set(self, username, password=None) -> None:
        self.calls.append(("username_pw_set", username, password))

    def tls_set(self, **kwargs) -> None:
        self.calls.append(("tls_set", kwargs))

    def will_set(self, topic, payload=None, qos=0, retain=False) -> None:
        self.calls.append(("will_set", topic, payload, qos, retain))

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.calls.append(("publish", topic, payload, qos, retain))

        class _Info:
            def wait_for_publish(self, timeout=None) -> None:
                pass

        return _Info()

    def subscribe(self, topic_filter, qos=0) -> None:
        self.calls.append(("subscribe", topic_filter, qos))

    def connect_async(self, host, port, keepalive=60) -> None:
        self.calls.append(("connect_async", host, port))

    def loop_start(self) -> None:
        self.calls.append(("loop_start",))

    def loop_stop(self) -> None:
        self.calls.append(("loop_stop",))

    def disconnect(self) -> None:
        self.calls.append(("disconnect",))

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


def _availability(payload: bytes):
    return parse_availability(payload)


def test_the_will_is_registered_before_connecting(fake_paho):
    bus = PahoBus(_settings(), availability_asset_id=API)
    bus.start(timeout=0)
    client = fake_paho.instances[0]
    order = [call[0] for call in client.calls]
    assert order.index("will_set") < order.index("connect_async")

    (will,) = client.named("will_set")
    _, topic, payload, qos, retain = will
    assert (
        topic
        == topics.availability_topic(API, "chaos")
        == "chaos/it/rack_01/application_service_digital_twin_api_01/availability"
    )
    assert qos == 1 and retain is True
    envelope = _availability(payload)
    assert envelope.asset_id == API
    assert envelope.state == "offline"
    assert envelope.source == AVAILABILITY_SOURCE


def test_online_is_published_retained_on_connect(fake_paho):
    bus = PahoBus(_settings(), availability_asset_id=API)
    bus.start(timeout=0)
    client = fake_paho.instances[0]
    assert client.named("publish") == []
    client.on_connect(client, None, {}, 0)
    (published,) = client.named("publish")
    _, topic, payload, qos, retain = published
    assert topic == bus.availability_topic
    assert qos == 1 and retain is True
    assert _availability(payload).state == "online"


def test_a_clean_stop_publishes_offline_then_disconnects(fake_paho):
    bus = PahoBus(_settings(), availability_asset_id=API)
    bus.start(timeout=0)
    client = fake_paho.instances[0]
    client.on_connect(client, None, {}, 0)
    bus.stop()
    names = [call[0] for call in client.calls]
    last_publish = max(index for index, name in enumerate(names) if name == "publish")
    assert last_publish < names.index("disconnect")
    _, topic, payload, _, retain = client.named("publish")[-1]
    assert topic == bus.availability_topic and retain is True
    assert _availability(payload).state == "offline"


def test_a_stop_that_never_connected_does_not_claim_to_have_been_online(fake_paho):
    bus = PahoBus(_settings(), availability_asset_id=API)
    bus.start(timeout=0)
    bus.stop()
    client = fake_paho.instances[0]
    assert client.named("publish") == []
    assert client.named("disconnect")


def test_without_an_asset_nothing_about_availability_is_sent(fake_paho):
    bus = PahoBus(_settings())
    bus.start(timeout=0)
    client = fake_paho.instances[0]
    client.on_connect(client, None, {}, 0)
    bus.stop()
    assert client.named("will_set") == []
    assert client.named("publish") == []
    assert bus.availability_topic is None


def test_build_bus_uses_the_primary_asset_by_default_and_none_on_the_secondary(fake_paho):
    primary = build_bus(_settings())
    assert isinstance(primary, PahoBus)
    assert primary.availability_asset_id == PRIMARY_PLATFORM_ASSET_ID == API

    secondary = build_bus(_settings(node_role="secondary"))
    assert isinstance(secondary, PahoBus)
    assert secondary.availability_asset_id is None

    named = build_bus(
        _settings(node_role="secondary", platform_asset_id="it.server.secondary_control_node.01")
    )
    assert named.availability_asset_id == "it.server.secondary_control_node.01"


def test_the_in_memory_bus_has_no_will_and_says_nothing():
    """No broker, nothing to outlive the process: availability is the producer's job there."""
    bus = build_bus(_settings(mqtt_enabled=False))
    assert isinstance(bus, InMemoryBus)
    assert not hasattr(bus, "availability_topic")
