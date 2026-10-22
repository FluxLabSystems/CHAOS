"""The compose stacks pass every platform setting through, and keep secrets apart.

deploy/docker-compose.yml and deploy/docker-compose.secondary.yml carry a
hand-written ``environment:`` block for the twin service. A setting added to
``chaos.config.Settings`` and documented in deploy/.env.example that never
reaches that block is a setting the container deployment silently ignores,
which is how the first pass lost the notification, overview and plugin
settings. These tests make that drift a failure.

They parse the YAML directly rather than running ``docker compose config``,
so they need no Docker; CI runs ``config -q`` as well.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from chaos.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "deploy"
PRIMARY = DEPLOY / "docker-compose.yml"
SECONDARY = DEPLOY / "docker-compose.secondary.yml"
ENV_EXAMPLE = DEPLOY / ".env.example"

#: Settings the stacks deliberately do not take from the environment: the image
#: sets both as ENV in deploy/Dockerfile, and they are not deployment choices.
IMAGE_FIXED = {"CHAOS_DATA_DIR", "CHAOS_SCHEMA_DIR"}

#: Names the platform reads straight from the process environment rather than
#: through the settings model, so they are deliberately not Settings fields.
#: The EMS override file is one: pydantic's .env handling is not consulted for
#: it, which is exactly why compose has to pass it through.
ENVIRONMENT_ONLY = {"CHAOS_EMS_CONFIG_FILE"}

#: Values a stack fixes by its own topology; they must be present as keys but
#: are not expected to be `${VAR}` pass-throughs.
TOPOLOGY_FIXED = {
    "CHAOS_NODE_ROLE",
    "CHAOS_DATABASE_URL",
    "CHAOS_MQTT_HOST",
    "CHAOS_MQTT_CLIENT_ID",
    "CHAOS_API_HOST",
    "CHAOS_API_PORT",
}

_SECRET_PATTERN = re.compile(r"(PASSWORD|SECRET|TOKEN)$")
_VARIABLE_REFERENCE = re.compile(r"\$\{([A-Z0-9_]+)")


def _twin_environment(path: Path) -> dict[str, str]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    environment = document["services"]["twin"]["environment"]
    assert isinstance(environment, dict), f"{path.name}: the twin environment must be a mapping"
    return {str(key): str(value) for key, value in environment.items()}


def _twin_volumes(path: Path) -> list[str]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [str(volume) for volume in document["services"]["twin"]["volumes"]]


def _settings_env_names() -> set[str]:
    return {f"CHAOS_{name.upper()}" for name in Settings.model_fields}


def _env_example_chaos_names() -> set[str]:
    names = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#?\s*(CHAOS_[A-Z0-9_]+)=", line)
        if match:
            names.add(match.group(1))
    return names


def _env_example_all_names() -> set[str]:
    names = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#?\s*([A-Z][A-Z0-9_]+)=", line)
        if match:
            names.add(match.group(1))
    return names


@pytest.mark.parametrize("compose", [PRIMARY, SECONDARY], ids=["primary", "secondary"])
def test_every_platform_setting_reaches_the_twin_service(compose):
    expected = _settings_env_names() - IMAGE_FIXED
    present = set(_twin_environment(compose))
    missing = sorted(expected - present)
    assert not missing, f"{compose.name}: Settings fields the twin never receives: {missing}"


@pytest.mark.parametrize("compose", [PRIMARY, SECONDARY], ids=["primary", "secondary"])
def test_every_documented_chaos_variable_reaches_the_twin_service(compose):
    """Everything deploy/.env.example offers, commented out or not, must do something."""
    documented = _env_example_chaos_names()
    present = set(_twin_environment(compose))
    missing = sorted(documented - present)
    assert not missing, f"{compose.name}: documented in .env.example but never passed to the twin: {missing}"


@pytest.mark.parametrize("compose", [PRIMARY, SECONDARY], ids=["primary", "secondary"])
def test_the_twin_only_receives_names_the_platform_or_its_plugins_read(compose):
    known = _settings_env_names() | ENVIRONMENT_ONLY | {"TZ"}
    for name in _twin_environment(compose):
        assert name in known or name.startswith("CHAOS_PLUGIN_"), (
            f"{compose.name}: {name} is set on the twin but is not a Settings field or a plugin option"
        )


@pytest.mark.parametrize("compose", [PRIMARY, SECONDARY], ids=["primary", "secondary"])
def test_pass_throughs_are_overridable_from_the_env_file(compose):
    """Anything not fixed by the topology is `${VAR...}`, so deploy/.env decides."""
    environment = _twin_environment(compose)
    hard_coded_by_design = TOPOLOGY_FIXED | {"TZ"}
    if compose == SECONDARY:
        hard_coded_by_design |= {
            "CHAOS_ALLOW_PHYSICAL_CONTROL",
            "CHAOS_EMS_ENABLED",
            "CHAOS_MQTT_ENABLED",
            "CHAOS_ALARM_ENGINE_ENABLED",
            "CHAOS_HISTORIAN_BACKEND",
        }
    fixed = sorted(
        name for name, value in environment.items() if "${" not in value and name not in hard_coded_by_design
    )
    assert not fixed, f"{compose.name}: hard-coded without a documented reason: {fixed}"


@pytest.mark.parametrize("compose", [PRIMARY, SECONDARY], ids=["primary", "secondary"])
def test_every_variable_the_stack_references_is_in_the_env_example(compose):
    referenced = set(_VARIABLE_REFERENCE.findall(compose.read_text(encoding="utf-8")))
    documented = _env_example_all_names()
    missing = sorted(referenced - documented)
    assert not missing, f"{compose.name} reads variables deploy/.env.example never mentions: {missing}"


@pytest.mark.parametrize("compose", [PRIMARY, SECONDARY], ids=["primary", "secondary"])
def test_the_certificate_directory_is_mounted_read_only(compose):
    """deploy/.env.example gives certificate paths under /run/secrets; something has to put them there."""
    mounts = [volume for volume in _twin_volumes(compose) if volume.rsplit(":", 2)[1] == "/run/secrets"]
    assert len(mounts) == 1, f"{compose.name}: expected one /run/secrets mount, found {mounts}"
    assert mounts[0].endswith(":ro"), f"{compose.name}: the secrets mount must be read-only"
    assert mounts[0].startswith("${SECRETS_DIR:-"), "the host directory must come from SECRETS_DIR"
    assert (DEPLOY / "secrets" / ".gitkeep").is_file(), (
        "the default directory must exist so Docker never creates it as root"
    )


def test_the_secondary_hard_codes_the_role_and_the_safety_gate():
    environment = _twin_environment(SECONDARY)
    assert environment["CHAOS_NODE_ROLE"] == "secondary"
    assert environment["CHAOS_ALLOW_PHYSICAL_CONTROL"] == "false"
    assert environment["CHAOS_EMS_ENABLED"] == "false"
    assert _twin_environment(PRIMARY)["CHAOS_NODE_ROLE"] == "primary"


def test_the_secondary_takes_every_secret_from_a_secondary_variable():
    """A different structure is a different trust subject: no credential is shared with the primary."""
    environment = _twin_environment(SECONDARY)
    shared = []
    for name, value in environment.items():
        if not _SECRET_PATTERN.search(name):
            continue
        for referenced in _VARIABLE_REFERENCE.findall(value):
            if not referenced.startswith("SECONDARY_"):
                shared.append(f"{name} <- {referenced}")
    assert not shared, f"secrets the secondary node would share with the primary: {shared}"


def test_the_mqtt_port_is_not_pinned_to_the_plaintext_listener():
    for compose in (PRIMARY, SECONDARY):
        assert "${CHAOS_MQTT_PORT:-1883}" in _twin_environment(compose)["CHAOS_MQTT_PORT"], compose.name


def test_the_backup_directory_is_where_the_backup_script_looks():
    """deploy/backup/backup.sh pulls `chaos backup` archives from /app/var/backups."""
    script = (DEPLOY / "backup" / "backup.sh").read_text(encoding="utf-8")
    assert "/app/var/backups" in script
    assert (
        _twin_environment(PRIMARY)["CHAOS_BACKUP_DIRECTORY"] == "${CHAOS_BACKUP_DIRECTORY:-/app/var/backups}"
    )


def test_env_example_secrets_are_placeholders():
    """Mirrors the CI check so it fails here first."""
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#?\s*([A-Z0-9_]+(?:PASSWORD|SECRET|TOKEN))=(.*)$", line)
        if match and match.group(2).strip():
            assert "CHANGE_ME" in match.group(2), (
                f"{match.group(1)} carries a value that is not a placeholder"
            )
