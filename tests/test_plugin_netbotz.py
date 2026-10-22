"""The NetBotz reference plugin.

Two transports ship. The HTTPS one reads a NetBotz 5.x appliance's REST API
and is exercised here through ``httpx.MockTransport``: what the appliance
answers is scripted, and the tests pin that TLS is never relaxed, that the
confirmed assets operation proves reachability, that readings are parsed only
through the configured field map, and that every failure -- a 404, a response
of the wrong shape, items without the configured fields -- is loud rather than
a fabricated reading. The SNMP one is a stub held to the standard the rest of
the platform is held to: it returns nothing, says why, and never lets
``not_configured`` be mistaken for ``ok``.

The mapping, identity routing, API and health reporting are tested against the
real v0.3 design package rather than invented assets -- including end-to-end
passes where an appliance's readings become current state on the real
``it.environmental_monitor.rack_01.nbrk0550_01``.
"""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from chaos import topics
from chaos.config import Settings
from chaos.models.registry import ExternalIdentifier
from chaos.models.telemetry import CurrentState
from chaos.plugins.manager import PluginManager
from chaos.plugins.mirror import MirrorService
from chaos.plugins.netbotz import NetBotzPlugin
from chaos.plugins.netbotz import sensors as sensor_map
from chaos.plugins.netbotz.client import (
    TRANSPORT_HTTP,
    TRANSPORT_NONE,
    TRANSPORT_SNMP,
    HttpTransport,
    HttpTransportConfig,
    NetBotzEnclosure,
    NetBotzSensorReading,
    NetBotzTransportError,
    UnconfiguredTransport,
    build_transport,
    parse_appliance_time,
)
from chaos.plugins.netbotz.mirror import ID_TYPE, PROTOCOL, NetBotzMirrorSource
from chaos.plugins.spec import HEALTH_DEGRADED, HEALTH_NOT_CONFIGURED, HEALTH_OK, PluginContext

RACK_MONITOR = "it.environmental_monitor.rack_01.nbrk0550_01"
T0 = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.UTC)


class ScriptedTransport:
    """A NetBotz appliance that exists only in this process."""

    kind = "scripted"
    configured = True

    def __init__(self, readings=(), enclosures=(), *, raises=None):
        self._readings = list(readings)
        self._enclosures = list(enclosures)
        self._raises = raises

    def describe(self) -> dict:
        return {"kind": self.kind, "configured": True, "host": "scripted"}

    def enclosures(self):
        if self._raises is not None:
            raise self._raises
        return list(self._enclosures)

    def read(self):
        return list(self._readings)


def reading(sensor_type: str, value, *, sensor_id="pod/1", enclosure="nbrk0550-01", unit=None):
    return NetBotzSensorReading(
        sensor_id=sensor_id,
        enclosure_id=enclosure,
        sensor_type=sensor_type,
        value=value,
        unit=unit,
        ts=T0,
    )


# ---------------------------------------------------------------------------
# The sensor catalogue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("vendor", "expected"),
    [
        ("temperature", "temperature"),
        ("Temp", "temperature"),
        ("TEMPERATURE_F", "temperature"),
        ("relative humidity", "humidity"),
        ("dewPoint", "dew_point"),
        ("door-switch", "door"),
        ("rope leak", "fluid"),
        ("Smoke Detector", "smoke"),
        ("comm status", "availability"),
    ],
)
def test_vendor_sensor_names_resolve_through_the_aliases(vendor, expected):
    resolved = sensor_map.sensor_type_for(vendor)
    assert resolved is not None and resolved.key == expected


def test_an_unknown_sensor_type_resolves_to_nothing():
    assert sensor_map.sensor_type_for("flux capacitor") is None
    assert sensor_map.sensor_type_for("") is None
    assert sensor_map.sensor_type_for(None) is None


def test_every_mapped_point_name_exists_in_the_point_dictionary():
    """The mapping may not name a point the design package does not define.

    This is the test that stops the catalogue drifting: a plugin that publishes
    to an undefined point produces a steady stream of dead letters, which is
    exactly the noise that masks real commissioning faults (see F-003 in
    docs/integration-findings.md).
    """
    import yaml

    from chaos.config import DATA_DIR

    document = yaml.safe_load((DATA_DIR / "point_dictionary.yaml").read_text(encoding="utf-8"))
    defined = set(document["points"])

    for sensor in sensor_map.SENSOR_TYPES:
        for name in sensor.point_names:
            assert name in defined, f"{sensor.key} maps to '{name}', which is not in the point dictionary"


def test_every_enum_value_the_plugin_publishes_is_in_the_dictionary():
    """Ingest rejects an enum violation, so an unmapped state would be dropped."""
    import yaml

    from chaos.config import DATA_DIR

    document = yaml.safe_load((DATA_DIR / "point_dictionary.yaml").read_text(encoding="utf-8"))

    for sensor in sensor_map.SENSOR_TYPES:
        if sensor.shape != "enum" or not sensor.mirrorable:
            continue
        allowed = set(document["points"][sensor.point_names[0]]["enum_values"])
        assert set(sensor.enum_values) <= allowed
        table = sensor_map.DOOR_STATES if sensor.key == "door" else sensor_map.AVAILABILITY_STATES
        assert set(table.values()) <= allowed
        assert sensor_map.UNKNOWN in allowed, "coerce() falls back to 'unknown'"


def test_unmirrorable_sensor_types_are_listed_rather_than_hidden():
    """A NetBotz capability with no canonical point is a documented gap."""
    unmirrorable = {sensor.key for sensor in sensor_map.SENSOR_TYPES if not sensor.mirrorable}
    assert {"airflow", "audio", "vibration", "camera_motion"} <= unmirrorable
    for key in unmirrorable:
        assert "no canonical point" in sensor_map.SENSOR_TYPES_BY_KEY[key].summary


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def test_fahrenheit_is_converted_because_the_point_is_named_in_celsius():
    """A mirrored Fahrenheit reading is large enough to defeat freeze protection."""
    temperature = sensor_map.SENSOR_TYPES_BY_KEY["temperature"]
    assert sensor_map.coerce(temperature, 68.0, unit="F") == pytest.approx(20.0)
    assert sensor_map.coerce(temperature, 68.0, unit="degF") == pytest.approx(20.0)
    assert sensor_map.coerce(temperature, 20.0, unit="C") == pytest.approx(20.0)
    assert sensor_map.coerce(temperature, 20.0, unit=None) == pytest.approx(20.0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1, True), (0, False), ("Active", True), ("normal", False), ("WET", True), ("dry", False), (True, True)],
)
def test_boolean_sensors_read_vendor_spellings(raw, expected):
    fluid = sensor_map.SENSOR_TYPES_BY_KEY["fluid"]
    assert sensor_map.coerce(fluid, raw) is expected


def test_an_unreadable_boolean_is_an_error_not_a_reassuring_default():
    """A leak sensor that cannot be read is not a leak sensor reading "dry"."""
    fluid = sensor_map.SENSOR_TYPES_BY_KEY["fluid"]
    with pytest.raises(sensor_map.CoercionError):
        sensor_map.coerce(fluid, "banana")


def test_a_missing_value_is_an_error():
    temperature = sensor_map.SENSOR_TYPES_BY_KEY["temperature"]
    with pytest.raises(sensor_map.CoercionError, match="no value"):
        sensor_map.coerce(temperature, None)


def test_a_non_numeric_value_for_a_numeric_point_is_an_error():
    temperature = sensor_map.SENSOR_TYPES_BY_KEY["temperature"]
    with pytest.raises(sensor_map.CoercionError, match="not numeric"):
        sensor_map.coerce(temperature, "warm")


def test_an_unrecognised_enum_state_becomes_unknown_rather_than_a_guess():
    door = sensor_map.SENSOR_TYPES_BY_KEY["door"]
    assert sensor_map.coerce(door, "opened") == "open"
    assert sensor_map.coerce(door, "who knows") == sensor_map.UNKNOWN


# ---------------------------------------------------------------------------
# The transport, and its honesty
# ---------------------------------------------------------------------------


def test_no_transport_is_selected_by_default():
    transport = build_transport(None, None)
    assert transport.kind == TRANSPORT_NONE
    assert transport.configured is False
    assert transport.read() == ()
    assert transport.enclosures() == ()


def test_snmp_still_reports_that_it_is_unimplemented():
    """This release has no SNMP. Saying otherwise would be the dangerous bug."""
    transport = build_transport(TRANSPORT_SNMP, "10.10.10.40")
    assert transport.configured is False
    assert "no implementation in this release" in transport.reason
    assert "SNMP client library" in transport.requirement
    assert "NETBOTZV2-MIB" in transport.requirement
    assert transport.read() == ()


def test_a_transport_with_no_host_says_so():
    transport = build_transport(TRANSPORT_HTTP, None)
    assert "no host was given" in transport.reason


def test_an_unknown_transport_name_is_named_back(caplog):
    transport = build_transport("carrier_pigeon", "10.10.10.40")
    assert transport.kind == TRANSPORT_NONE
    assert "carrier_pigeon" in transport.reason


def test_the_transport_description_carries_no_credentials():
    described = UnconfiguredTransport(TRANSPORT_SNMP, host="10.10.10.40").describe()
    assert set(described) == {"kind", "configured", "host", "reason", "requirement"}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def setup_plugin(transport=None, options=None) -> NetBotzPlugin:
    plugin = NetBotzPlugin(transport_factory=(lambda context: transport)) if transport else NetBotzPlugin()
    plugin.setup(PluginContext(settings=Settings(), options=options or {}))
    return plugin


def test_an_unconfigured_plugin_reports_not_configured_not_ok():
    health = setup_plugin().health()
    assert health.state == HEALTH_NOT_CONFIGURED
    assert "transport" in health.detail.lower()


def test_a_host_alone_does_not_make_the_plugin_healthy():
    """The base implementation would say ``ok`` here. A host is not a connection."""
    health = setup_plugin(options={"host": "10.10.10.40", "transport": "snmp"}).health()
    assert health.state == HEALTH_NOT_CONFIGURED


def test_a_reachable_appliance_reports_ok():
    transport = ScriptedTransport(enclosures=[NetBotzEnclosure("nbrk0550-01", reachable=True)])
    health = setup_plugin(transport).health()
    assert health.state == HEALTH_OK
    assert health.facts["enclosures"] == 1


def test_an_unreachable_appliance_reports_degraded():
    """Configured and not answering is a different problem from never set up."""
    transport = ScriptedTransport(enclosures=[NetBotzEnclosure("nbrk0550-01", reachable=False)])
    health = setup_plugin(transport).health()
    assert health.state == HEALTH_DEGRADED
    assert "nbrk0550-01" in health.detail


def test_a_transport_that_raises_reports_degraded():
    transport = ScriptedTransport(raises=TimeoutError("no route to host"))
    health = setup_plugin(transport).health()
    assert health.state == HEALTH_DEGRADED
    assert "TimeoutError" in health.detail


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def test_a_reading_is_offered_by_both_identity_routes():
    """Binding address and appliance identity, so it works before and after commissioning."""
    source = NetBotzMirrorSource(ScriptedTransport())
    translated = source.translate(reading("temperature", 21.0, sensor_id="nbrk0550-01/temp/1"))

    assert translated.external_id == "nbrk0550-01/temp/1"
    assert translated.device_id == "nbrk0550-01"
    assert translated.point_name == "temperature_air_c"
    assert translated.alternate_point_names == ("temperature_c", "value")
    assert translated.unit == "°C"


def test_an_unknown_sensor_type_is_counted_as_a_mapping_gap():
    source = NetBotzMirrorSource(ScriptedTransport())
    assert source.translate(reading("flux_capacitor", 1)) is None
    assert source.counters["unknown_sensor_type"] == 1
    assert source.unmapped_types["flux_capacitor"] == 1


def test_a_known_but_unmirrorable_type_is_counted_separately():
    """ "We have no point for airflow" and "we have never heard of it" differ."""
    source = NetBotzMirrorSource(ScriptedTransport())
    assert source.translate(reading("airflow", 2.0)) is None
    assert source.counters["unmirrorable_sensor_type"] == 1
    assert source.counters["unknown_sensor_type"] == 0


def test_an_uncoercible_value_is_counted_and_dropped():
    source = NetBotzMirrorSource(ScriptedTransport())
    assert source.translate(reading("fluid", "banana")) is None
    assert source.counters["uncoercible_value"] == 1


def test_a_source_with_no_transport_is_unavailable():
    source = NetBotzMirrorSource(build_transport(TRANSPORT_NONE, None))
    assert source.available() is False
    assert list(source.read()) == []


def test_reading_translates_the_whole_batch():
    transport = ScriptedTransport(
        [
            reading("temperature", 68.0, sensor_id="a", unit="F"),
            reading("humidity", 41.0, sensor_id="b"),
            reading("airflow", 2.0, sensor_id="c"),
        ]
    )
    produced = list(NetBotzMirrorSource(transport).read())
    assert [item.point_name for item in produced] == ["temperature_air_c", "humidity_relative_pct"]
    assert produced[0].value == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# End to end, against the real design package
# ---------------------------------------------------------------------------


def test_appliance_readings_become_current_state_on_the_real_rack_monitor(
    db_session, session_factory, bus, loaded_registry
):
    """The whole point, exercised on the shipped v0.3 register.

    One ``external_identifiers`` row is the entire commissioning step: no
    binding per sensor, no topic configuration, no change to ingest.
    """
    db_session.add(ExternalIdentifier(asset_id=RACK_MONITOR, id_type=ID_TYPE, value="nbrk0550-01"))
    db_session.commit()

    settings = Settings(database_url="sqlite://", mqtt_enabled=False)
    transport = ScriptedTransport(
        [
            reading("temperature", 24.5, sensor_id="nbrk0550-01/temp/1"),
            reading("humidity", 38.0, sensor_id="nbrk0550-01/humi/1"),
        ]
    )

    from chaos.ingest.service import IngestService

    ingest = IngestService(session_factory, bus, settings, auto_sweep=False, clock=lambda: T0)
    ingest.start()

    service = MirrorService(
        session_factory,
        bus,
        settings,
        [NetBotzMirrorSource(transport)],
        auto_poll=False,
        clock=lambda: T0,
    )
    service.refresh_identity()
    published = service.poll_once(force=True)
    ingest.stop()

    assert published == 2
    temperature = db_session.get(CurrentState, topics.point_id(RACK_MONITOR, "temperature_air_c"))
    humidity = db_session.get(CurrentState, topics.point_id(RACK_MONITOR, "humidity_relative_pct"))
    assert temperature is not None and temperature.value_numeric == pytest.approx(24.5)
    assert humidity is not None and humidity.value_numeric == pytest.approx(38.0)
    assert temperature.quality == "good"


def test_nothing_is_mirrored_until_the_registry_says_what_the_appliance_is(
    db_session, session_factory, bus, loaded_registry
):
    """No external identifier, no binding: the readings are dropped, not guessed."""
    settings = Settings(database_url="sqlite://", mqtt_enabled=False)
    transport = ScriptedTransport([reading("temperature", 24.5, sensor_id="nbrk0550-01/temp/1")])

    service = MirrorService(
        session_factory,
        bus,
        settings,
        [NetBotzMirrorSource(transport)],
        auto_poll=False,
        clock=lambda: T0,
    )
    service.refresh_identity()

    assert service.poll_once(force=True) == 0
    state = service.states["netbotz.sensors"]
    assert state.drops["unresolved_identity"] == 1
    assert "nbrk0550-01/temp/1" in state.unresolved


# ---------------------------------------------------------------------------
# The plugin's API
# ---------------------------------------------------------------------------


def test_the_plugin_declares_the_capabilities_it_provides():
    plugin = setup_plugin()
    assert set(plugin.manifest.capabilities) == {"api", "mirror"}
    assert len(plugin.routers()) == 1
    assert len(plugin.mirror_sources()) == 1
    assert plugin.services() == ()


def test_the_status_endpoint_reports_the_transport(client):
    payload = client.get("/api/v1/ext/netbotz").json()
    assert payload["manifest"]["name"] == "netbotz"
    assert payload["transport"]["configured"] is False
    assert payload["health"]["state"] == HEALTH_NOT_CONFIGURED


def test_the_sensor_type_endpoint_documents_the_mapping(client):
    payload = client.get("/api/v1/ext/netbotz/sensor-types").json()
    assert payload["protocol"] == PROTOCOL
    assert payload["id_type"] == ID_TYPE
    assert payload["mirrorable"] > 0
    assert payload["unmirrorable"] > 0
    temperature = next(entry for entry in payload["sensor_types"] if entry["key"] == "temperature")
    assert temperature["point_names"][0] == "temperature_air_c"


def test_the_readings_endpoint_returns_nothing_when_nothing_is_connected(client):
    payload = client.get("/api/v1/ext/netbotz/readings").json()
    assert payload["count"] == 0
    assert payload["transport"]["configured"] is False


def test_the_bindings_endpoint_says_nothing_refers_to_netbotz_yet(client):
    payload = client.get("/api/v1/ext/netbotz/bindings").json()
    assert payload["summary"] == {
        "enclosures_mapped": 0,
        "bindings_resolvable": 0,
        "bindings_uncommissioned": 0,
    }
    assert "Nothing in the registry refers to NetBotz" in payload["detail"]


def test_the_bindings_endpoint_distinguishes_unbound_from_unreachable(client, db_session):
    """The two failures look identical on a dashboard and have opposite fixes."""
    db_session.add(ExternalIdentifier(asset_id=RACK_MONITOR, id_type=ID_TYPE, value="nbrk0550-01"))
    from chaos.models.registry import Asset, AssetClass

    if db_session.get(AssetClass, "environmental_monitor") is None:
        db_session.add(AssetClass(name="environmental_monitor", allowed_domains=["it"]))
    if db_session.get(Asset, RACK_MONITOR) is None:
        db_session.add(
            Asset(
                asset_id=RACK_MONITOR,
                domain="it",
                asset_class="environmental_monitor",
                name="NetBotz Rack Monitor 550",
            )
        )
    db_session.commit()

    payload = client.get("/api/v1/ext/netbotz/bindings").json()
    assert payload["summary"]["enclosures_mapped"] == 1
    assert payload["external_identifiers"][0]["enclosure_id"] == "nbrk0550-01"
    assert "mapped to assets by external identifier" in payload["detail"]


def test_the_plugin_loads_on_the_secondary_node():
    """The NetBotz 500 is in the container the secondary node exists to outlive."""
    settings = Settings(database_url="sqlite://", mqtt_enabled=False, node_role="secondary")
    record = PluginManager.discover(settings, environ={}).get("netbotz")
    assert record is not None and record.loaded


# ---------------------------------------------------------------------------
# The HTTPS transport, against a scripted appliance
# ---------------------------------------------------------------------------

HOST = "netbotz.lan"
ASSETS = [
    {"id": "nbrk0550-01", "label": "Rack monitor", "model": "NBRK0550", "firmware": "5.1.2"},
    {"id": "camera-22", "label": "Door camera"},
]
SENSORS = [
    {
        "id": "temp-1",
        "type": "temperature",
        "value": 24.5,
        "unit": "C",
        "label": "Rack top",
        "timestamp": 1780000000000,
    },
    {
        "id": "humi-1",
        "type": "humidity",
        "value": 38,
        "unit": "%",
        "label": "Rack top",
        "timestamp": 1780000000000,
    },
]


class Appliance:
    """A scripted NetBotz REST API behind httpx.MockTransport."""

    def __init__(self, *, assets=None, readings=None, status=200, body=None, login=False):
        self.assets = ASSETS if assets is None else assets
        self.readings = SENSORS if readings is None else readings
        self.status = status
        self.body = body
        self.login = login
        self.requests: list[httpx.Request] = []
        self.logins = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.login and path == "/rest/login" and request.method == "POST":
            self.logins += 1
            return httpx.Response(200, json={"ok": True}, headers={"Set-Cookie": "session=abc; Path=/"})
        if self.login and request.headers.get("cookie") != "session=abc":
            return httpx.Response(401, text="not logged in")
        if path == "/rest/assets":
            return httpx.Response(200, json=self.assets)
        if path == "/rest/sensors":
            if self.body is not None:
                return httpx.Response(self.status, json=self.body)
            return httpx.Response(self.status, json=self.readings)
        return httpx.Response(404, text="no such operation")


def http_transport(appliance: Appliance, **options) -> HttpTransport:
    transport = build_transport(
        TRANSPORT_HTTP, HOST, options=options, http_transport=httpx.MockTransport(appliance)
    )
    assert isinstance(transport, HttpTransport)
    return transport


def test_http_transport_is_configured_by_a_host_and_verifies_tls_unconditionally():
    transport = http_transport(Appliance())
    assert transport.kind == TRANSPORT_HTTP and transport.configured is True
    described = transport.describe()
    assert described["base_url"] == "https://netbotz.lan/rest"
    assert described["tls_verification"] == "required"
    assert described["ca_file"] is None
    assert described["auth"] == "none"
    # Nothing that could be a credential is in the description, whatever is set.
    described = http_transport(Appliance(), username="apc", password="hunter2", token="t0k").describe()
    assert "hunter2" not in str(described) and "t0k" not in str(described)
    assert described["auth"] == "bearer"


def test_http_transport_has_no_way_to_disable_verification():
    """There is no option for it: a 'verify' or 'insecure' key is simply unknown."""
    config = HttpTransportConfig.from_options(HOST, {"verify": "false", "insecure": "true"})
    assert not hasattr(config, "verify") and not hasattr(config, "insecure")
    assert http_transport(Appliance(), verify="false").describe()["tls_verification"] == "required"


def test_http_transport_trusts_a_private_ca_through_ca_file(monkeypatch, tmp_path):
    seen: dict = {}
    real_client = httpx.Client

    class RecordingClient(real_client):
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", RecordingClient)
    ca = tmp_path / "ca.crt"
    ca.write_text("placeholder")
    transport = http_transport(Appliance(), ca_file=str(ca))
    transport.enclosures()
    assert seen["verify"] == str(ca)
    transport.close()


def test_http_transport_reads_the_confirmed_assets_operation_as_enclosures():
    appliance = Appliance()
    transport = http_transport(appliance)
    found = transport.enclosures()
    assert [e.as_dict() for e in found] == [
        {
            "enclosure_id": "nbrk0550-01",
            "label": "Rack monitor",
            "model": "NBRK0550",
            "firmware": "5.1.2",
            "reachable": True,
        },
        {
            "enclosure_id": "camera-22",
            "label": "Door camera",
            "model": None,
            "firmware": None,
            "reachable": True,
        },
    ]
    request = appliance.requests[0]
    assert request.method == "GET" and str(request.url) == "https://netbotz.lan/rest/assets"
    assert request.headers["accept"] == "application/json"


def test_http_transport_caches_the_assets_listing_for_the_ttl():
    appliance = Appliance()
    clock = {"now": 0.0}
    transport = HttpTransport(
        HttpTransportConfig.from_options(HOST, {"cache_ttl_s": "30"}),
        transport=httpx.MockTransport(appliance),
        clock=lambda: clock["now"],
    )
    transport.enclosures()
    transport.enclosures()
    assert len(appliance.requests) == 1
    clock["now"] = 31.0
    transport.enclosures()
    assert len(appliance.requests) == 2


def test_an_answering_appliance_with_no_assets_is_still_one_reachable_enclosure():
    transport = http_transport(Appliance(assets=[]))
    (enclosure,) = transport.enclosures()
    assert enclosure.enclosure_id == HOST and enclosure.reachable is True


def test_http_transport_without_a_readings_path_reads_nothing_and_health_says_exactly_what_to_set():
    appliance = Appliance()
    transport = http_transport(appliance)
    assert transport.readings_ready is False
    assert transport.read() == ()
    assert all(r.url.path != "/rest/sensors" for r in appliance.requests)

    health = setup_plugin(transport).health()
    assert health.state == HEALTH_NOT_CONFIGURED
    assert "2 appliance(s) answered" in health.detail
    assert "CHAOS_PLUGIN_NETBOTZ_READINGS_PATH" in health.detail
    assert "docs/rest" in health.detail


def test_http_transport_parses_readings_through_the_default_field_map():
    transport = http_transport(Appliance(), readings_path="sensors")
    assert transport.readings_ready is True
    readings = list(transport.read())
    assert [r.as_dict() for r in readings] == [
        {
            "sensor_id": "temp-1",
            "enclosure_id": HOST,
            "sensor_type": "temperature",
            "value": 24.5,
            "unit": "C",
            "label": "Rack top",
            "ts": "2026-05-28T20:26:40+00:00",
        },
        {
            "sensor_id": "humi-1",
            "enclosure_id": HOST,
            "sensor_type": "humidity",
            "value": 38,
            "unit": "%",
            "label": "Rack top",
            "ts": "2026-05-28T20:26:40+00:00",
        },
    ]
    assert transport.last_read_error is None
    assert setup_plugin(transport).health().state == HEALTH_OK


def test_http_transport_field_map_and_list_key_are_configurable():
    body = {
        "data": {
            "sensors": [
                {"sensorId": "s-9", "sensorType": "dewPoint", "reading": 11.0, "assetId": "nbrk0550-01"},
            ]
        }
    }
    transport = http_transport(
        Appliance(body=body),
        readings_path="sensors",
        readings_key="data.sensors",
        reading_id_field="sensorId",
        reading_type_field="sensorType",
        reading_value_field="reading",
        reading_enclosure_field="assetId",
    )
    (reading,) = transport.read()
    assert (reading.sensor_id, reading.sensor_type, reading.value, reading.enclosure_id) == (
        "s-9",
        "dewPoint",
        11.0,
        "nbrk0550-01",
    )
    assert transport.describe()["field_map"]["reading_id_field"] == "sensorId"


def test_http_transport_fails_loudly_on_a_404_rather_than_inventing_readings():
    transport = http_transport(Appliance(status=404), readings_path="sensors")
    with pytest.raises(NetBotzTransportError, match="HTTP 404") as excinfo:
        transport.read()
    assert "/docs/rest" in str(excinfo.value)
    assert transport.last_read_error is not None

    health = setup_plugin(transport).health()
    assert health.state == HEALTH_DEGRADED
    assert "last readings poll failed" in health.detail
    assert "HTTP 404" in health.detail


def test_http_transport_fails_loudly_on_a_response_of_the_wrong_shape():
    transport = http_transport(Appliance(body={"result": "ok", "count": 2}), readings_path="sensors")
    with pytest.raises(NetBotzTransportError, match="CHAOS_PLUGIN_NETBOTZ_READINGS_KEY") as excinfo:
        transport.read()
    assert "count, result" in str(excinfo.value)  # the keys it saw, so the setting can be chosen

    transport = http_transport(Appliance(body={"rows": []}), readings_path="sensors", readings_key="data")
    with pytest.raises(NetBotzTransportError, match="has no 'data'"):
        transport.read()


def test_http_transport_fails_loudly_when_no_item_carries_the_configured_fields():
    body = [{"sensorId": "s-1", "reading": 1.0}, {"sensorId": "s-2", "reading": 2.0}]
    transport = http_transport(Appliance(body=body), readings_path="sensors")
    with pytest.raises(NetBotzTransportError, match="none of the 2 item"):
        transport.read()
    assert transport.counters["malformed_items"] == 2
    assert "CHAOS_PLUGIN_NETBOTZ_READING_" in transport.last_read_error


def test_http_transport_drops_and_counts_partially_malformed_items():
    body = [SENSORS[0], {"id": "broken", "type": "temperature"}]  # no value field
    transport = http_transport(Appliance(body=body), readings_path="sensors")
    readings = list(transport.read())
    assert [r.sensor_id for r in readings] == ["temp-1"]
    assert transport.counters["malformed_items"] == 1
    assert transport.last_read_error is None


def test_http_transport_reports_transport_errors_as_failures():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    transport = build_transport(TRANSPORT_HTTP, HOST, options={}, http_transport=httpx.MockTransport(handler))
    with pytest.raises(NetBotzTransportError, match="ConnectError"):
        transport.enclosures()
    assert transport.counters["failures"] == 1
    assert setup_plugin(transport).health().state == HEALTH_DEGRADED


def test_http_transport_sends_basic_auth_or_a_bearer_token():
    appliance = Appliance()
    http_transport(appliance, username="apc", password="hunter2").enclosures()
    assert appliance.requests[-1].headers["authorization"].startswith("Basic ")

    appliance = Appliance()
    http_transport(appliance, token="t0k").enclosures()
    assert appliance.requests[-1].headers["authorization"] == "Bearer t0k"


def test_http_transport_logs_in_once_for_a_cookie_session():
    appliance = Appliance(login=True)
    transport = http_transport(
        appliance, username="apc", password="hunter2", login_path="login", readings_path="sensors"
    )
    transport.enclosures()
    transport.read()
    assert appliance.logins == 1
    login = next(r for r in appliance.requests if r.method == "POST")
    assert json.loads(login.content) == {"username": "apc", "password": "hunter2"}
    assert all(r.headers.get("cookie") == "session=abc" for r in appliance.requests if r.method == "GET")


def test_appliance_times_are_milliseconds_since_the_epoch():
    assert parse_appliance_time(1780000000000) == dt.datetime(2026, 5, 28, 20, 26, 40, tzinfo=dt.UTC)
    assert parse_appliance_time("1780000000") == dt.datetime(2026, 5, 28, 20, 26, 40, tzinfo=dt.UTC)
    assert parse_appliance_time("2026-05-28T20:26:40Z") == dt.datetime(2026, 5, 28, 20, 26, 40, tzinfo=dt.UTC)
    assert parse_appliance_time(None) is None
    assert parse_appliance_time("not a time") is None


def test_http_readings_become_current_state_on_the_real_rack_monitor(
    db_session, session_factory, bus, loaded_registry
):
    """The whole path with the real transport: scripted appliance -> HTTPS -> mirror -> ingest."""
    db_session.add(ExternalIdentifier(asset_id=RACK_MONITOR, id_type=ID_TYPE, value="nbrk0550-01"))
    db_session.commit()
    settings = Settings(database_url="sqlite://", mqtt_enabled=False)
    transport = http_transport(Appliance(), readings_path="sensors", enclosure_id="nbrk0550-01")

    from chaos.ingest.service import IngestService

    ingest = IngestService(session_factory, bus, settings, auto_sweep=False, clock=lambda: T0)
    ingest.start()
    service = MirrorService(
        session_factory, bus, settings, [NetBotzMirrorSource(transport)], auto_poll=False, clock=lambda: T0
    )
    service.refresh_identity()
    published = service.poll_once(force=True)
    ingest.stop()

    assert published == 2
    temperature = db_session.get(CurrentState, topics.point_id(RACK_MONITOR, "temperature_air_c"))
    humidity = db_session.get(CurrentState, topics.point_id(RACK_MONITOR, "humidity_relative_pct"))
    assert temperature is not None and temperature.value_numeric == pytest.approx(24.5)
    assert humidity is not None and humidity.value_numeric == pytest.approx(38.0)


def test_the_api_reports_an_unreachable_appliance_as_a_finding_not_a_500(client):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    plugin = client.app.state.plugins.get("netbotz").plugin
    original = plugin.transport
    plugin.transport = build_transport(
        TRANSPORT_HTTP, HOST, options={}, http_transport=httpx.MockTransport(handler)
    )
    try:
        payload = client.get("/api/v1/ext/netbotz/enclosures").json()
        assert payload["count"] == 0 and "ConnectError" in payload["error"]
        payload = client.get("/api/v1/ext/netbotz/readings").json()
        assert payload["count"] == 0 and payload["error"] is None  # no readings path: nothing attempted
    finally:
        plugin.transport = original
