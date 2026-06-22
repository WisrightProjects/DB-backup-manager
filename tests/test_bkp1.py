"""BKP-1 acceptance tests — file & directory backups.

Each test class names the AC it verifies. Tests target the *behaviour* the AC
describes, not implementation detail.

Portability note: backup_paths.resolve() runs candidate paths through
os.path.realpath() + os.sep, so on Windows "/srv/app/config" resolves to
"D:\\srv\\app\\config". The accept/reject *decision* (the actual security
contract under test) is identical on Windows and Linux, so the tests assert
structurally (path segments / under-root membership / ValueError raised)
rather than against a hard-coded POSIX string. That keeps them green on the
Windows dev box AND inside the Linux container.
"""
import json
import os
import sqlite3
import tempfile

import pytest

from app import backup_paths
from app import main
from app.services import engine   # backup/restore engine extracted from main

# Keep test-generated logs / scratch repos out of the repo tree.
_SCRATCH = tempfile.gettempdir()


def _scratch(name: str) -> str:
    return os.path.join(_SCRATCH, "bkp1-" + name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _under_root(resolved: str, root: str) -> bool:
    """True if `resolved` is the realpath'd root or sits beneath it."""
    real_root = os.path.realpath(root)
    return resolved == real_root or resolved.startswith(real_root + os.sep)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point main.DB_PATH at a fresh sqlite file with the full schema."""
    db_file = tmp_path / "projects.db"
    monkeypatch.setattr(main, "DB_PATH", str(db_file))
    main.init_db()
    return str(db_file)


def _base_payload(**overrides):
    """A minimal valid ProjectPayload kwargs dict; override per test."""
    data = dict(
        name="Test Project",
        db_engine="postgres",
        project_type="database",
        connection_type="connection_string",
        connection_string="postgresql://u:p@localhost:5432/db",
        restic_tag="test-tag",
        storage_type="local",
    )
    data.update(overrides)
    return data


# ===========================================================================
# AC3: Backup includes a Docker volume
#   resolve({source:volume, value:media_vol}) -> .../media_vol/_data
# ===========================================================================
class TestAC3VolumeResolution:
    def test_volume_resolves_to_data_dir_under_volume_root(self):
        resolved = backup_paths.resolve({"source": "volume", "value": "media_vol"})
        # Must terminate in <name>/_data and stay under the docker volumes root.
        assert resolved.endswith(os.path.join("media_vol", "_data"))
        assert _under_root(resolved, backup_paths.VOLUME_ROOT)

    def test_volume_value_cannot_escape_volume_root(self):
        # review MUST-FIX #3 / risk #7: value "../../etc" must NOT escape.
        with pytest.raises(ValueError):
            backup_paths.resolve({"source": "volume", "value": "../../etc"})


# ===========================================================================
# AC4: Backup includes a host directory under the root
#   HOST_BACKUP_ROOT=/srv + {source:host, value:/srv/app/config} -> accepted
# ===========================================================================
class TestAC4HostUnderRoot:
    def test_host_path_under_root_is_accepted(self, monkeypatch):
        monkeypatch.setattr(backup_paths, "HOST_BACKUP_ROOT", "/srv")
        resolved = backup_paths.resolve({"source": "host", "value": "/srv/app/config"})
        assert _under_root(resolved, "/srv")
        assert resolved.endswith(os.path.join("app", "config"))

    def test_host_root_itself_is_accepted(self, monkeypatch):
        monkeypatch.setattr(backup_paths, "HOST_BACKUP_ROOT", "/srv")
        resolved = backup_paths.resolve({"source": "host", "value": "/srv"})
        assert resolved == os.path.realpath("/srv")


# ===========================================================================
# AC5: Host path outside the root is rejected (save-time, server-side)
# ===========================================================================
class TestAC5RejectOutsideRoot:
    def test_absolute_path_outside_root_rejected(self, monkeypatch):
        monkeypatch.setattr(backup_paths, "HOST_BACKUP_ROOT", "/srv")
        with pytest.raises(ValueError):
            backup_paths.resolve({"source": "host", "value": "/etc/shadow"})

    def test_traversal_out_of_root_rejected(self, monkeypatch):
        # "/srv/../etc" normalises to /etc -> must be rejected.
        monkeypatch.setattr(backup_paths, "HOST_BACKUP_ROOT", "/srv")
        with pytest.raises(ValueError):
            backup_paths.resolve({"source": "host", "value": "/srv/../etc"})

    def test_unknown_source_rejected(self):
        with pytest.raises(ValueError):
            backup_paths.resolve({"source": "ftp", "value": "/srv/x"})

    def test_validate_raises_on_first_bad_spec(self, monkeypatch):
        monkeypatch.setattr(backup_paths, "HOST_BACKUP_ROOT", "/srv")
        specs = [
            {"source": "host", "value": "/srv/ok"},
            {"source": "host", "value": "/etc/passwd"},
        ]
        with pytest.raises(ValueError):
            backup_paths.validate(specs)

    def test_create_endpoint_rejects_bad_path_with_400(self, fresh_db, monkeypatch):
        monkeypatch.setattr(backup_paths, "HOST_BACKUP_ROOT", "/srv")
        payload = main.ProjectPayload(
            **_base_payload(backup_paths=[{"source": "host", "value": "/etc/shadow"}])
        )
        with pytest.raises(main.HTTPException) as exc:
            main.create_project(payload, user="admin")
        assert exc.value.status_code == 400
        # Nothing should have been persisted.
        conn = sqlite3.connect(fresh_db)
        count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        conn.close()
        assert count == 0

    def test_update_endpoint_rejects_bad_path_with_400(self, fresh_db, monkeypatch):
        monkeypatch.setattr(backup_paths, "HOST_BACKUP_ROOT", "/srv")
        # First create a valid project so update has a target.
        good = main.ProjectPayload(**_base_payload(name="Good"))
        created = main.create_project(good, user="admin")
        pid = created["id"]
        # Now try to update it with an out-of-root host path.
        bad = main.ProjectPayload(
            **_base_payload(name="Good",
                            backup_paths=[{"source": "host", "value": "/srv/../etc"}])
        )
        with pytest.raises(main.HTTPException) as exc:
            main.update_project(pid, bad, user="admin")
        assert exc.value.status_code == 400


# ===========================================================================
# AC1 / AC2 (data-layer slice): files-only + db+paths projects persist correctly
# (Full UI behaviour is manual; this verifies the save contract the form posts.)
# ===========================================================================
class TestAC1AC2Persistence:
    def test_files_only_project_persists_include_db_false_and_one_path(self, fresh_db, monkeypatch):
        monkeypatch.setattr(backup_paths, "HOST_BACKUP_ROOT", "/srv")
        payload = main.ProjectPayload(**_base_payload(
            name="FilesOnly",
            include_db=False,
            backup_paths=[{"source": "volume", "value": "media_vol"}],
        ))
        created = main.create_project(payload, user="admin")
        proj = main.get_project_or_404(created["id"])
        assert proj["include_db"] is False
        assert proj["backup_paths"] == [{"source": "volume", "value": "media_vol"}]

    def test_db_plus_two_paths_persists(self, fresh_db, monkeypatch):
        monkeypatch.setattr(backup_paths, "HOST_BACKUP_ROOT", "/srv")
        payload = main.ProjectPayload(**_base_payload(
            name="DbPlus",
            include_db=True,
            backup_paths=[
                {"source": "host", "value": "/srv/app/config"},
                {"source": "volume", "value": "media_vol"},
            ],
        ))
        created = main.create_project(payload, user="admin")
        proj = main.get_project_or_404(created["id"])
        assert proj["include_db"] is True
        assert len(proj["backup_paths"]) == 2


# ===========================================================================
# AC6: include_db=false skips the dump; empty sources never call restic
# ===========================================================================
class TestAC6SkipDumpAndEmptyGuard:
    def test_empty_sources_returns_guard_without_invoking_restic(self, monkeypatch):
        """include_db off + no paths + non-WP => guard fires, no subprocess."""
        called = {"subprocess": False, "dump": False}

        def fake_run(*a, **k):
            called["subprocess"] = True
            raise AssertionError("subprocess.run must not be reached")

        def fake_dump(*a, **k):
            called["dump"] = True
            return True, ""

        monkeypatch.setattr(main.subprocess, "run", fake_run)
        monkeypatch.setattr(engine, "_dump_db", fake_dump)

        project = {
            "name": "EmptyProj", "restic_tag": "empty", "project_type": "database",
            "include_db": False, "backup_paths": [], "storage_type": "local",
            "log_file": _scratch("_ac6.log"),
        }
        ok, msg = engine.run_app_backup(project)
        assert ok is False
        assert "Nothing to back up" in msg
        assert called["dump"] is False        # dump skipped (AC6)
        assert called["subprocess"] is False  # restic never invoked

    def test_include_db_false_does_not_call_dump_db(self, monkeypatch):
        """With one (existing) path and include_db off, _dump_db is never called
        and the restic argv contains no .sql dump file."""
        captured = {}
        dump_called = {"v": False}

        def fake_run(cmd, *a, **k):
            captured["cmd"] = cmd

            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

        def fake_dump(*a, **k):
            dump_called["v"] = True
            return True, ""

        monkeypatch.setattr(main.subprocess, "run", fake_run)
        monkeypatch.setattr(engine, "_dump_db", fake_dump)
        # Resolve to an existing dir so the path is included as a source.
        monkeypatch.setattr(backup_paths, "resolve", lambda spec: os.path.dirname(__file__))

        project = {
            "name": "NoDb", "restic_tag": "nodb", "project_type": "database",
            "include_db": False,
            "backup_paths": [{"source": "host", "value": "/srv/whatever"}],
            "storage_type": "local",
            "local_repo_path": _scratch("_repo_nodb"),
            "log_file": _scratch("_ac6b.log"),
        }
        # Pretend the local repo is already initialised so init isn't attempted.
        monkeypatch.setattr(main.Path, "exists", lambda self: True)
        ok, msg = engine.run_app_backup(project)
        assert dump_called["v"] is False
        # restic backup command must not include any .sql file as a source.
        assert "cmd" in captured
        assert not any(str(x).endswith(".sql") for x in captured["cmd"])


# ===========================================================================
# AC7 / AC8: restore scope dispatch
#   files -> rsync runs, DB import is NOT called
#   db    -> DB import runs, file restore is NOT called
# ===========================================================================
class TestAC7AC8RestoreScope:
    def _project(self):
        return {
            "name": "ScopeProj", "restic_tag": "scope", "project_type": "database",
            "include_db": True,
            "backup_paths": [{"source": "host", "value": "/srv/app/config"}],
            "storage_type": "local",
            "log_file": _scratch("_scope.log"),
        }

    def _patch_restic_restore_ok(self, monkeypatch):
        def fake_run(cmd, *a, **k):
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        monkeypatch.setattr(main.subprocess, "run", fake_run)

    def test_files_scope_runs_file_restore_not_db_import(self, monkeypatch):
        self._patch_restic_restore_ok(monkeypatch)
        calls = {"db": False, "files": False}
        monkeypatch.setattr(engine, "_import_db",
                            lambda *a, **k: (calls.__setitem__("db", True), (True, ""))[1])
        monkeypatch.setattr(engine, "_restore_files",
                            lambda *a, **k: calls.__setitem__("files", True))

        ok, _ = engine.run_restore(self._project(), "snap123", scope="files")
        assert ok is True
        assert calls["files"] is True      # AC7: files restored
        assert calls["db"] is False        # AC7: DB NOT imported

    def test_db_scope_runs_db_import_not_file_restore(self, monkeypatch):
        # restic restore + an .sql file present in the restore dir.
        def fake_run(cmd, *a, **k):
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        monkeypatch.setattr(main.subprocess, "run", fake_run)

        # Make rglob find a fake .sql so the db branch reaches _import_db.
        from pathlib import Path as _P
        monkeypatch.setattr(main.Path, "rglob",
                            lambda self, pat: iter([_P("dump.sql")]) if pat == "*.sql" else iter([]))

        calls = {"db": False, "files": False}
        monkeypatch.setattr(engine, "_import_db",
                            lambda *a, **k: (calls.__setitem__("db", True), (True, ""))[1])
        monkeypatch.setattr(engine, "_restore_files",
                            lambda *a, **k: calls.__setitem__("files", True))

        ok, _ = engine.run_restore(self._project(), "snap123", scope="db")
        assert ok is True
        assert calls["db"] is True         # AC8: DB imported
        assert calls["files"] is False     # AC8: files NOT written back

    def test_invalid_scope_rejected_at_route(self, fresh_db, monkeypatch):
        # Route guards scope; bad scope -> 400 before any restore runs.
        monkeypatch.setattr(backup_paths, "HOST_BACKUP_ROOT", "/srv")
        payload = main.ProjectPayload(**_base_payload(name="RouteScope"))
        created = main.create_project(payload, user="admin")
        body = main.RestorePayload(scope="wipe-everything")
        with pytest.raises(main.HTTPException) as exc:
            main.restore_snapshot(created["id"], "snap1", body=body, scope=None, user="admin")
        assert exc.value.status_code == 400


# ===========================================================================
# AC10: Excludes are honored -> restic invoked with matching --exclude flags
# ===========================================================================
class TestAC10Excludes:
    def test_excludes_become_exclude_flags(self, monkeypatch):
        captured = {}

        def fake_run(cmd, *a, **k):
            # The first restic call we care about is "restic backup ...".
            if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "backup":
                captured["cmd"] = cmd

            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

        monkeypatch.setattr(main.subprocess, "run", fake_run)
        monkeypatch.setattr(engine, "_dump_db", lambda *a, **k: (True, ""))
        monkeypatch.setattr(backup_paths, "resolve", lambda spec: os.path.dirname(__file__))
        monkeypatch.setattr(main.Path, "exists", lambda self: True)
        # _dump_db is faked so the dump file won't exist; stub stat() for size log.
        import types
        monkeypatch.setattr(main.Path, "stat",
                            lambda self: types.SimpleNamespace(st_size=123))

        project = {
            "name": "ExclProj", "restic_tag": "excl", "project_type": "database",
            "include_db": True,
            "backup_paths": [{"source": "host", "value": "/srv/app"}],
            "backup_excludes": "node_modules,*.log",
            "storage_type": "local",
            "local_repo_path": _scratch("_repo_excl"),
            "log_file": _scratch("_ac10.log"),
        }
        ok, msg = engine.run_app_backup(project)
        cmd = captured.get("cmd")
        assert cmd is not None, f"restic backup never invoked; msg={msg}"
        # Both excludes present as --exclude <pattern> pairs.
        for pat in ("node_modules", "*.log"):
            assert "--exclude" in cmd
            assert pat in cmd
            idx = cmd.index(pat)
            assert cmd[idx - 1] == "--exclude"

    def test_no_excludes_means_no_exclude_flag(self, monkeypatch):
        captured = {}

        def fake_run(cmd, *a, **k):
            if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "backup":
                captured["cmd"] = cmd

            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

        monkeypatch.setattr(main.subprocess, "run", fake_run)
        monkeypatch.setattr(engine, "_dump_db", lambda *a, **k: (True, ""))
        monkeypatch.setattr(backup_paths, "resolve", lambda spec: os.path.dirname(__file__))
        monkeypatch.setattr(main.Path, "exists", lambda self: True)
        import types
        monkeypatch.setattr(main.Path, "stat",
                            lambda self: types.SimpleNamespace(st_size=10))

        project = {
            "name": "NoExcl", "restic_tag": "noexcl", "project_type": "database",
            "include_db": True,
            "backup_paths": [{"source": "host", "value": "/srv/app"}],
            "backup_excludes": "",
            "storage_type": "local",
            "local_repo_path": _scratch("_repo_noexcl"),
            "log_file": _scratch("_ac10b.log"),
        }
        engine.run_app_backup(project)
        cmd = captured.get("cmd")
        assert cmd is not None
        assert "--exclude" not in cmd


# ===========================================================================
# AC11: Backward compatibility — a legacy row missing the new columns hydrates
# to include_db=True / backup_paths=[] with no error.
# ===========================================================================
class TestAC11BackwardCompat:
    def test_legacy_row_without_new_columns_hydrates_defaults(self):
        legacy = {
            "id": "legacy-1", "name": "Legacy WP", "db_engine": "mariadb",
            "project_type": "wordpress", "connection_type": "docker",
            "restic_tag": "legacy", "wp_resource_id": "abc123",
            # No backup_paths / include_db / backup_excludes keys at all.
        }
        proj = main._hydrate_project(legacy)
        assert proj["include_db"] is True          # defaults DB-on (AC11)
        assert proj["backup_paths"] == []          # defaults to empty list

    def test_raw_json_string_is_parsed_to_list(self):
        row = {
            "id": "x", "name": "X", "db_engine": "postgres",
            "project_type": "database", "connection_type": "connection_string",
            "restic_tag": "x",
            "backup_paths": json.dumps([{"source": "volume", "value": "v1"}]),
            "include_db": 1,
        }
        proj = main._hydrate_project(row)
        assert proj["backup_paths"] == [{"source": "volume", "value": "v1"}]
        assert proj["include_db"] is True

    def test_include_db_zero_coerces_to_false(self):
        row = {
            "id": "y", "name": "Y", "db_engine": "postgres",
            "project_type": "database", "connection_type": "connection_string",
            "restic_tag": "y", "backup_paths": "[]", "include_db": 0,
        }
        proj = main._hydrate_project(row)
        assert proj["include_db"] is False
