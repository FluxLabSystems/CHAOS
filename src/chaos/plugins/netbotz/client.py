"""Talking to a NetBotz appliance -- over HTTPS, and honestly about SNMP.

The transport is a separate, injectable object for two reasons. The obvious one
is that it makes the mapping testable without an appliance. The other is that it
lets the plugin ship exactly what it can back: :class:`HttpTransport` reads a
NetBotz 5.x appliance's REST API over verified TLS, and
:class:`UnconfiguredTransport` is a working object that reports exactly what is
missing and returns no readings -- the shape the notification channels in
:mod:`chaos.alarms.notify` take for voice.

Nothing in this module ever reports a sensor value that did not come from a
device. An integration you believe in but that does not exist is worse than no
integration, because the temperature it is not reporting looks like a
temperature that is fine.

**What is confirmed about the appliance, and what is not.** The NetBotz 5.x
user guide (990-5934, "REST API") documents that the API is served at
``https://<appliance>/rest`` as JSON, that its interactive documentation is at
``https://<appliance>/docs/rest`` (log on to the Web UI first), that
``GET /rest/assets`` lists the appliance's assets with an ``id`` and a
``label``, and that times are milliseconds since the UNIX epoch. It does not
document the operation that lists sensor *values* or that operation's field
names, and this module does not guess them: the readings path is a setting
with no default, and the field names are settings with documented defaults.
A path that answers 404, a response of the wrong shape, or items without the
configured fields are reported as failures -- on the plugin's health, on the
mirror's counters and in the log -- never turned into readings.

``snmp`` is not implemented. NetBotz appliances expose sensors through the
NETBOTZV2-MIB (enterprise 5528) and the PowerNet MIB, and the platform
declares no SNMP dependency. The column layout of those tables was not
confirmed against an appliance's own MIB file, so no walk ships: a walk of
the wrong columns would mirror the wrong value under the right name, which is
exactly the failure this plugin exists to avoid.

Both protocols are reachable only from VLAN 10 (SDD 5.4, section 15.3), which is
a deployment fact the plugin cannot verify for you and should not pretend to.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import Any, Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)

#: Transport kinds this plugin recognises in its ``transport`` option.
TRANSPORT_NONE = "none"
TRANSPORT_SNMP = "snmp"
TRANSPORT_HTTP = "http"
TRANSPORT_KINDS = (TRANSPORT_NONE, TRANSPORT_SNMP, TRANSPORT_HTTP)

#: What each transport needs before it can deliver. Surfaced verbatim through
#: the plugin's health so the answer to "why is the NetBotz not mirroring" is
#: on the screen rather than in this file.
REQUIREMENTS: dict[str, str] = {
    TRANSPORT_SNMP: (
        "an SNMP client library (the platform declares no SNMP dependency), SNMPv3 authPriv "
        "credentials for the appliance, and a walk of the NETBOTZV2-MIB sensor tables whose column "
        "layout has been confirmed against the appliance's own MIB file so pods are discovered rather "
        "than hard-coded -- none of which ships in this release"
    ),
    TRANSPORT_HTTP: (
        "CHAOS_PLUGIN_NETBOTZ_HOST (a NetBotz 5.x appliance with its HTTPS REST API enabled), an "
        "account or token with read access, the appliance's certificate trusted on this node "
        "(CHAOS_PLUGIN_NETBOTZ_CA_FILE for a private CA; verification is never disabled), and "
        "CHAOS_PLUGIN_NETBOTZ_READINGS_PATH naming the sensor-listing operation shown at "
        "https://<host>/docs/rest"
    ),
    TRANSPORT_NONE: (
        "a transport: set CHAOS_PLUGIN_NETBOTZ_TRANSPORT to 'http' (implemented) with the host, "
        "credentials and readings path for the appliance; 'snmp' is not implemented"
    ),
}


@dataclass(frozen=True)
class NetBotzEnclosure:
    """One appliance, as it identifies itself."""

    enclosure_id: str
    label: str | None = None
    model: str | None = None
    firmware: str | None = None
    reachable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "enclosure_id": self.enclosure_id,
            "label": self.label,
            "model": self.model,
            "firmware": self.firmware,
            "reachable": self.reachable,
        }


@dataclass(frozen=True)
class NetBotzSensorReading:
    """One sensor value as the appliance reports it, before any mapping.

    ``sensor_id`` is the appliance's own address for the sensor and is what a
    ``point_bindings.source_address`` row is expected to hold.
    """

    sensor_id: str
    enclosure_id: str
    sensor_type: str
    value: Any
    unit: str | None = None
    label: str | None = None
    ts: dt.datetime | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sensor_id": self.sensor_id,
            "enclosure_id": self.enclosure_id,
            "sensor_type": self.sensor_type,
            "value": self.value,
            "unit": self.unit,
            "label": self.label,
            "ts": self.ts.isoformat() if self.ts else None,
        }


@runtime_checkable
class NetBotzTransport(Protocol):
    """How the plugin reaches an appliance."""

    kind: str
    #: ``False`` means every :meth:`read` will return nothing, and the plugin
    #: reports ``not_configured`` rather than ``degraded``: nothing has broken,
    #: nothing has been set up.
    configured: bool

    def describe(self) -> dict[str, Any]:
        """Non-secret facts for the plugin's health and its API. Never credentials."""
        ...

    def enclosures(self) -> Sequence[NetBotzEnclosure]: ...

    def read(self) -> Sequence[NetBotzSensorReading]: ...


class UnconfiguredTransport:
    """A transport that has nothing to talk to, and says so.

    It is a real object with real behaviour -- returning nothing and explaining
    why -- rather than ``None`` guarded by ``if`` statements at each call site.
    """

    configured = False

    def __init__(self, kind: str = TRANSPORT_NONE, *, reason: str | None = None, host: str | None = None):
        self.kind = kind if kind in TRANSPORT_KINDS else TRANSPORT_NONE
        self.host = host
        self.requirement = REQUIREMENTS.get(self.kind, REQUIREMENTS[TRANSPORT_NONE])
        self.reason = reason or (
            f"The '{self.kind}' transport has no implementation in this release."
            if self.kind != TRANSPORT_NONE
            else "No NetBotz transport is selected."
        )

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "configured": False,
            "host": self.host,
            "reason": self.reason,
            "requirement": self.requirement,
        }

    def enclosures(self) -> Sequence[NetBotzEnclosure]:
        return ()

    def read(self) -> Sequence[NetBotzSensorReading]:
        return ()

    @property
    def detail(self) -> str:
        """One sentence for the plugin's health line."""
        return f"{self.reason} Required before it can deliver: {self.requirement}."


# ---------------------------------------------------------------------------
# HTTPS
# ---------------------------------------------------------------------------


class NetBotzTransportError(RuntimeError):
    """The appliance could not be read: a status, a transport failure or a response of the wrong shape."""


#: Option keys (``CHAOS_PLUGIN_NETBOTZ_<KEY>``) the HTTP transport reads, with
#: their defaults. ``readings_path`` has none on purpose: see the module
#: docstring.
@dataclass(frozen=True)
class HttpTransportConfig:
    host: str
    port: int = 443
    #: Confirmed by the NetBotz 5.x user guide.
    base_path: str = "/rest"
    assets_path: str = "assets"
    #: Dotted key under which the assets response holds its list; blank means
    #: the response body is the list.
    assets_key: str = ""
    #: Value for the ``types`` query parameter of the assets operation (the
    #: guide shows ``camera``); blank asks for every asset.
    asset_types: str = ""
    #: The operation that lists sensor values, relative to ``base_path``. Not
    #: documented publicly; read it off https://<host>/docs/rest.
    readings_path: str = ""
    readings_key: str = ""
    username: str = ""
    password: str = ""
    token: str = ""
    #: If the appliance's API uses a cookie session, the login operation
    #: (relative to ``base_path``) that takes ``{"username", "password"}``.
    login_path: str = ""
    #: CA bundle to trust for the appliance's certificate. Verification is
    #: never disabled; this is how a private CA is trusted.
    ca_file: str = ""
    timeout_s: float = 10.0
    #: How long an assets listing is reused, so a health check per scrape does
    #: not become a request per scrape.
    cache_ttl_s: float = 30.0
    #: Identity the plugin reports for the appliance itself and, unless a
    #: reading names its own enclosure, for every reading. Defaults to host.
    enclosure_id: str = ""
    # Field names. ``id`` and ``label`` are confirmed for assets; the rest are
    # defaults to adjust from the appliance's own documentation.
    asset_id_field: str = "id"
    asset_label_field: str = "label"
    asset_model_field: str = "model"
    asset_firmware_field: str = "firmware"
    reading_id_field: str = "id"
    reading_type_field: str = "type"
    reading_value_field: str = "value"
    reading_unit_field: str = "unit"
    reading_label_field: str = "label"
    reading_ts_field: str = "timestamp"
    reading_enclosure_field: str = ""

    @classmethod
    def from_options(cls, host: str, options: Mapping[str, str] | None = None) -> HttpTransportConfig:
        """Build from plugin options, falling back to the defaults on a bad value."""
        options = options or {}
        values: dict[str, Any] = {"host": host.strip()}
        for spec in fields(cls):
            if spec.name == "host":
                continue
            raw = options.get(spec.name)
            if raw is None or str(raw).strip() == "":
                continue
            raw = str(raw).strip()
            if spec.type in ("int", int):
                try:
                    values[spec.name] = int(raw)
                except ValueError:
                    logger.warning(
                        "NetBotz option %s=%r is not an integer; using %s", spec.name, raw, spec.default
                    )
            elif spec.type in ("float", float):
                try:
                    values[spec.name] = float(raw)
                except ValueError:
                    logger.warning(
                        "NetBotz option %s=%r is not a number; using %s", spec.name, raw, spec.default
                    )
            else:
                values[spec.name] = raw
        config = cls(**values)
        if not config.base_path.startswith("/"):
            config = cls(**{**values, "base_path": "/" + config.base_path})
        return config

    @property
    def base_url(self) -> str:
        authority = self.host if self.port == 443 else f"{self.host}:{self.port}"
        return f"https://{authority}{self.base_path.rstrip('/')}"

    @property
    def appliance_id(self) -> str:
        return self.enclosure_id or self.host

    @property
    def auth_mode(self) -> str:
        if self.login_path:
            return "session"
        if self.token:
            return "bearer"
        if self.username:
            return "basic"
        return "none"

    def field_map(self) -> dict[str, str]:
        return {
            spec.name: getattr(self, spec.name)
            for spec in fields(self)
            if spec.name.endswith("_field") and getattr(self, spec.name)
        }


class HttpTransport:
    """The appliance's HTTPS REST API, read-only, TLS verified.

    ``configured`` is true as soon as there is a host: the transport can then
    prove reachability, trust and credentials through the confirmed assets
    operation, which is the commissioning step that matters. Readings need
    ``readings_path`` as well; until it is set :meth:`read` returns nothing and
    :attr:`readings_ready` is false, and the plugin's health says so.
    """

    kind = TRANSPORT_HTTP
    configured = True

    def __init__(
        self,
        config: HttpTransportConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.host = config.host
        self._transport = transport
        self._clock = clock
        self._client: httpx.Client | None = None
        self._logged_in = False
        self._enclosure_cache: tuple[float, tuple[NetBotzEnclosure, ...]] | None = None
        #: Last failure of any request, for the description and the log.
        self.last_error: str | None = None
        #: Last failure of :meth:`read` specifically; cleared by a successful read.
        self.last_read_error: str | None = None
        self.counters: dict[str, int] = {"requests": 0, "failures": 0, "readings": 0, "malformed_items": 0}

    # -- facts ---------------------------------------------------------------

    @property
    def readings_ready(self) -> bool:
        return bool(self.config.readings_path)

    @property
    def readings_requirement(self) -> str:
        return (
            "set CHAOS_PLUGIN_NETBOTZ_READINGS_PATH to the operation that lists sensor values, as shown "
            f"at https://{self.config.host}/docs/rest (the NetBotz 5.x documentation confirms /rest and "
            "/rest/assets but not the sensor-listing operation, so this release does not guess it); adjust "
            "the CHAOS_PLUGIN_NETBOTZ_READING_*_FIELD names if its JSON differs from the defaults"
        )

    def describe(self) -> dict[str, Any]:
        config = self.config
        return {
            "kind": self.kind,
            "configured": True,
            "host": config.host,
            "base_url": config.base_url,
            "assets_path": config.assets_path,
            "assets_key": config.assets_key or None,
            "readings_path": config.readings_path or None,
            "readings_key": config.readings_key or None,
            "readings_ready": self.readings_ready,
            "auth": config.auth_mode,
            "tls_verification": "required",
            "ca_file": config.ca_file or None,
            "timeout_s": config.timeout_s,
            "enclosure_id": config.appliance_id,
            "field_map": config.field_map(),
            "last_error": self.last_error,
            "last_read_error": self.last_read_error,
            "counters": dict(self.counters),
            "requirement": None if self.readings_ready else self.readings_requirement,
        }

    # -- HTTP ----------------------------------------------------------------

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            config = self.config
            headers = {"Accept": "application/json"}
            if config.token:
                headers["Authorization"] = f"Bearer {config.token}"
            auth = None
            if config.username and config.auth_mode == "basic":
                auth = httpx.BasicAuth(config.username, config.password)
            self._client = httpx.Client(
                base_url=config.base_url,
                # A private CA bundle or the system store. Never False.
                verify=config.ca_file or True,
                timeout=config.timeout_s,
                headers=headers,
                auth=auth,
                transport=self._transport,
            )
            self._logged_in = False
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._logged_in = False

    def _login(self, client: httpx.Client) -> None:
        config = self.config
        path = config.login_path.lstrip("/")
        self.counters["requests"] += 1
        try:
            response = client.post(path, json={"username": config.username, "password": config.password})
        except httpx.HTTPError as exc:
            raise NetBotzTransportError(
                f"{type(exc).__name__}: {exc} (POST {config.base_url}/{path})"
            ) from exc
        if not response.is_success:
            raise NetBotzTransportError(
                f"HTTP {response.status_code} from POST {config.base_url}/{path} (login)"
            )
        self._logged_in = True

    def _get(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        """GET a JSON document from the appliance, or raise a specific error."""
        config = self.config
        path = path.lstrip("/")
        url = f"{config.base_url}/{path}"
        try:
            client = self._ensure_client()
            if config.login_path and not self._logged_in:
                self._login(client)
            self.counters["requests"] += 1
            response = client.get(path, params=params or None)
            if response.status_code == 401 and config.login_path and self._logged_in:
                # The session expired: log in once more, then give up honestly.
                self._logged_in = False
                self._login(client)
                self.counters["requests"] += 1
                response = client.get(path, params=params or None)
        except NetBotzTransportError as exc:
            self._fail(str(exc))
            raise
        except httpx.HTTPError as exc:
            message = f"{type(exc).__name__}: {exc} (GET {url})"
            self._fail(message)
            raise NetBotzTransportError(message) from exc
        if not response.is_success:
            message = f"HTTP {response.status_code} from GET {url}"
            if response.status_code == 404:
                message += " -- the path is a setting, not a confirmed appliance operation; check /docs/rest"
            self._fail(message)
            raise NetBotzTransportError(message)
        try:
            document = response.json()
        except ValueError as exc:
            message = f"GET {url} returned a body that is not JSON ({response.headers.get('content-type')})"
            self._fail(message)
            raise NetBotzTransportError(message) from exc
        self.last_error = None
        return document

    def _fail(self, message: str) -> None:
        self.counters["failures"] += 1
        self.last_error = message
        logger.warning("NetBotz %s: %s", self.config.host, message)

    # -- the transport contract ----------------------------------------------

    def enclosures(self) -> Sequence[NetBotzEnclosure]:
        config = self.config
        now = self._clock()
        if self._enclosure_cache is not None:
            cached_at, cached = self._enclosure_cache
            if now - cached_at < config.cache_ttl_s:
                return cached
        params = {"types": config.asset_types} if config.asset_types else None
        document = self._get(config.assets_path, params)
        items = _extract_list(document, config.assets_key, "assets", "CHAOS_PLUGIN_NETBOTZ_ASSETS_KEY")
        found: list[NetBotzEnclosure] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            identifier = item.get(config.asset_id_field)
            if identifier in (None, ""):
                continue
            found.append(
                NetBotzEnclosure(
                    enclosure_id=str(identifier),
                    label=_text(item.get(config.asset_label_field)),
                    model=_text(item.get(config.asset_model_field)),
                    firmware=_text(item.get(config.asset_firmware_field)),
                    reachable=True,
                )
            )
        if not found:
            # The appliance answered, which is the fact that matters; it just
            # lists no assets (or lists them under other field names).
            found.append(
                NetBotzEnclosure(enclosure_id=config.appliance_id, label=config.host, reachable=True)
            )
        result = tuple(found)
        self._enclosure_cache = (now, result)
        return result

    def read(self) -> Sequence[NetBotzSensorReading]:
        config = self.config
        if not self.readings_ready:
            self.last_read_error = None
            return ()
        try:
            document = self._get(config.readings_path)
            items = _extract_list(
                document, config.readings_key, "readings", "CHAOS_PLUGIN_NETBOTZ_READINGS_KEY"
            )
        except NetBotzTransportError as exc:
            self.last_read_error = str(exc)
            raise

        readings: list[NetBotzSensorReading] = []
        malformed = 0
        for item in items:
            reading = self._reading_from(item)
            if reading is None:
                malformed += 1
                continue
            readings.append(reading)
        self.counters["malformed_items"] += malformed
        if items and not readings:
            message = (
                f"none of the {len(items)} item(s) from GET {config.base_url}/{config.readings_path.lstrip('/')} "
                f"carried the configured fields (id={config.reading_id_field!r}, "
                f"type={config.reading_type_field!r}, value={config.reading_value_field!r}); "
                "adjust CHAOS_PLUGIN_NETBOTZ_READING_*_FIELD from /docs/rest"
            )
            self._fail(message)
            self.last_read_error = message
            raise NetBotzTransportError(message)
        if malformed:
            logger.warning(
                "NetBotz %s: %d of %d reading item(s) lacked the configured fields and were dropped",
                config.host,
                malformed,
                len(items),
            )
        self.counters["readings"] += len(readings)
        self.last_read_error = None
        return readings

    def _reading_from(self, item: Any) -> NetBotzSensorReading | None:
        config = self.config
        if not isinstance(item, dict):
            return None
        identifier = item.get(config.reading_id_field)
        sensor_type = item.get(config.reading_type_field)
        if identifier in (None, "") or sensor_type in (None, "") or config.reading_value_field not in item:
            return None
        enclosure = None
        if config.reading_enclosure_field:
            enclosure = _text(item.get(config.reading_enclosure_field))
        return NetBotzSensorReading(
            sensor_id=str(identifier),
            enclosure_id=enclosure or config.appliance_id,
            sensor_type=str(sensor_type),
            value=item.get(config.reading_value_field),
            unit=_text(item.get(config.reading_unit_field)),
            label=_text(item.get(config.reading_label_field)),
            ts=parse_appliance_time(item.get(config.reading_ts_field)),
            detail={"source": "http"},
        )


def _extract_list(document: Any, key: str, what: str, setting: str) -> list[Any]:
    """Find the list a response holds, or say precisely why it could not be found."""
    node = document
    if key:
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise NetBotzTransportError(
                    f"the {what} response has no {key!r} ({setting}); top-level keys: {_keys(document)}"
                )
            node = node[part]
    if isinstance(node, list):
        return node
    if isinstance(node, dict):
        raise NetBotzTransportError(
            f"the {what} response is a JSON object, not a list; set {setting} to the key that holds "
            f"the list (keys seen: {_keys(node)})"
        )
    raise NetBotzTransportError(f"the {what} response is {type(node).__name__}, not a list")


def _keys(document: Any) -> str:
    if isinstance(document, dict):
        return ", ".join(sorted(str(k) for k in document)[:12]) or "(none)"
    return f"({type(document).__name__})"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_appliance_time(value: Any) -> dt.datetime | None:
    """Milliseconds since the epoch (the NetBotz convention), seconds, or ISO-8601."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    number: float | None = None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        try:
            number = float(text)
        except ValueError:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                parsed = dt.datetime.fromisoformat(text)
            except ValueError:
                return None
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)
    if number > 1e11:  # milliseconds -- a seconds value this large is year 5138
        number /= 1000.0
    try:
        return dt.datetime.fromtimestamp(number, tz=dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Choosing a transport
# ---------------------------------------------------------------------------


def build_transport(
    kind: str | None,
    host: str | None,
    *,
    options: Mapping[str, str] | None = None,
    http_transport: httpx.BaseTransport | None = None,
) -> NetBotzTransport:
    """Choose a transport from configuration.

    ``http`` with a host produces :class:`HttpTransport`. Every other branch
    produces :class:`UnconfiguredTransport` with the reason spelled out, and it
    is written as a dispatch so that an SNMP transport, when its tables have
    been confirmed, is one line here and nothing anywhere else.
    """
    selected = (kind or TRANSPORT_NONE).strip().lower()
    if selected not in TRANSPORT_KINDS:
        return UnconfiguredTransport(
            TRANSPORT_NONE,
            host=host,
            reason=f"Unknown NetBotz transport {selected!r}; expected one of {', '.join(TRANSPORT_KINDS)}.",
        )
    if selected != TRANSPORT_NONE and not host:
        return UnconfiguredTransport(
            selected,
            reason=f"The '{selected}' transport was selected but no host was given.",
        )
    if selected == TRANSPORT_HTTP:
        assert host is not None
        return HttpTransport(HttpTransportConfig.from_options(host, options), transport=http_transport)
    return UnconfiguredTransport(selected, host=host)
