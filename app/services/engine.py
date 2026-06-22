"""Backup / restore engine — restic, DB dump/import, SSH tunnel, file restore.

Pure service layer extracted from app/main.py (keeps the HTTP layer under the
600-line cap). No FastAPI imports here; functions take/return plain values and
are called by the routes and the scheduler in app/main.py.

Public surface used by main.py:
  - append_log, get_log_file
  - get_restic_env, get_project_restic_env
  - run_app_backup(project) -> (ok, message)
  - run_restore(project, snapshot_id, scope) -> (ok, message)
"""
import os
import subprocess
import shutil
import tempfile
import socket
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app import backup_paths

RESTIC_REPO           = os.environ.get("RESTIC_REPO", "")
RESTIC_PASSWORD_FILE  = os.environ.get("RESTIC_PASSWORD_FILE", "/opt/backups/.restic-pass")
AWS_ACCESS_KEY_ID     = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def get_log_file(project: dict) -> str:
    return project.get("log_file") or f"/opt/backups/{project['restic_tag']}.log"


def append_log(log_file: str, message: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Restic environment
# ---------------------------------------------------------------------------

def get_restic_env():
    env = os.environ.copy()
    env["RESTIC_REPOSITORY"]     = RESTIC_REPO
    env["AWS_ACCESS_KEY_ID"]     = AWS_ACCESS_KEY_ID
    env["AWS_SECRET_ACCESS_KEY"] = AWS_SECRET_ACCESS_KEY
    # Prefer inline password env var; fall back to password file
    restic_password = os.environ.get("RESTIC_PASSWORD", "")
    if restic_password:
        env["RESTIC_PASSWORD"] = restic_password
        env.pop("RESTIC_PASSWORD_FILE", None)
    else:
        env["RESTIC_PASSWORD_FILE"] = RESTIC_PASSWORD_FILE
    return env


def get_project_restic_env(project: dict) -> dict:
    env = os.environ.copy()
    if project.get("storage_type") == "local":
        repo = project.get("local_repo_path") or f"/opt/backups/restic/{project['restic_tag']}"
        env["RESTIC_REPOSITORY"] = repo
        env.pop("AWS_ACCESS_KEY_ID", None)
        env.pop("AWS_SECRET_ACCESS_KEY", None)
    else:
        env["RESTIC_REPOSITORY"]     = RESTIC_REPO
        env["AWS_ACCESS_KEY_ID"]     = AWS_ACCESS_KEY_ID
        env["AWS_SECRET_ACCESS_KEY"] = AWS_SECRET_ACCESS_KEY
    restic_password = os.environ.get("RESTIC_PASSWORD", "")
    if restic_password:
        env["RESTIC_PASSWORD"] = restic_password
        env.pop("RESTIC_PASSWORD_FILE", None)
    else:
        env["RESTIC_PASSWORD_FILE"] = RESTIC_PASSWORD_FILE
    return env


def parse_connection_string(conn_str: str, engine: str) -> dict:
    parsed = urllib.parse.urlparse(conn_str)
    default_port = 5432 if engine == "postgres" else 3306
    return {
        "host":     parsed.hostname or "localhost",
        "port":     parsed.port or default_port,
        "user":     urllib.parse.unquote(parsed.username or ""),
        "password": urllib.parse.unquote(parsed.password or ""),
        "database": parsed.path.lstrip("/"),
    }


# ---------------------------------------------------------------------------
# SSH Tunnel
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("localhost", port), timeout=1):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.3)
    return False


@contextmanager
def maybe_ssh_tunnel(project: dict, db_host: str, db_port: int):
    """Open an SSH tunnel if ssh_host is configured, else yield original host/port."""
    if not project.get("ssh_host"):
        yield db_host, db_port
        return

    local_port = _find_free_port()
    cmd = [
        "ssh", "-N",
        "-L", f"{local_port}:{db_host}:{db_port}",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=10",
        "-o", "ExitOnForwardFailure=yes",
        "-p", str(project.get("ssh_port") or 22),
    ]
    if project.get("ssh_key"):
        cmd += ["-i", project["ssh_key"]]
    cmd.append(f"{project['ssh_user']}@{project['ssh_host']}")

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        if not _wait_for_port(local_port):
            proc.kill()
            stderr = proc.stderr.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"SSH tunnel to {project['ssh_host']} failed: {stderr}")
        yield "localhost", local_port
    finally:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# Dump / Import
# ---------------------------------------------------------------------------

def _dump_db(project: dict, dump_file: Path) -> tuple[bool, str]:
    engine = project["db_engine"]

    if project["connection_type"] == "connection_string":
        c = parse_connection_string(project["connection_string"], engine)
        try:
            with maybe_ssh_tunnel(project, c["host"], int(c["port"])) as (host, port):
                env = os.environ.copy()
                if engine == "postgres":
                    env["PGPASSWORD"] = c["password"]
                    cmd = ["pg_dump", "-h", host, "-p", str(port),
                           "-U", c["user"], "-d", c["database"], "-f", str(dump_file)]
                elif engine in ("mariadb", "mysql"):
                    env["MYSQL_PWD"] = c["password"]
                    cmd = ["mysqldump", "-h", host, f"-P{port}",
                           "-u", c["user"], c["database"], f"--result-file={dump_file}"]
                else:
                    return False, f"Unsupported engine: {engine}"
                r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
                if r.returncode != 0:
                    return False, r.stderr.strip()
        except RuntimeError as e:
            return False, str(e)

    else:  # docker mode
        container = project.get("docker_container", "")
        user      = project.get("db_user", "")
        password  = project.get("db_pass", "")
        db        = project.get("db_name", "")

        if engine == "postgres":
            cmd = ["docker", "exec", "-e", f"PGPASSWORD={password}",
                   container, "pg_dump", "-U", user, db]
        elif engine in ("mariadb", "mysql"):
            # MariaDB images ship mariadb-dump; the mysqldump symlink was dropped.
            dump_bin = "mariadb-dump" if engine == "mariadb" else "mysqldump"
            cmd = ["docker", "exec", container,
                   dump_bin, "-u", user, f"-p{password}", db]
        else:
            return False, f"Unsupported engine: {engine}"

        r = subprocess.run(cmd, capture_output=True, timeout=300)
        if r.returncode != 0:
            return False, r.stderr.decode("utf-8", errors="replace").strip()
        dump_file.write_bytes(r.stdout)

    return True, ""


def _import_db(project: dict, sql_file: Path) -> tuple[bool, str]:
    engine = project["db_engine"]

    if project["connection_type"] == "connection_string":
        c = parse_connection_string(project["connection_string"], engine)
        try:
            with maybe_ssh_tunnel(project, c["host"], int(c["port"])) as (host, port):
                env = os.environ.copy()
                if engine == "postgres":
                    env["PGPASSWORD"] = c["password"]
                    cmd = ["psql", "-h", host, "-p", str(port),
                           "-U", c["user"], "-d", c["database"], "-f", str(sql_file)]
                    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
                elif engine in ("mariadb", "mysql"):
                    env["MYSQL_PWD"] = c["password"]
                    cmd = ["mysql", "-h", host, f"-P{port}", "-u", c["user"], c["database"]]
                    with open(sql_file) as f:
                        r = subprocess.run(cmd, stdin=f, env=env,
                                           capture_output=True, text=True, timeout=300)
                else:
                    return False, f"Unsupported engine: {engine}"
                if r.returncode != 0:
                    return False, r.stderr.strip()
        except RuntimeError as e:
            return False, str(e)

    else:  # docker mode
        container = project.get("docker_container", "")
        user      = project.get("db_user", "")
        password  = project.get("db_pass", "")
        db        = project.get("db_name", "")

        if engine == "postgres":
            cmd = ["docker", "exec", "-i", "-e", f"PGPASSWORD={password}",
                   container, "psql", "-U", user, "-d", db]
        elif engine in ("mariadb", "mysql"):
            # MariaDB images ship the `mariadb` client; MySQL images ship `mysql`.
            cli_bin = "mariadb" if engine == "mariadb" else "mysql"
            cmd = ["docker", "exec", "-i", container,
                   cli_bin, "-u", user, f"-p{password}", db]
        else:
            return False, f"Unsupported engine: {engine}"

        with open(sql_file) as f:
            r = subprocess.run(cmd, stdin=f, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return False, r.stderr.strip()

    return True, ""


# ---------------------------------------------------------------------------
# Backup / Restore
# ---------------------------------------------------------------------------

def _wp_uploads_path(project: dict) -> str:
    """Resolve+validate the WordPress uploads volume path through the allow-list.

    Routes the legacy {wp_resource_id}_wp-uploads volume through
    backup_paths.resolve() so an operator-supplied wp_resource_id containing
    '..' cannot escape the Docker volumes root before a restore rsync --delete
    (security fix). Raises ValueError on an out-of-root id.
    """
    return backup_paths.resolve(
        {"source": "volume", "value": f"{project['wp_resource_id']}_wp-uploads"})


def run_app_backup(project: dict) -> tuple[bool, str]:
    log_file = get_log_file(project)
    tag      = project["restic_tag"]
    ts       = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp_dir  = Path(tempfile.mkdtemp(prefix="backup-"))
    lines    = []

    try:
        append_log(log_file, f"INFO: Starting backup for {project['name']}")
        dump_file = tmp_dir / f"{tag}-{ts}.sql"

        # Assemble the list of source paths restic will capture in one snapshot.
        sources: list[str] = []

        # 1) Optional DB dump — skip pg_dump/mysqldump entirely when off (AC6).
        if project.get("include_db", True):
            ok, err = _dump_db(project, dump_file)
            if not ok:
                append_log(log_file, f"ERROR: DB dump failed: {err}")
                return False, f"DB dump failed: {err}"
            size = dump_file.stat().st_size
            lines.append(f"DB dump complete ({size:,} bytes)")
            append_log(log_file, "INFO: DB dump complete")
            sources.append(str(dump_file))
        else:
            lines.append("DB dump skipped (include_db=false)")
            append_log(log_file, "INFO: DB dump skipped (include_db=false)")

        # 2) Configured file/directory sources (volume | host), allow-listed.
        for spec in project.get("backup_paths", []):
            spec_dict = spec if isinstance(spec, dict) else spec.dict()
            path = backup_paths.resolve(spec_dict)   # raises on escaped root
            if Path(path).exists():
                sources.append(path)
                lines.append(f"Including {spec_dict.get('source')}: {path}")
            else:
                append_log(log_file, f"WARN: path missing, skipped: {path}")
                lines.append(f"WARN: path missing, skipped: {path}")

        # 3) Legacy WordPress preset still works unchanged (AC11), now validated.
        if project["project_type"] == "wordpress" and project.get("wp_resource_id"):
            try:
                uploads = Path(_wp_uploads_path(project))
            except ValueError as e:
                append_log(log_file, f"WARN: WordPress uploads skipped, invalid resource id: {e}")
            else:
                if uploads.exists():
                    sources.append(str(uploads))
                    lines.append("Including WordPress uploads")

        # 4) Empty-source guard (AC6): never invoke restic with zero sources.
        if not sources:
            msg = ("Nothing to back up: include_db is off and no configured "
                   "paths exist. Add at least one path or enable the DB dump.")
            append_log(log_file, f"ERROR: {msg}")
            return False, msg

        restic_env = get_project_restic_env(project)

        # Auto-init local repo on first use
        if project.get("storage_type") == "local":
            repo_path = Path(project.get("local_repo_path") or f"/opt/backups/restic/{project['restic_tag']}")
            if not (repo_path / "config").exists():
                repo_path.mkdir(parents=True, exist_ok=True)
                r_init = subprocess.run(
                    ["restic", "init"], env=restic_env,
                    capture_output=True, text=True, timeout=30,
                )
                if r_init.returncode != 0:
                    append_log(log_file, f"ERROR: Failed to init local repo: {r_init.stderr.strip()}")
                    return False, f"Failed to init local repo: {r_init.stderr.strip()}"
                append_log(log_file, "INFO: Local restic repo initialised")

        # Build --exclude flags from the comma-separated excludes field (AC10).
        excludes = [e.strip() for e in (project.get("backup_excludes") or "").split(",") if e.strip()]
        exclude_flags = sum([["--exclude", e] for e in excludes], [])

        r = subprocess.run(
            ["restic", "backup"] + sources + exclude_flags + ["--tag", tag],
            env=restic_env, capture_output=True, text=True,
            timeout=int(os.environ.get("BACKUP_TIMEOUT", 1800)),   # AC12
        )
        if r.returncode != 0:
            append_log(log_file, f"ERROR: Restic backup failed: {r.stderr.strip()}")
            return False, f"Restic backup failed: {r.stderr.strip()}"

        lines.append("Restic backup complete.")
        append_log(log_file, "INFO: Restic backup complete")

        r = subprocess.run(
            ["restic", "forget", "--tag", tag,
             "--keep-daily",   str(project.get("keep_daily",   7)),
             "--keep-weekly",  str(project.get("keep_weekly",  4)),
             "--keep-monthly", str(project.get("keep_monthly", 6)),
             "--keep-yearly",  str(project.get("keep_yearly",  1)),
             "--prune"],
            env=restic_env, capture_output=True, text=True, timeout=300,
        )
        if r.returncode == 0:
            lines.append("Retention policy applied.")
            append_log(log_file, "INFO: Retention policy applied")
        else:
            lines.append(f"Warning: retention failed: {r.stderr.strip()}")

        return True, "\n".join(lines)

    except subprocess.TimeoutExpired:
        append_log(log_file, "ERROR: Backup timed out")
        return False, "Backup timed out"
    except Exception as e:
        append_log(log_file, f"ERROR: {e}")
        return False, str(e)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _rsync_into_target(restored_src: Path, target: str, log_file: str) -> tuple[bool, str]:
    """rsync a restored directory back over its live target with --delete.

    The caller MUST have re-validated ``target`` through backup_paths before
    invoking this so --delete cannot escape the allow-listed root (MUST-FIX #3).
    """
    Path(target).mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["rsync", "-a", "--delete", f"{restored_src}/", f"{target}/"],
        capture_output=True, text=True, timeout=int(os.environ.get("BACKUP_TIMEOUT", 1800)),
    )
    if r.returncode == 0:
        append_log(log_file, f"INFO: Files restored to {target}")
        return True, f"Files restored to {target}"
    return False, f"File restore error ({target}): {r.stderr.strip()}"


def _restore_files(project: dict, restore_dir: Path, log_file: str, lines: list) -> None:
    """Restore each configured path spec from restore_dir back to its target.

    restic preserves absolute path layout under --target, so a resolved path
    ``/p`` lands at ``restore_dir/p.lstrip('/')``. We re-resolve+validate every
    spec (MUST-FIX #3) before any --delete, then rsync if the data is present.

    Legacy WordPress (AC11): the uploads volume is restored via its explicit,
    allow-list-validated wp-uploads path — NOT a blind rglob('_data').
    """
    restored_any = False

    # New per-spec restores (volume | host), each mapped by its absolute layout.
    for spec in project.get("backup_paths", []):
        spec_dict = spec if isinstance(spec, dict) else spec.dict()
        target = backup_paths.resolve(spec_dict)        # re-validate (MUST-FIX #3)
        restored_src = restore_dir / target.lstrip("/")
        if not restored_src.exists():
            lines.append(f"WARN: no restored data for {target}, skipped")
            continue
        ok, msg = _rsync_into_target(restored_src, target, log_file)
        lines.append(msg)
        restored_any = True

    # Legacy WordPress preset: explicit, validated uploads path (security fix).
    if project["project_type"] == "wordpress" and project.get("wp_resource_id"):
        uploads_target = _wp_uploads_path(project)      # raises on escaped root
        restored_src = restore_dir / uploads_target.lstrip("/")
        if restored_src.exists():
            ok, msg = _rsync_into_target(restored_src, uploads_target, log_file)
            lines.append("WordPress uploads restored." if ok else msg)
            restored_any = True
        else:
            lines.append("No uploads directory found in snapshot.")

    if not restored_any and not project.get("backup_paths"):
        lines.append("No file paths configured to restore.")


def run_restore(project: dict, snapshot_id: str, scope: str = "all") -> tuple[bool, str]:
    """Restore a snapshot. scope: 'all' | 'db' | 'files'.

    'db' fully bypasses the rsync/--delete block (AC8); 'files' never imports
    the DB (AC7).
    """
    log_file    = get_log_file(project)
    restore_dir = Path(tempfile.mkdtemp(prefix="restore-"))
    lines       = []

    try:
        r = subprocess.run(
            ["restic", "restore", snapshot_id, "--target", str(restore_dir)],
            env=get_project_restic_env(project), capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            return False, f"Restic restore failed: {r.stderr}"

        # --- Database (only for scope all|db, and only if this project has a DB)
        if scope in ("all", "db") and project.get("include_db", True):
            sql_files = list(restore_dir.rglob("*.sql"))
            if not sql_files:
                lines.append("No SQL dump found in snapshot.")
            else:
                ok, err = _import_db(project, sql_files[0])
                if not ok:
                    lines.append(f"DB restore error: {err}")
                else:
                    lines.append("Database restored successfully.")
                    append_log(log_file, f"INFO: Restored DB from snapshot {snapshot_id}")
        elif scope == "db":
            lines.append("DB restore skipped (include_db=false).")

        # --- Files (only for scope all|files) — fully bypassed for db-only (AC8)
        if scope in ("all", "files"):
            _restore_files(project, restore_dir, log_file, lines)

        return True, "\n".join(lines) if lines else "Nothing restored for this scope."

    except subprocess.TimeoutExpired:
        return False, "Restore timed out"
    except ValueError as e:
        # Raised by backup_paths.resolve() if a target escapes the root.
        append_log(log_file, f"ERROR: restore aborted, invalid path: {e}")
        return False, f"Restore aborted, invalid path: {e}"
    except Exception as e:
        return False, str(e)
    finally:
        shutil.rmtree(restore_dir, ignore_errors=True)
