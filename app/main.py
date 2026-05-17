import os
import json
import subprocess
import shutil
import tempfile
import secrets
import sqlite3
import re
import socket
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.status import HTTP_401_UNAUTHORIZED
from pydantic import BaseModel

app = FastAPI(title="Backup Manager")
security = HTTPBasic()

RESTIC_REPO          = os.environ.get("RESTIC_REPO", "")
RESTIC_PASSWORD_FILE = os.environ.get("RESTIC_PASSWORD_FILE", "/opt/backups/.restic-pass")
AUTH_USERNAME        = os.environ.get("AUTH_USERNAME", "admin")
AUTH_PASSWORD        = os.environ.get("AUTH_PASSWORD", "changeme")
AWS_ACCESS_KEY_ID    = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
DB_PATH              = os.environ.get("DB_PATH", "/opt/backups/backup-manager.db")

DOWNLOAD_DIR = Path("/tmp/backup-downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id               TEXT PRIMARY KEY,
            name             TEXT NOT NULL,
            db_engine        TEXT NOT NULL,
            project_type     TEXT NOT NULL DEFAULT 'database',
            connection_type  TEXT NOT NULL,
            connection_string TEXT,
            docker_container TEXT,
            db_user          TEXT,
            db_pass          TEXT,
            db_name          TEXT,
            ssh_host         TEXT,
            ssh_port         INTEGER DEFAULT 22,
            ssh_user         TEXT,
            ssh_key          TEXT,
            keep_daily       INTEGER DEFAULT 7,
            keep_weekly      INTEGER DEFAULT 4,
            keep_monthly     INTEGER DEFAULT 6,
            keep_yearly      INTEGER DEFAULT 1,
            restic_tag       TEXT NOT NULL,
            backup_script    TEXT,
            log_file         TEXT,
            wp_resource_id   TEXT,
            created_at       TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------------------------
# Pydantic
# ---------------------------------------------------------------------------

class ProjectPayload(BaseModel):
    name:              str
    db_engine:         str
    project_type:      str = "database"
    connection_type:   str
    connection_string: Optional[str] = None
    docker_container:  Optional[str] = None
    db_user:           Optional[str] = None
    db_pass:           Optional[str] = None
    db_name:           Optional[str] = None
    ssh_host:          Optional[str] = None
    ssh_port:          Optional[int] = 22
    ssh_user:          Optional[str] = None
    ssh_key:           Optional[str] = None
    keep_daily:        int = 7
    keep_weekly:       int = 4
    keep_monthly:      int = 6
    keep_yearly:       int = 1
    restic_tag:        str
    backup_script:     Optional[str] = None
    log_file:          Optional[str] = None
    wp_resource_id:    Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"


def generate_project_id(name: str) -> str:
    conn = get_db()
    existing = {r["id"] for r in conn.execute("SELECT id FROM projects").fetchall()}
    conn.close()
    base = slugify(name)
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def get_restic_env():
    env = os.environ.copy()
    env["RESTIC_REPOSITORY"]    = RESTIC_REPO
    env["RESTIC_PASSWORD_FILE"] = RESTIC_PASSWORD_FILE
    env["AWS_ACCESS_KEY_ID"]    = AWS_ACCESS_KEY_ID
    env["AWS_SECRET_ACCESS_KEY"] = AWS_SECRET_ACCESS_KEY
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


def get_project_or_404(project_id: str) -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return dict(row)


def verify_auth(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(credentials.username, AUTH_USERNAME)
    ok_pass = secrets.compare_digest(credentials.password, AUTH_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


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
            cmd = ["docker", "exec", container,
                   "mysqldump", "-u", user, f"-p{password}", db]
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
            cmd = ["docker", "exec", "-i", container,
                   "mariadb", "-u", user, f"-p{password}", db]
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

def run_app_backup(project: dict) -> tuple[bool, str]:
    log_file = get_log_file(project)
    tag      = project["restic_tag"]
    ts       = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmp_dir  = Path(tempfile.mkdtemp(prefix="backup-"))
    lines    = []

    try:
        append_log(log_file, f"INFO: Starting backup for {project['name']}")
        dump_file = tmp_dir / f"{tag}-{ts}.sql"

        ok, err = _dump_db(project, dump_file)
        if not ok:
            append_log(log_file, f"ERROR: DB dump failed: {err}")
            return False, f"DB dump failed: {err}"

        size = dump_file.stat().st_size
        lines.append(f"DB dump complete ({size:,} bytes)")
        append_log(log_file, "INFO: DB dump complete")

        backup_paths = [str(dump_file)]
        if project["project_type"] == "wordpress" and project.get("wp_resource_id"):
            uploads = Path(f"/var/lib/docker/volumes/{project['wp_resource_id']}_wp-uploads/_data")
            if uploads.exists():
                backup_paths.append(str(uploads))
                lines.append(f"Including WordPress uploads")

        r = subprocess.run(
            ["restic", "backup"] + backup_paths + ["--tag", tag],
            env=get_restic_env(), capture_output=True, text=True, timeout=600,
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
            env=get_restic_env(), capture_output=True, text=True, timeout=300,
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


def run_restore(project: dict, snapshot_id: str) -> tuple[bool, str]:
    log_file    = get_log_file(project)
    restore_dir = Path(tempfile.mkdtemp(prefix="restore-"))
    lines       = []

    try:
        r = subprocess.run(
            ["restic", "restore", snapshot_id, "--target", str(restore_dir)],
            env=get_restic_env(), capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            return False, f"Restic restore failed: {r.stderr}"

        sql_files = list(restore_dir.rglob("*.sql"))
        if not sql_files:
            return False, "No SQL dump found in snapshot."

        ok, err = _import_db(project, sql_files[0])
        if not ok:
            lines.append(f"DB restore error: {err}")
        else:
            lines.append("Database restored successfully.")
            append_log(log_file, f"INFO: Restored snapshot {snapshot_id}")

        if project["project_type"] == "wordpress" and project.get("wp_resource_id"):
            uploads_target  = Path(f"/var/lib/docker/volumes/{project['wp_resource_id']}_wp-uploads/_data")
            restored_uploads = list(restore_dir.rglob("_data"))
            if restored_uploads:
                r = subprocess.run(
                    ["rsync", "-a", "--delete",
                     f"{restored_uploads[0]}/", f"{uploads_target}/"],
                    capture_output=True, text=True, timeout=300,
                )
                lines.append(
                    "WordPress uploads restored." if r.returncode == 0
                    else f"Uploads restore error: {r.stderr}"
                )
            else:
                lines.append("No uploads directory found in snapshot.")

        return True, "\n".join(lines)

    except subprocess.TimeoutExpired:
        return False, "Restore timed out"
    except Exception as e:
        return False, str(e)
    finally:
        shutil.rmtree(restore_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(user: str = Depends(verify_auth)):
    html_path = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(content=html_path.read_text())


# ---------------------------------------------------------------------------
# Routes — Projects CRUD
# ---------------------------------------------------------------------------

@app.get("/api/projects")
def list_projects(user: str = Depends(verify_auth)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
    conn.close()
    return {
        "projects": [
            {
                "id":              r["id"],
                "name":            r["name"],
                "type":            r["project_type"],
                "db_engine":       r["db_engine"],
                "connection_type": r["connection_type"],
                "tag":             r["restic_tag"],
                "ssh":             bool(r["ssh_host"]),
            }
            for r in rows
        ]
    }


@app.post("/api/projects", status_code=201)
def create_project(body: ProjectPayload, user: str = Depends(verify_auth)):
    project_id = generate_project_id(body.name)
    conn = get_db()
    conn.execute(
        """INSERT INTO projects
           (id,name,db_engine,project_type,connection_type,connection_string,
            docker_container,db_user,db_pass,db_name,
            ssh_host,ssh_port,ssh_user,ssh_key,
            keep_daily,keep_weekly,keep_monthly,keep_yearly,
            restic_tag,backup_script,log_file,wp_resource_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (project_id, body.name, body.db_engine, body.project_type, body.connection_type,
         body.connection_string, body.docker_container, body.db_user, body.db_pass, body.db_name,
         body.ssh_host, body.ssh_port, body.ssh_user, body.ssh_key,
         body.keep_daily, body.keep_weekly, body.keep_monthly, body.keep_yearly,
         body.restic_tag, body.backup_script, body.log_file, body.wp_resource_id),
    )
    conn.commit()
    conn.close()
    return {"id": project_id, "name": body.name}


@app.get("/api/projects/{project_id}")
def get_project_detail(project_id: str, user: str = Depends(verify_auth)):
    return get_project_or_404(project_id)


@app.put("/api/projects/{project_id}")
def update_project(project_id: str, body: ProjectPayload, user: str = Depends(verify_auth)):
    existing = get_project_or_404(project_id)
    # Keep existing password when not provided (edit form leaves it blank)
    db_pass = body.db_pass if body.db_pass else existing.get("db_pass")
    conn = get_db()
    conn.execute(
        """UPDATE projects SET
           name=?,db_engine=?,project_type=?,connection_type=?,connection_string=?,
           docker_container=?,db_user=?,db_pass=?,db_name=?,
           ssh_host=?,ssh_port=?,ssh_user=?,ssh_key=?,
           keep_daily=?,keep_weekly=?,keep_monthly=?,keep_yearly=?,
           restic_tag=?,backup_script=?,log_file=?,wp_resource_id=?
           WHERE id=?""",
        (body.name, body.db_engine, body.project_type, body.connection_type, body.connection_string,
         body.docker_container, body.db_user, db_pass, body.db_name,
         body.ssh_host, body.ssh_port, body.ssh_user, body.ssh_key,
         body.keep_daily, body.keep_weekly, body.keep_monthly, body.keep_yearly,
         body.restic_tag, body.backup_script, body.log_file, body.wp_resource_id,
         project_id),
    )
    conn.commit()
    conn.close()
    return {"id": project_id, "name": body.name}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str, user: str = Depends(verify_auth)):
    get_project_or_404(project_id)
    conn = get_db()
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    conn.close()
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Routes — Snapshots / Backup / Restore / Logs
# ---------------------------------------------------------------------------

@app.get("/api/projects/{project_id}/snapshots")
def list_snapshots(project_id: str, user: str = Depends(verify_auth)):
    project = get_project_or_404(project_id)
    try:
        r = subprocess.run(
            ["restic", "snapshots", "--json", "--tag", project["restic_tag"]],
            env=get_restic_env(), capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return JSONResponse({"error": r.stderr.strip()}, status_code=500)

        snapshots = json.loads(r.stdout) if r.stdout.strip() else []
        formatted = sorted(
            [{"id": s["short_id"], "full_id": s["id"], "time": s["time"],
              "hostname": s.get("hostname", ""), "tags": s.get("tags", []),
              "paths": s.get("paths", [])} for s in snapshots],
            key=lambda x: x["time"], reverse=True,
        )
        return {"snapshots": formatted, "project": project["name"]}
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "Timeout connecting to backup repository"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/projects/{project_id}/backup")
def trigger_backup(project_id: str, user: str = Depends(verify_auth)):
    project = get_project_or_404(project_id)
    try:
        if project.get("backup_script"):
            r = subprocess.run(
                ["bash", project["backup_script"]],
                capture_output=True, text=True, timeout=300,
            )
            return {"success": r.returncode == 0, "output": r.stdout + r.stderr}
        success, output = run_app_backup(project)
        return {"success": success, "output": output}
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "Backup timed out (5 min limit)"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/projects/{project_id}/snapshots/{snapshot_id}/prepare-download")
def prepare_download(project_id: str, snapshot_id: str, user: str = Depends(verify_auth)):
    get_project_or_404(project_id)
    restore_dir  = DOWNLOAD_DIR / snapshot_id
    archive_path = DOWNLOAD_DIR / f"{snapshot_id}.tar.gz"

    if archive_path.exists():
        return {"ready": True, "size": archive_path.stat().st_size, "filename": f"{snapshot_id}.tar.gz"}
    if restore_dir.exists():
        shutil.rmtree(restore_dir)

    try:
        r = subprocess.run(
            ["restic", "restore", snapshot_id, "--target", str(restore_dir)],
            env=get_restic_env(), capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            return JSONResponse({"error": r.stderr.strip()}, status_code=500)

        subprocess.run(
            ["tar", "-czf", str(archive_path), "-C", str(restore_dir), "."],
            capture_output=True, text=True, timeout=600,
        )
        shutil.rmtree(restore_dir, ignore_errors=True)
        return {"ready": True, "size": archive_path.stat().st_size, "filename": f"{snapshot_id}.tar.gz"}
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "Download preparation timed out"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/projects/{project_id}/snapshots/{snapshot_id}/download")
def download_snapshot(project_id: str, snapshot_id: str, user: str = Depends(verify_auth)):
    get_project_or_404(project_id)
    archive_path = DOWNLOAD_DIR / f"{snapshot_id}.tar.gz"
    if not archive_path.exists():
        raise HTTPException(status_code=404, detail="Archive not ready. Call prepare-download first.")
    return FileResponse(
        path=str(archive_path),
        filename=f"backup-{project_id}-{snapshot_id}.tar.gz",
        media_type="application/gzip",
    )


@app.post("/api/projects/{project_id}/snapshots/{snapshot_id}/restore")
def restore_snapshot(project_id: str, snapshot_id: str, user: str = Depends(verify_auth)):
    project = get_project_or_404(project_id)
    success, output = run_restore(project, snapshot_id)
    if success:
        return {"success": True, "output": output}
    return JSONResponse({"error": output}, status_code=500)


@app.get("/api/projects/{project_id}/logs")
def get_logs(
    project_id: str,
    lines: int = Query(default=100, ge=1, le=2000),
    user: str = Depends(verify_auth),
):
    project = get_project_or_404(project_id)
    try:
        log_path = Path(get_log_file(project))
        if not log_path.exists():
            return {"logs": "No log file found."}
        log_lines = log_path.read_text().strip().split("\n")
        return {"logs": "\n".join(log_lines[-lines:])}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/downloads/{snapshot_id}")
def cleanup_download(snapshot_id: str, user: str = Depends(verify_auth)):
    archive_path = DOWNLOAD_DIR / f"{snapshot_id}.tar.gz"
    if archive_path.exists():
        archive_path.unlink()
    return {"cleaned": True}
