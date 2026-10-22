"""The effective EMS configuration (SDD 30.2) and its audit trail (SDD 39 EMS-T015).

``load_ems_config`` builds the configuration the EMS runs on -- the built-in
defaults overridden by the file ``CHAOS_EMS_CONFIG_FILE`` names -- validates
every override against the ``EmsConfig`` field it targets, and records one
``configuration_revisions`` row whenever the effective configuration differs
from the last one recorded. The service consumes it when no configuration is
passed in, and re-reads it at every tick so a threshold change takes effect
without a restart.

The file is always written here; nothing reads the real environment, and the
revision table is the shared in-memory database every test gets.
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa
from test_ems_shedding import ALL_LOADS
from test_ems_state_machine import seed_current_state

from chaos.ems import RecordingCommandPort
from chaos.ems.config import (
    DEFAULT_CONFIG,
    EMS_CONFIG_FILE_ENV,
    EMS_CONFIG_TARGET_TYPE,
    EmsConfig,
    EmsConfigError,
    build_config,
    config_file_path,
    load_ems_config,
    plain_config,
)
from chaos.ems.loader import load_schedule
from chaos.ems.service import EnergyManagerService
from chaos.models.base import utcnow
from chaos.models.registry import Asset, ConfigurationRevision

SITE = "site.site.primary.01"


def write(tmp_path, text: str, name: str = "ems.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def revisions(session) -> list[ConfigurationRevision]:
    return list(
        session.scalars(
            sa.select(ConfigurationRevision)
            .where(ConfigurationRevision.target_type == EMS_CONFIG_TARGET_TYPE)
            .order_by(ConfigurationRevision.revision)
        )
    )


# ---------------------------------------------------------------------------
# Building the effective configuration
# ---------------------------------------------------------------------------


def test_without_the_variable_the_defaults_are_in_force():
    load = load_ems_config(environ={})
    assert load.config == DEFAULT_CONFIG
    assert load.source.kind == "defaults"
    assert load.source.path is None
    assert load.source.overrides == {}
    assert load.source.env_var == EMS_CONFIG_FILE_ENV
    assert load.revision_id is None and not load.recorded
    assert config_file_path({EMS_CONFIG_FILE_ENV: "  "}) is None


def test_file_overrides_are_applied_and_attributed(tmp_path):
    path = write(
        tmp_path,
        "changed_by: owner.a\n"
        "reason: bench trial of a lower warning threshold\n"
        "battery_temp_warning_c: 42\n"
        "dwell_normal_s: 900\n"
        "publish_retained: false\n"
        "shed_states: [CONSERVE, CRITICAL_RESERVE]\n"
        "thermal_derate_container_curve: [[38, 0], [44, 40], [50, 100]]\n"
        "restore_stagger_s: {none: 1, low: 2, medium: 3, high: 4}\n",
    )
    load = load_ems_config(path=path)
    config = load.config
    assert config.battery_temp_warning_c == 42.0
    assert isinstance(config.battery_temp_warning_c, float)
    # Item 1: the derived battery curve follows the overridden threshold.
    assert config.thermal_derate_battery_curve == ((42.0, 0.0), (55.0, 100.0))
    assert config.dwell_normal_s == 900
    assert config.publish_retained is False
    assert config.shed_states == ("CONSERVE", "CRITICAL_RESERVE")
    assert config.thermal_derate_container_curve == ((38.0, 0.0), (44.0, 40.0), (50.0, 100.0))
    assert config.stagger_s("high") == 4
    # Everything else is the documented default.
    assert config.surplus_soc_pct == DEFAULT_CONFIG.surplus_soc_pct

    assert load.source.kind == "file"
    assert load.source.path == str(path)
    assert load.source.changed_by == "owner.a"
    assert load.source.reason == "bench trial of a lower warning threshold"
    assert load.source.overrides == {
        "battery_temp_warning_c": 42.0,
        "dwell_normal_s": 900,
        "publish_retained": False,
        "shed_states": ["CONSERVE", "CRITICAL_RESERVE"],
        "thermal_derate_container_curve": [[38.0, 0.0], [44.0, 40.0], [50.0, 100.0]],
        "restore_stagger_s": {"none": 1, "low": 2, "medium": 3, "high": 4},
    }


def test_the_variable_names_the_file_and_json_is_accepted(tmp_path):
    path = write(tmp_path, json.dumps({"surplus_minimum_window_min": 45}), name="ems.json")
    load = load_ems_config(environ={EMS_CONFIG_FILE_ENV: str(path)})
    assert load.config.surplus_minimum_window_min == 45
    assert load.source.changed_by == f"unattributed ({EMS_CONFIG_FILE_ENV})"
    assert load.source.reason == f"EMS configuration loaded from {path}"


def test_an_empty_file_is_the_defaults_from_a_file(tmp_path):
    load = load_ems_config(path=write(tmp_path, "# nothing overridden yet\n"))
    assert load.config == DEFAULT_CONFIG
    assert load.source.kind == "file" and load.source.overrides == {}


# ---------------------------------------------------------------------------
# Validation: every problem named, nothing coerced beyond int -> float
# ---------------------------------------------------------------------------


def test_wrong_types_and_unknown_fields_are_rejected_together(tmp_path):
    path = write(
        tmp_path,
        "battery_temp_warning_c: hot\n"
        "dwell_normal_s: 1.5\n"
        "publish_retained: 1\n"
        "shed_states: CONSERVE\n"
        "surplus_soc_pct: true\n"
        "restore_stagger_s: {medium: fast}\n"
        "thermal_derate_battery_curve: [[45, 0, 1]]\n"
        "battery_temp_warning: 40\n",
    )
    with pytest.raises(EmsConfigError) as excinfo:
        load_ems_config(path=path)
    message = str(excinfo.value)
    assert message.startswith(f"EMS configuration file {path} rejected:")
    assert "battery_temp_warning_c: expected float, got 'hot' (str)" in message
    assert "dwell_normal_s: expected int, got 1.5 (float)" in message
    assert "publish_retained: expected bool, got 1 (int)" in message
    assert "shed_states: expected tuple[str, ...], got 'CONSERVE' (str)" in message
    assert "surplus_soc_pct: expected float, got True (bool)" in message
    assert "restore_stagger_s['medium']: expected int, got 'fast' (str)" in message
    assert "thermal_derate_battery_curve[0]: expected 2 items, got 3" in message
    assert "battery_temp_warning: not an EmsConfig field" in message


def test_combinations_the_config_refuses_are_rejected_with_the_same_clarity(tmp_path):
    with pytest.raises(EmsConfigError, match="battery_temp_warning_c .* must be below"):
        load_ems_config(path=write(tmp_path, "battery_temp_warning_c: 60\n"))
    with pytest.raises(EmsConfigError, match="restore_stagger_s must define the 'medium'"):
        load_ems_config(path=write(tmp_path, "restore_stagger_s: {none: 1}\n"))
    with pytest.raises(EmsConfigError, match="changed_by: expected a non-empty string"):
        load_ems_config(path=write(tmp_path, "changed_by: 42\n"))


def test_a_missing_or_malformed_file_is_an_error_not_the_defaults(tmp_path):
    with pytest.raises(EmsConfigError, match="does not exist"):
        load_ems_config(path=tmp_path / "absent.yaml")
    with pytest.raises(EmsConfigError, match="is not valid YAML"):
        load_ems_config(path=write(tmp_path, "battery_temp_warning_c: [unterminated\n"))
    with pytest.raises(EmsConfigError, match="is not valid JSON"):
        load_ems_config(path=write(tmp_path, "{", name="ems.json"))
    with pytest.raises(EmsConfigError, match="must hold a mapping"):
        load_ems_config(path=write(tmp_path, "- 1\n- 2\n"))


def test_build_config_accepts_null_for_a_derived_curve():
    config = build_config({"thermal_derate_battery_curve": None, "battery_temp_warning_c": 40})
    assert config.thermal_derate_battery_curve == ((40.0, 0.0), (55.0, 100.0))


# ---------------------------------------------------------------------------
# The configuration revision (EMS-T015)
# ---------------------------------------------------------------------------


def test_the_first_load_records_one_revision_and_repeats_add_nothing(db_session, settings):
    first = load_ems_config(db_session, settings=settings, environ={})
    db_session.commit()
    assert first.recorded and first.revision == 1 and first.revision_id

    again = load_ems_config(db_session, settings=settings, environ={})
    assert not again.recorded
    assert again.revision_id == first.revision_id and again.revision == 1

    rows = revisions(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.target_type == "ems_config" and row.target_id == settings.site_id
    assert row.changed_by == "chaos.ems.config (built-in defaults)"
    assert row.diff["old"] is None
    assert row.diff["new"] == plain_config(DEFAULT_CONFIG)
    assert row.diff["changes"] == {}
    assert row.diff["compared_with"] == "built-in defaults"
    assert row.diff["approved_by"] is None
    assert row.diff["source"]["kind"] == "defaults"


def test_a_changed_file_is_one_revision_with_old_and_new_values(db_session, settings, tmp_path):
    path = write(tmp_path, "changed_by: owner.a\nreason: first trial\nbattery_temp_warning_c: 42\n")
    first = load_ems_config(db_session, settings=settings, path=path)
    assert first.recorded and first.revision == 1
    row = revisions(db_session)[0]
    assert row.changed_by == "owner.a" and row.reason == "first trial"
    # The first revision is compared with the built-in defaults.
    assert row.diff["changes"] == {
        "battery_temp_warning_c": {"old": 45.0, "new": 42.0},
        "thermal_derate_battery_curve": {
            "old": [[45.0, 0.0], [55.0, 100.0]],
            "new": [[42.0, 0.0], [55.0, 100.0]],
        },
    }

    # Same content, different attribution: not a change.
    write(tmp_path, "changed_by: owner.b\nreason: re-saved\nbattery_temp_warning_c: 42\n")
    unchanged = load_ems_config(db_session, settings=settings, path=path)
    assert not unchanged.recorded and unchanged.revision == 1

    write(
        tmp_path,
        "changed_by: owner.b\nreason: second trial\nbattery_temp_warning_c: 44\ndwell_normal_s: 300\n",
    )
    second = load_ems_config(db_session, settings=settings, path=path)
    assert second.recorded and second.revision == 2
    rows = revisions(db_session)
    assert [r.revision for r in rows] == [1, 2]
    assert rows[1].changed_by == "owner.b"
    assert rows[1].diff["old"] == rows[0].diff["new"]
    assert rows[1].diff["compared_with"] == "previous revision"
    assert rows[1].diff["changes"] == {
        "battery_temp_warning_c": {"old": 42.0, "new": 44.0},
        "thermal_derate_battery_curve": {
            "old": [[42.0, 0.0], [55.0, 100.0]],
            "new": [[44.0, 0.0], [55.0, 100.0]],
        },
        "dwell_normal_s": {"old": 600, "new": 300},
    }
    assert rows[1].diff["approved_by"] is None

    # Back to the defaults is a change too, and the diff says so.
    third = load_ems_config(db_session, settings=settings, environ={})
    assert third.recorded and third.revision == 3
    assert revisions(db_session)[2].diff["changes"]["dwell_normal_s"] == {"old": 300, "new": 600}


def test_the_revision_is_per_site(db_session, settings):
    load_ems_config(db_session, settings=settings, environ={})
    other = settings.model_copy(update={"site_id": "site.site.secondary.01"})
    load = load_ems_config(db_session, settings=other, environ={})
    assert load.recorded and load.revision == 1
    assert {r.target_id for r in revisions(db_session)} == {settings.site_id, "site.site.secondary.01"}


# ---------------------------------------------------------------------------
# The service consumes it
# ---------------------------------------------------------------------------


@pytest.fixture()
def energy_db(db_session):
    """Registry rows, the load schedule and a healthy, fresh input set."""
    for asset_id in ALL_LOADS:
        db_session.add(
            Asset(
                asset_id=asset_id,
                domain="energy",
                asset_class="load",
                name=asset_id,
                status="planned",
                criticality="critical",
                control_authority="supervisory",
            )
        )
    db_session.flush()
    load_schedule(db_session)
    seed_current_state(db_session, ts=utcnow())
    db_session.commit()
    return db_session


@pytest.fixture()
def enabled(settings):
    return settings.model_copy(update={"ems_enabled": True})


def test_the_service_takes_the_effective_configuration_when_none_is_passed(
    session_factory, bus, enabled, db_session, tmp_path, monkeypatch
):
    path = write(tmp_path, "changed_by: owner.a\nreason: trial\nbattery_temp_warning_c: 42\n")
    monkeypatch.setenv(EMS_CONFIG_FILE_ENV, str(path))
    service = EnergyManagerService(session_factory, bus, enabled, command_port=RecordingCommandPort())
    assert service.config.battery_temp_warning_c == 42.0
    assert service.config_load is not None and service.config_load.recorded
    assert service.config_load.source.path == str(path)
    # Every coordinator runs on the same object.
    for component in (
        service.state_machine,
        service.leases,
        service.shedding,
        service.generator,
        service.blackstart,
    ):
        assert component.config is service.config
    assert len(revisions(db_session)) == 1


def test_an_explicit_configuration_is_the_callers_and_records_nothing(
    session_factory, bus, enabled, db_session
):
    config = EmsConfig(battery_temp_warning_c=40.0)
    service = EnergyManagerService(
        session_factory, bus, enabled, config=config, command_port=RecordingCommandPort()
    )
    assert service.config is config
    assert service.config_load is None
    assert revisions(db_session) == []


def test_a_rejected_file_stops_the_service_from_being_built(
    session_factory, bus, enabled, tmp_path, monkeypatch
):
    monkeypatch.setenv(EMS_CONFIG_FILE_ENV, str(write(tmp_path, "battery_temp_warning_c: hot\n")))
    with pytest.raises(EmsConfigError, match="battery_temp_warning_c: expected float"):
        EnergyManagerService(session_factory, bus, enabled, command_port=RecordingCommandPort())


def test_a_threshold_change_takes_effect_at_the_next_tick(
    session_factory, bus, enabled, energy_db, tmp_path, monkeypatch
):
    path = write(tmp_path, "changed_by: owner.a\nreason: trial\nbattery_temp_warning_c: 42\n")
    monkeypatch.setenv(EMS_CONFIG_FILE_ENV, str(path))
    service = EnergyManagerService(session_factory, bus, enabled, command_port=RecordingCommandPort())
    result = service.tick(utcnow())
    assert result.config_revision == 1 and result.config_error is None
    assert result.as_dict()["config_revision"] == 1

    write(tmp_path, "changed_by: owner.b\nreason: commissioned value\nbattery_temp_warning_c: 44\n")
    result = service.tick(utcnow())
    assert result.config_revision == 2
    assert service.config.battery_temp_warning_c == 44.0
    assert service.config.thermal_derate_battery_curve == ((44.0, 0.0), (55.0, 100.0))
    for component in (
        service.state_machine,
        service.leases,
        service.shedding,
        service.generator,
        service.blackstart,
    ):
        assert component.config is service.config
    rows = revisions(energy_db)
    assert [r.revision for r in rows] == [1, 2]
    assert rows[1].changed_by == "owner.b"
    assert rows[1].diff["changes"]["battery_temp_warning_c"] == {"old": 42.0, "new": 44.0}

    # A broken file keeps the last good configuration in force and says so.
    write(tmp_path, "battery_temp_warning_c: hot\n")
    result = service.tick(utcnow())
    assert result.config_revision == 2
    assert result.config_error is not None and "battery_temp_warning_c: expected float" in result.config_error
    assert service.config.battery_temp_warning_c == 44.0
    assert len(revisions(energy_db)) == 2

    # Fixed again: no new revision, because the effective configuration is unchanged.
    write(tmp_path, "changed_by: owner.b\nreason: commissioned value\nbattery_temp_warning_c: 44\n")
    result = service.tick(utcnow())
    assert result.config_error is None and result.config_revision == 2
    assert len(revisions(energy_db)) == 2
