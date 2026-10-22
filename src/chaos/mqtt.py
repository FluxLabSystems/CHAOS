"""Message-bus abstraction over MQTT.

Two implementations share one interface:

* :class:`PahoBus` -- Eclipse Mosquitto over paho-mqtt, the deployment path.
* :class:`InMemoryBus` -- a synchronous in-process broker used by tests, the
  simulator's offline mode and bench testing (SDD section 19 step 1).

Everything that touches the bus depends on :class:`MessageBus`, so no subsystem
needs a live broker to be exercised.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from chaos.config import Settings, get_settings

logger = logging.getLogger(__name__)

Handler = Callable[["Message"], None]


@dataclass(frozen=True)
class Message:
    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False

    @property
    def text(self) -> str:
        return self.payload.decode("utf-8", errors="replace")


def topic_matches(filter_: str, topic: str) -> bool:
    """MQTT wildcard matching for ``+`` (one level) and ``#`` (rest)."""
    if filter_ == "#":
        return not topic.startswith("$")
    f_parts = filter_.split("/")
    t_parts = topic.split("/")
    for index, f_part in enumerate(f_parts):
        if f_part == "#":
            return index <= len(t_parts)
        if index >= len(t_parts):
            return False
        if f_part == "+":
            continue
        if f_part != t_parts[index]:
            return False
    return len(f_parts) == len(t_parts)


class MessageBus(Protocol):
    def publish(self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False) -> None: ...

    def subscribe(self, topic_filter: str, handler: Handler) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


@dataclass
class InMemoryBus:
    """Synchronous in-process bus.

    Publishing delivers to matching subscribers immediately on the calling
    thread, which makes tests deterministic. Retained messages are replayed to
    late subscribers exactly like a real broker.
    """

    subscriptions: list[tuple[str, Handler]] = field(default_factory=list)
    retained: dict[str, Message] = field(default_factory=dict)
    published: list[Message] = field(default_factory=list)
    running: bool = False

    def publish(self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False) -> None:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        message = Message(topic=topic, payload=payload, qos=qos, retain=retain)
        self.published.append(message)
        if retain:
            if payload:
                self.retained[topic] = message
            else:
                self.retained.pop(topic, None)
        for topic_filter, handler in list(self.subscriptions):
            if topic_matches(topic_filter, topic):
                handler(message)

    def subscribe(self, topic_filter: str, handler: Handler) -> None:
        self.subscriptions.append((topic_filter, handler))
        for topic, message in list(self.retained.items()):
            if topic_matches(topic_filter, topic):
                handler(message)

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    # -- test helpers ----------------------------------------------------
    def clear(self) -> None:
        self.published.clear()

    def topics(self) -> list[str]:
        return [m.topic for m in self.published]

    def last(self, topic_filter: str) -> Message | None:
        for message in reversed(self.published):
            if topic_matches(topic_filter, message.topic):
                return message
        return None


class PahoBus:
    """MQTT bus backed by paho-mqtt."""

    def __init__(self, settings: Settings | None = None, client_id: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.client_id = client_id or self.settings.mqtt_client_id
        self._subscriptions: list[tuple[str, Handler]] = []
        self._lock = threading.Lock()
        self._client = None
        self._connected = threading.Event()

    def _build_client(self):
        import paho.mqtt.client as mqtt

        try:  # paho-mqtt 2.x
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id, clean_session=False
            )
        except AttributeError:  # pragma: no cover - paho-mqtt 1.x
            client = mqtt.Client(client_id=self.client_id, clean_session=False)

        if self.settings.mqtt_username:
            client.username_pw_set(self.settings.mqtt_username, self.settings.mqtt_password)
        if self.settings.mqtt_tls:
            self._configure_tls(client)

        def on_connect(_client, _userdata, _flags, reason_code, *_args):
            if reason_code not in (0, "Success"):
                logger.error("MQTT connect failed: %s", reason_code)
                return
            logger.info("MQTT connected to %s:%s", self.settings.mqtt_host, self.settings.mqtt_port)
            self._connected.set()
            with self._lock:
                for topic_filter, _ in self._subscriptions:
                    _client.subscribe(topic_filter, qos=1)

        def on_disconnect(_client, _userdata, *args):  # pragma: no cover - network path
            self._connected.clear()
            logger.warning("MQTT disconnected: %s", args[-1] if args else "unknown")

        def on_message(_client, _userdata, msg):
            message = Message(topic=msg.topic, payload=msg.payload, qos=msg.qos, retain=msg.retain)
            with self._lock:
                handlers = [h for f, h in self._subscriptions if topic_matches(f, msg.topic)]
            for handler in handlers:
                try:
                    handler(message)
                except Exception:  # pragma: no cover - handler isolation
                    logger.exception("MQTT handler failed for topic %s", msg.topic)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        return client

    def _configure_tls(self, client) -> None:
        """Apply ``CHAOS_MQTT_TLS_*`` to the client.

        The broker certificate is issued by the property's private CA
        (deploy/mosquitto/README.md), so ``CHAOS_MQTT_TLS_CA`` is what makes
        verification possible: a bare ``tls_set()`` would trust only the
        system store and could never verify that broker. Each configured file
        must exist -- a mistyped CA path that silently fell back to the system
        store would leave the connection failing for a reason nobody can see.
        Verification is only relaxed when ``mqtt_tls_insecure`` is set
        explicitly, and that is logged as the weakening it is.
        """
        import ssl

        settings = self.settings
        material = {
            "CHAOS_MQTT_TLS_CA": settings.mqtt_tls_ca,
            "CHAOS_MQTT_TLS_CERT": settings.mqtt_tls_cert,
            "CHAOS_MQTT_TLS_KEY": settings.mqtt_tls_key,
        }
        for name, path in material.items():
            if path and not Path(path).is_file():
                raise FileNotFoundError(f"{name} points at {path}, which is not a readable file")
        if settings.mqtt_tls_cert and not settings.mqtt_tls_key:
            logger.warning(
                "CHAOS_MQTT_TLS_CERT is set without CHAOS_MQTT_TLS_KEY; the certificate file "
                "must therefore carry its own private key"
            )
        client.tls_set(
            ca_certs=settings.mqtt_tls_ca or None,
            certfile=settings.mqtt_tls_cert or None,
            keyfile=settings.mqtt_tls_key or None,
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        if settings.mqtt_tls_ca:
            logger.info("MQTT TLS enabled; broker certificate verified against %s", settings.mqtt_tls_ca)
        else:
            logger.warning(
                "MQTT TLS enabled without CHAOS_MQTT_TLS_CA: the broker certificate is verified "
                "against the system trust store only, which cannot verify a private-CA broker"
            )
        if settings.mqtt_tls_insecure:
            logger.warning(
                "CHAOS_MQTT_TLS_INSECURE is set: broker hostname verification is DISABLED. "
                "This is acceptable on a bench and nowhere else."
            )
            client.tls_insecure_set(True)

    def publish(self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False) -> None:
        if self._client is None:
            raise RuntimeError("PahoBus.start() must be called before publishing")
        self._client.publish(topic, payload, qos=qos, retain=retain)

    def subscribe(self, topic_filter: str, handler: Handler) -> None:
        with self._lock:
            self._subscriptions.append((topic_filter, handler))
        if self._client is not None and self._connected.is_set():
            self._client.subscribe(topic_filter, qos=1)

    def start(self, timeout: float = 5.0) -> None:
        self._client = self._build_client()
        self._client.connect_async(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=60)
        self._client.loop_start()
        self._connected.wait(timeout=timeout)

    def stop(self) -> None:
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
        self._connected.clear()


def build_bus(settings: Settings | None = None) -> MessageBus:
    """Return the configured bus, falling back to in-memory when MQTT is off."""
    settings = settings or get_settings()
    if not settings.mqtt_enabled:
        logger.info("MQTT disabled; using in-memory bus")
        return InMemoryBus()
    return PahoBus(settings)
