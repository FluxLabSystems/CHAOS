"""The backup endpoints and the module they share with ``chaos backup``.

Two properties matter here. The archive the API writes must be the archive the
command line writes -- same manifest, same table set, restorable by
``chaos restore`` -- because an operator who takes a backup from the console on
Monday and restores it from a laptop on Friday is relying on that. And the
listing must say what is actually in the directory, including a file that is
not a backup at all, because the nightly job's output is what the weekly check
reads.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from chaos import cli
from chaos.backup import backup_directory, list_archives, read_manifest, run_backup
from chaos.config import Settings
from chaos.models.commands import AuditLogEntry
from chaos.models.registry import Asset, AssetClass


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A throwaway workspace root, so no test writes into the repository's var/."""
    root = tmp_path / "work"
    (root / "data").mkdir(parents=True)
    (root / "var").mkdir()
    (root / "data" / "note.yaml").write_text("schema_version: test\n", encoding="utf-8")
    return root


@pytest.fixture()
def settings(workspace: Path) -> Settings:
    return Settings(
        database_url="sqlite://",
        mqtt_enabled=False,
        ems_enabled=False,
        alarm_engine_enabled=False,
        allow_physical_control=False,
        node_role="primary",
        data_dir=workspace / "data",
        schema_dir=workspace / "schemas-absent",
    )


@pytest.fixture()
def maintainer_headers() -> dict[str, str]:
    return {"X-Operator": "test.maintainer", "X-Operator-Role": "maintainer"}


@pytest.fixture()
def one_asset(db_session):
    db_session.add(AssetClass(name="pump", allowed_domains=["water"]))
    db_session.add(
        Asset(
            asset_id="water.pump.well.01",
            domain="water",
            asset_class="pump",
            name="Well pump",
            status="planned",
        )
    )
    db_session.commit()


# ------------------------------------------------------------------ module --


def test_the_backup_directory_is_the_cli_default(settings, workspace):
    assert backup_directory(settings) == workspace / "var" / "backups"


def test_run_backup_writes_an_archive_chaos_restore_accepts(settings, session_factory, one_asset):
    with session_factory() as session:
        result = run_backup(settings, session=session)

    assert result.path.parent == backup_directory(settings)
    assert result.path.name.startswith("homestead-primary-") and result.path.name.endswith(".tar.gz")
    assert result.size_bytes == result.path.stat().st_size > 0
    assert result.row_counts["assets"] == 1
    assert result.tables == len(result.row_counts) > 0
    assert result.rows >= 1
    assert result.includes_history is False
    assert "telemetry_samples" not in result.row_counts

    # The strict reader behind `chaos restore` accepts it unchanged.
    bundle = cli.read_backup_archive(result.path)
    assert bundle.manifest["kind"] == cli.BACKUP_KIND
    assert bundle.manifest["row_counts"] == result.row_counts
    assert len(bundle.tables["assets"]) == 1

    with tarfile.open(result.path) as tar:
        names = tar.getnames()
    assert names[0] == "manifest.json", "the manifest must be the first member so listings stay cheap"
    assert "RESTORE.txt" in names
    assert "settings.json" in names
    assert "design-package/data/note.yaml" in names


def test_run_backup_can_include_history(settings, session_factory):
    with session_factory() as session:
        result = run_backup(settings, include_history=True, session=session)
    assert result.includes_history is True
    assert "telemetry_samples" in result.row_counts
    assert "ingest_dead_letters" in result.row_counts


def test_run_backup_accepts_a_directory_or_a_file(settings, session_factory, tmp_path):
    target_dir = tmp_path / "offsite"
    target_dir.mkdir()
    with session_factory() as session:
        in_dir = run_backup(settings, target_dir, session=session)
        as_file = run_backup(settings, tmp_path / "named.tar.gz", session=session)
    assert in_dir.path.parent == target_dir
    assert as_file.path == tmp_path / "named.tar.gz"


def test_read_manifest_reads_only_what_it_needs(settings, session_factory):
    with session_factory() as session:
        result = run_backup(settings, session=session)
    manifest = read_manifest(result.path)
    assert manifest["created_at"] == result.created_at.isoformat()
    assert manifest["row_counts"] == result.row_counts


def test_listing_is_newest_first_and_admits_a_file_that_is_not_a_backup(settings, session_factory, tmp_path):
    directory = tmp_path / "archives"
    directory.mkdir()
    import datetime as dt

    with session_factory() as session:
        older = run_backup(settings, directory, session=session, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
        newer = run_backup(settings, directory, session=session, now=dt.datetime(2026, 2, 1, tzinfo=dt.UTC))
    (directory / "stray.tar.gz").write_bytes(b"this is not a tar archive")
    (directory / "notes.txt").write_text("ignored", encoding="utf-8")

    entries = list_archives(directory)
    # Real archives lead, by the time they say they were taken; the stray file
    # is listed last rather than hidden, so the weekly check sees it.
    assert [e["name"] for e in entries] == [newer.path.name, older.path.name, "stray.tar.gz"]
    stray = entries[-1]
    assert stray["manifest"] is None
    assert "not a readable backup archive" in stray["error"]
    assert all(e["name"] != "notes.txt" for e in entries)
    assert entries[0]["manifest"]["restorable"] is True
    assert entries[0]["manifest"]["tables"] == newer.tables
    assert entries[0]["manifest"]["rows"] == newer.rows


def test_listing_a_missing_directory_is_empty_not_an_error(tmp_path):
    assert list_archives(tmp_path / "nowhere") == []


# --------------------------------------------------------------------- API --


def test_post_backup_requires_a_maintainer(client, operator_headers):
    assert client.post("/api/v1/backup", json={}).status_code == 403
    assert client.post("/api/v1/backup", json={}, headers=operator_headers).status_code == 403


def test_post_backup_writes_the_same_archive_the_cli_writes(
    client, settings, db_session, one_asset, maintainer_headers
):
    response = client.post("/api/v1/backup", json={"include_history": False}, headers=maintainer_headers)
    assert response.status_code == 201, response.text
    payload = response.json()

    path = Path(payload["path"])
    assert path.is_file()
    assert path.parent == backup_directory(settings)
    assert payload["size_bytes"] == path.stat().st_size
    assert payload["tables"] == len(payload["row_counts"]) > 0
    assert payload["rows"] == sum(payload["row_counts"].values())
    assert payload["row_counts"]["assets"] == 1
    assert payload["includes_history"] is False
    assert payload["written_by"] == "test.maintainer"
    assert payload["created_at"]

    bundle = cli.read_backup_archive(path)
    assert bundle.manifest["kind"] == cli.BACKUP_KIND
    assert bundle.manifest["row_counts"] == payload["row_counts"]
    with tarfile.open(path) as tar:
        settings_blob = json.loads(tar.extractfile("settings.json").read().decode())
    assert settings_blob["database_url"].startswith("sqlite:")

    # The write is audited against the named maintainer.
    entry = db_session.query(AuditLogEntry).filter(AuditLogEntry.action == "backup.create").one()
    assert entry.actor == "test.maintainer"
    assert entry.target_id == path.name
    assert entry.detail["sha256"] == payload["sha256"]


def test_post_backup_with_history(client, maintainer_headers):
    response = client.post("/api/v1/backup", json={"include_history": True}, headers=maintainer_headers)
    assert response.status_code == 201, response.text
    assert response.json()["includes_history"] is True
    assert "telemetry_samples" in response.json()["row_counts"]


def test_post_backup_rejects_unknown_fields(client, maintainer_headers):
    response = client.post("/api/v1/backup", json={"output": "/etc/passwd"}, headers=maintainer_headers)
    assert response.status_code == 422


def test_get_backup_lists_what_post_wrote(client, settings, maintainer_headers):
    empty = client.get("/api/v1/backup").json()
    assert empty["directory"] == str(backup_directory(settings))
    assert empty["exists"] is False
    assert empty["count"] == 0 and empty["archives"] == []

    first = client.post("/api/v1/backup", json={}, headers=maintainer_headers).json()
    listing = client.get("/api/v1/backup").json()
    assert listing["exists"] is True
    assert listing["count"] == 1
    entry = listing["archives"][0]
    assert entry["name"] == first["name"]
    assert entry["size_bytes"] == first["size_bytes"]
    assert entry["error"] is None
    assert entry["manifest"]["created_at"] == first["created_at"]
    assert entry["manifest"]["node_role"] == "primary"
    assert entry["manifest"]["tables"] == first["tables"]
    assert entry["manifest"]["rows"] == first["rows"]
    assert entry["manifest"]["restorable"] is True


def test_get_backup_is_readable_by_a_viewer(client):
    assert client.get("/api/v1/backup").status_code == 200


def test_post_backup_reports_an_unwritable_directory(client, settings, maintainer_headers):
    # A file where the backup directory should be: the write cannot succeed and
    # must say so rather than answering 201 with nothing on disk.
    directory = backup_directory(settings)
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.write_text("in the way", encoding="utf-8")
    response = client.post("/api/v1/backup", json={}, headers=maintainer_headers)
    assert response.status_code == 507
    assert "Could not" in response.json()["detail"]
