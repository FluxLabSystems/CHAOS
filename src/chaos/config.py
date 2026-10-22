"""Platform configuration.

Every setting is overridable through the environment so that the same image can
run as the primary node in the power container or as the physically separate
secondary control node described in SDD section 16.1.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root: <root>/src/chaos/config.py -> parents[2]
ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
SCHEMA_DIR = ROOT_DIR / "schemas"


#: Optional strings that arrive through the environment as ``""`` when a
#: deployment passes every setting through unconditionally (the compose files
#: do: ``CHAOS_X: ${CHAOS_X:-}``). An empty value means "not set", never "the
#: empty string is the credential".
OPTIONAL_STRING_SETTINGS = (
    "mqtt_username",
    "mqtt_password",
    "mqtt_tls_ca",
    "mqtt_tls_cert",
    "mqtt_tls_key",
    "notify_smtp_host",
    "notify_smtp_username",
    "notify_smtp_password",
    "notify_smtp_from",
    "notify_push_url",
    "notify_push_token",
    "supervisor_stop_secret",
    "backup_directory",
    "platform_asset_id",
)

#: The registry asset that stands for the platform's own API service on the
#: primary node. The secondary node's service has no asset in the register yet
#: (the asset it.server.secondary_control_node.01 leaves ``services`` open), so
#: it publishes no availability until CHAOS_PLATFORM_ASSET_ID names one.
PRIMARY_PLATFORM_ASSET_ID = "it.application_service.rack_01.digital_twin_api_01"


class Settings(BaseSettings):
    """Runtime settings for the digital twin core service."""

    model_config = SettingsConfigDict(
        env_prefix="CHAOS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Identity -------------------------------------------------------
    node_role: str = "primary"  # primary | secondary
    site_id: str = "site.site.primary.01"
    timezone: str = "America/Toronto"

    # --- Storage --------------------------------------------------------
    # SQLite keeps the platform runnable on a laptop and in CI. PostgreSQL +
    # PostGIS is the deployment target (see deploy/docker-compose.yml).
    database_url: str = f"sqlite:///{ROOT_DIR / 'var' / 'homestead.db'}"
    sql_echo: bool = False

    # --- Machine-readable design package --------------------------------
    data_dir: Path = DATA_DIR
    schema_dir: Path = SCHEMA_DIR

    # --- MQTT -----------------------------------------------------------
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_tls: bool = False
    #: TLS material for the broker connection (deploy/mosquitto/README.md,
    #: "Enabling TLS"). The deployment issues the broker certificate from a
    #: private CA, so the CA bundle is what makes verification possible rather
    #: than an optional extra: without it ``tls_set()`` falls back to the
    #: system trust store and a private-CA broker can never be verified. The
    #: client certificate and key are for the later step where the broker
    #: requires one (``require_certificate true``).
    mqtt_tls_ca: str | None = None
    mqtt_tls_cert: str | None = None
    mqtt_tls_key: str | None = None
    #: Skip hostname verification of the broker certificate. Never the default:
    #: a broker that cannot be verified is a broker that must not carry command
    #: traffic. Exists for a bench where the certificate's name cannot match,
    #: and is logged as the weakening it is.
    mqtt_tls_insecure: bool = False
    mqtt_client_id: str = "chaos-core"
    mqtt_base_topic: str = "chaos"
    mqtt_enabled: bool = True

    # --- Historian ------------------------------------------------------
    # "sql" writes samples to the relational historian table (works everywhere);
    # "none" keeps current state only and writes no samples. These are the only
    # backends that exist: the InfluxDB-versus-TimescaleDB decision is open, and
    # a name that nothing implements must not be accepted and stamped into
    # point_samples_index as if it meant something.
    historian_backend: Literal["sql", "none"] = "sql"
    historian_raw_retention_days: int = 90

    # --- Telemetry semantics -------------------------------------------
    default_stale_after_s: int = 60

    # --- API ------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_title: str = "Project CHAOS API"
    api_root_path: str = ""
    #: Shared secret for ``POST /_supervisor/stop``, the graceful-stop route the
    #: Windows process supervisor calls before it would otherwise have to kill
    #: the backend. Unset (the default) means the route does not exist -- a
    #: container or a development checkout has no supervisor, so there is
    #: nothing to present the secret and nothing to answer it. The route is
    #: also loopback-only; see chaos.api.app.
    supervisor_stop_secret: str | None = None

    # --- Overview thresholds -------------------------------------------
    # The property overview derives a freeze/heat risk from the site's outside
    # air temperature. Both trip points are settings rather than constants so a
    # site can move them without a code change; the defaults are commissioning
    # values, not agronomic decisions.
    overview_freeze_risk_c: float = 2.0
    overview_heat_risk_c: float = 32.0

    # --- Control safety -------------------------------------------------
    # Global master switch. Physical control stays disabled until a subsystem
    # has passed the SDD section 19 commissioning sequence.
    allow_physical_control: bool = False
    command_default_ttl_s: int = 300

    # --- EMS ------------------------------------------------------------
    ems_enabled: bool = True
    ems_tick_interval_s: int = 5

    # --- Alarms / notification -----------------------------------------
    alarm_engine_enabled: bool = True
    notification_backends: str = "log"  # comma separated: log,email,push,voice
    #: Role -> recipient directories, one grammar for both:
    #: ``role=addr,addr;role=addr``. The roles are the ones escalation paths
    #: name (operator, on_call, maintainer). Email addresses for the email
    #: channel; topic names for the push channel.
    notify_email_recipients: str = ""
    notify_push_targets: str = ""
    #: Email goes through an SMTP relay reachable without internet access
    #: (SDD 5.1 local-first). ``security`` is starttls, tls or none.
    notify_smtp_host: str | None = None
    notify_smtp_port: int = 587
    notify_smtp_security: str = "starttls"
    notify_smtp_username: str | None = None
    notify_smtp_password: str | None = None
    notify_smtp_from: str | None = None
    notify_smtp_timeout_s: float = 10.0
    #: Push goes to a self-hosted, ntfy-compatible HTTP endpoint (one JSON POST
    #: per message; see chaos.alarms.notify.PushChannel for the exact shape).
    notify_push_url: str | None = None
    notify_push_token: str | None = None
    notify_push_timeout_s: float = 10.0
    #: A delivery that failed is retried on later alarm-engine cycles, at most
    #: this many times and no sooner than this many seconds apart. A failed
    #: row is never mistaken for a delivered one.
    notify_retry_limit: int = 3
    notify_retry_interval_s: int = 300

    # --- Backups and self-monitoring -----------------------------------
    #: Where ``chaos backup`` writes by default and where the backup-age
    #: monitor looks. Unset means ``<workspace>/var/backups`` -- the repository
    #: root in a checkout, ``/app/var/backups`` in the container (see
    #: :func:`workspace_root`). The Windows product's CLI shim sends backups to
    #: ``%ProgramData%``, so there this must name that directory.
    backup_directory: Path | None = None
    #: The asset whose ``backup_age_h`` point the monitor publishes. The
    #: shipped ``backup_overdue`` alarm watches the platform's database
    #: service, so that is the default.
    backup_age_asset_id: str = "it.application_service.rack_01.postgres_01"
    #: The registry asset that represents this platform instance. When MQTT is
    #: on, the bus registers a retained ``offline`` last will on that asset's
    #: availability topic and publishes ``online`` on connect, which is what
    #: lets the class-scoped ``service_unavailable`` alarm fire for the API
    #: itself. Unset: the primary's shipped asset; nothing on a secondary.
    platform_asset_id: str | None = None

    # --- Plugins --------------------------------------------------------
    # Third-party integrations (SDD 26.2 vendor bindings) load through
    # chaos.plugins. Enablement is explicit and cheap to reason about:
    # "*" means every plugin found, a comma-separated list means only those,
    # and the disabled list always wins -- so one line in the environment takes
    # a misbehaving vendor integration out of the platform without a rebuild.
    plugins_enabled: str = "*"
    plugins_disabled: str = ""
    #: JSON object keyed by plugin name, e.g. {"netbotz": {"host": "10.10.10.40"}}.
    #: Per-key environment variables (CHAOS_PLUGIN_<NAME>_<KEY>) override it,
    #: which is where credentials belong.
    plugin_options: str = ""
    #: The mirror engine turns plugin readings into canonical telemetry. It is
    #: a bus producer, so it is only useful where the bus is running.
    plugin_mirror_enabled: bool = True

    @field_validator(*OPTIONAL_STRING_SETTINGS, mode="before")
    @classmethod
    def _empty_means_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def is_secondary(self) -> bool:
        return self.node_role.lower() == "secondary"

    @property
    def notification_backend_list(self) -> list[str]:
        return [b.strip() for b in self.notification_backends.split(",") if b.strip()]

    @property
    def enabled_plugin_list(self) -> list[str]:
        """The plugin names this node will load, or ``["*"]`` for all of them."""
        return [p.strip().lower() for p in self.plugins_enabled.split(",") if p.strip()]

    @property
    def disabled_plugin_list(self) -> list[str]:
        return [p.strip().lower() for p in self.plugins_disabled.split(",") if p.strip()]

    @property
    def resolved_backup_directory(self) -> Path:
        """``backup_directory``, or the workspace default when it is unset."""
        if self.backup_directory:
            return Path(self.backup_directory).expanduser()
        return workspace_root(self) / "var" / "backups"

    @property
    def resolved_platform_asset_id(self) -> str | None:
        """The asset this instance publishes availability for, if any."""
        if self.platform_asset_id:
            return self.platform_asset_id
        return None if self.is_secondary else PRIMARY_PLATFORM_ASSET_ID


def workspace_root(settings: Settings | None = None) -> Path:
    """Best-effort location of the deployment working tree.

    In a source checkout this is the repository root. In the container the
    package is installed into a virtualenv, so ``ROOT_DIR`` points inside
    ``site-packages``; the data directory's parent is the reliable anchor
    because the image copies ``data/``, ``schemas/``, ``tools/`` and ``var/``
    side by side under ``/app``.
    """
    candidates: list[Path] = []
    data_dir = getattr(settings, "data_dir", None)
    if data_dir:
        parent = Path(data_dir).resolve().parent
        if parent.is_dir():
            candidates.append(parent)
    candidates.append(ROOT_DIR)
    candidates.append(Path.cwd())

    for candidate in candidates:
        if (candidate / "tools").is_dir() or (candidate / "var").is_dir():
            return candidate
    return candidates[0]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
