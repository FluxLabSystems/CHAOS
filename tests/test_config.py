"""Settings validation: what the environment may and may not say.

Every ``CHAOS_*`` variable maps onto a field of :class:`chaos.config.Settings`.
These tests pin the two places where accepting a value silently would be worse
than refusing it: a historian backend that nothing implements, and an empty
string standing in for an absent credential.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chaos.config import OPTIONAL_STRING_SETTINGS, Settings


@pytest.mark.parametrize("backend", ["sql", "none"])
def test_historian_backend_accepts_the_two_backends_that_exist(backend):
    assert Settings(database_url="sqlite://", historian_backend=backend).historian_backend == backend


@pytest.mark.parametrize("backend", ["influx", "timescale", "SQL", ""])
def test_historian_backend_refuses_a_backend_nothing_implements(backend):
    """A name nothing implements used to be accepted and stamped into point_samples_index."""
    with pytest.raises(ValidationError) as error:
        Settings(database_url="sqlite://", historian_backend=backend)
    message = str(error.value)
    assert "historian_backend" in message
    assert "'sql'" in message and "'none'" in message


def test_historian_backend_is_validated_from_the_environment(monkeypatch):
    monkeypatch.setenv("CHAOS_HISTORIAN_BACKEND", "influx")
    with pytest.raises(ValidationError):
        Settings(database_url="sqlite://")


@pytest.mark.parametrize("field", OPTIONAL_STRING_SETTINGS)
def test_an_empty_optional_string_means_unset(field):
    """`CHAOS_X: ${CHAOS_X:-}` in a compose file hands over "" for an unset variable."""
    assert getattr(Settings(database_url="sqlite://", **{field: ""}), field) is None
    assert getattr(Settings(database_url="sqlite://", **{field: "   "}), field) is None
    assert getattr(Settings(database_url="sqlite://", **{field: "value"}), field) == "value"


def test_every_optional_string_setting_is_declared_optional():
    for field in OPTIONAL_STRING_SETTINGS:
        assert Settings.model_fields[field].default is None, (
            f"{field} is listed as optional but has a default"
        )


def test_required_strings_keep_the_empty_string():
    """The rule applies to the listed optionals only; a base topic of "" is a configuration error to see."""
    assert Settings(database_url="sqlite://", mqtt_base_topic="").mqtt_base_topic == ""
