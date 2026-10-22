"""The simulator's command line calls itself what the installer calls it."""

from __future__ import annotations

import pathlib
import tomllib

import pytest

from simulator import cli as simulator_cli

PYPROJECT = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"


def _console_script_name() -> str:
    scripts = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["scripts"]
    (name,) = [name for name, target in scripts.items() if target == "simulator.cli:main"]
    return name


def test_the_parser_is_named_after_the_console_script():
    assert simulator_cli.build_parser().prog == _console_script_name() == "chaos-simulator"


def test_usage_names_the_installed_command(capsys):
    with pytest.raises(SystemExit) as excinfo:
        simulator_cli.main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: chaos-simulator")
    assert "homestead-simulator" not in out


def test_list_scenarios_exits_zero_and_lists_them(capsys):
    assert simulator_cli.main(["--list-scenarios"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Available scenarios:")
    assert "clear_summer_day" in out


def test_the_broker_identity_matches_the_command_name():
    """The MQTT client id is what shows up in the broker log and the ACL."""
    args = simulator_cli.build_parser().parse_args(["--offline"])
    site, bus, _collector = simulator_cli.build_site(args, simulator_cli.get_scenario("clear_summer_day"))
    try:
        assert site.settings.mqtt_client_id == "chaos-simulator"
    finally:
        bus.stop()
