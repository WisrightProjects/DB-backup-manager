import os
import json
import subprocess
import shutil
import secrets
import sqlite3
import re
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from starlette.status import HTTP_401_UNAUTHORIZED
from pydantic import BaseModel

from app import backup_paths
from app.services import engine, notify

app = FastAPI(title="Backup Manager")
security = HTTPBasic()

# Serve the dashboard's CSS/JS. Unauthenticated by design (no secrets in assets);
# the API and the dashboard page itself stay behind basic auth.
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

AUTH_USERNAME        = os.environ.get("AUTH_USERNAME", "admin")
AUTH_PASSWORD        = os.environ.get("AUTH_PASSWORD", "changeme")
DB_PATH              = os.environ.get("DB_PATH", "/opt/backups/backup-manager.db")
TIMEZONE             = os.environ.get("TIMEZONE", "UTC")
# Restic/AWS/SSH config + the backup-restore engine now live in app/services/engine.py.

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
            schedule_cron    TEXT DEFAULT '',
            created_at       TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migrate existing DBs that predate added columns
    for migration in [
        "ALTER TABLE projects ADD COLUMN schedule_cron TEXT DEFAULT ''",
        "ALTER TABLE projects ADD COLUMN storage_type TEXT DEFAULT 's3'",
        "ALTER TABLE projects ADD COLUMN local_repo_path TEXT",
        # BKP-1: file/directory backups + optional DB dump
        "ALTER TABLE projects ADD COLUMN backup_paths TEXT DEFAULT '[]'",
        "ALTER TABLE projects ADD COLUMN include_db INTEGER DEFAULT 1",
        "ALTER TABLE projects ADD COLUMN backup_excludes TEXT DEFAULT ''",
        # Per-project email recipients for backup success/failure notifications
        "ALTER TABLE projects ADD COLUMN notify_email TEXT DEFAULT ''",
    ]:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

scheduler = BackgroundScheduler(timezone=TIMEZONE)


def _backup_job(project_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    conn.close()
    if not row:
        return
    # Decode backup_paths/include_db here too: the scheduler builds the project
    # dict directly from a raw row and does NOT go through get_project_or_404,
    # so without this it would iterate the raw JSON string (review MUST-FIX #1).
    project = _hydrate_project(row)
    success, output = engine.run_app_backup(project)
    engine.append_log(engine.get_log_file(project),
               f"INFO: Scheduled backup {'succeeded' if success else 'FAILED'}: {output[:200]}")
    notify.send_backup_result(project, success, output)


def _sync_schedule(project: dict):
    job_id = f"backup_{project['id']}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    cron = (project.get("schedule_cron") or "").strip()
    if cron:
        try:
            scheduler.add_job(
                _backup_job,
                CronTrigger.from_crontab(cron, timezone=TIMEZONE),
                id=job_id,
                args=[project["id"]],
                replace_existing=True,
            )
        except Exception as e:
            engine.append_log(engine.get_log_file(project), f"ERROR: Invalid cron expression '{cron}': {e}")


def _load_all_schedules():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM projects WHERE schedule_cron IS NOT NULL AND schedule_cron != ''"
    ).fetchall()
    conn.close()
    for row in rows:
        _sync_schedule(dict(row))


scheduler.start()
_load_all_schedules()


# ---------------------------------------------------------------------------
# Pydantic
# ---------------------------------------------------------------------------

class PathSpec(BaseModel):
    source: str            # "volume" | "host"
    value:  str


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
    schedule_cron:     Optional[str] = ""
    storage_type:      str = "s3"
    local_repo_path:   Optional[str] = None
    # BKP-1: file/directory sources, optional DB dump, restic excludes
    backup_paths:      list[PathSpec] = []
    include_db:        bool = True
    backup_excludes:   Optional[str] = ""
    # Comma-separated recipients for success/failure emails (per-project)
    notify_email:      Optional[str] = ""


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


def _hydrate_project(row) -> dict:
    """Turn a raw projects row into a dict with backup_paths/include_db decoded.

    Input : a sqlite3.Row (or dict) straight from the projects table.
    Output: a plain dict where ``backup_paths`` is a list[dict] (parsed JSON,
            never a raw string) and ``include_db`` is coerced to bool.

    Shared by every read path — both get_project_or_404 (HTTP routes) and
    _backup_job (scheduler), so scheduled backups never iterate the raw
    backup_paths JSON string character-by-character (review MUST-FIX #1).
    """
    project = dict(row)
    raw_paths = project.get("backup_paths")
    if isinstance(raw_paths, str):
        try:
            project["backup_paths"] = json.loads(raw_paths or "[]")
        except (ValueError, TypeError):
            project["backup_paths"] = []
    elif raw_paths is None:
        project["backup_paths"] = []
    # SQLite stores include_db as 0/1; older rows may lack it entirely (DB-on).
    project["include_db"] = bool(project.get("include_db", 1))
    return project


def get_project_or_404(project_id: str) -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return _hydrate_project(row)


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
# Routes — Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(user: str = Depends(verify_auth)):
    html_path = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Routes — Projects CRUD
# ---------------------------------------------------------------------------

@app.get("/api/projects")
def list_projects(user: str = Depends(verify_auth)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
    conn.close()
    projects = []
    for r in rows:
        job = scheduler.get_job(f"backup_{r['id']}")
        next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
        projects.append({
            "id":              r["id"],
            "name":            r["name"],
            "type":            r["project_type"],
            "db_engine":       r["db_engine"],
            "connection_type": r["connection_type"],
            "tag":             r["restic_tag"],
            "ssh":             bool(r["ssh_host"]),
            "schedule_cron":   r["schedule_cron"] or "",
            "next_run":        next_run,
            "storage_type":    r["storage_type"],
        })
    return {"projects": projects}


@app.post("/api/projects", status_code=201)
def create_project(body: ProjectPayload, user: str = Depends(verify_auth)):
    # Server-side, save-time validation of backup paths (AC5, MUST-FIX #2).
    path_specs = [p.dict() for p in body.backup_paths]
    try:
        backup_paths.validate(path_specs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    project_id = generate_project_id(body.name)
    conn = get_db()
    conn.execute(
        """INSERT INTO projects
           (id,name,db_engine,project_type,connection_type,connection_string,
            docker_container,db_user,db_pass,db_name,
            ssh_host,ssh_port,ssh_user,ssh_key,
            keep_daily,keep_weekly,keep_monthly,keep_yearly,
            restic_tag,backup_script,log_file,wp_resource_id,schedule_cron,
            storage_type,local_repo_path,backup_paths,include_db,backup_excludes,notify_email)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (project_id, body.name, body.db_engine, body.project_type, body.connection_type,
         body.connection_string, body.docker_container, body.db_user, body.db_pass, body.db_name,
         body.ssh_host, body.ssh_port, body.ssh_user, body.ssh_key,
         body.keep_daily, body.keep_weekly, body.keep_monthly, body.keep_yearly,
         body.restic_tag, body.backup_script, body.log_file, body.wp_resource_id,
         body.schedule_cron or "", body.storage_type, body.local_repo_path,
         json.dumps(path_specs), int(body.include_db), body.backup_excludes or "",
         body.notify_email or ""),
    )
    conn.commit()
    conn.close()
    project = get_project_or_404(project_id)
    _sync_schedule(project)
    return {"id": project_id, "name": body.name}


@app.get("/api/projects/{project_id}")
def get_project_detail(project_id: str, user: str = Depends(verify_auth)):
    return get_project_or_404(project_id)


@app.put("/api/projects/{project_id}")
def update_project(project_id: str, body: ProjectPayload, user: str = Depends(verify_auth)):
    existing = get_project_or_404(project_id)
    # Server-side, save-time validation of backup paths (AC5, MUST-FIX #2).
    path_specs = [p.dict() for p in body.backup_paths]
    try:
        backup_paths.validate(path_specs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Keep existing password when not provided (edit form leaves it blank)
    db_pass = body.db_pass if body.db_pass else existing.get("db_pass")
    conn = get_db()
    conn.execute(
        """UPDATE projects SET
           name=?,db_engine=?,project_type=?,connection_type=?,connection_string=?,
           docker_container=?,db_user=?,db_pass=?,db_name=?,
           ssh_host=?,ssh_port=?,ssh_user=?,ssh_key=?,
           keep_daily=?,keep_weekly=?,keep_monthly=?,keep_yearly=?,
           restic_tag=?,backup_script=?,log_file=?,wp_resource_id=?,schedule_cron=?,
           storage_type=?,local_repo_path=?,backup_paths=?,include_db=?,backup_excludes=?,
           notify_email=?
           WHERE id=?""",
        (body.name, body.db_engine, body.project_type, body.connection_type, body.connection_string,
         body.docker_container, body.db_user, db_pass, body.db_name,
         body.ssh_host, body.ssh_port, body.ssh_user, body.ssh_key,
         body.keep_daily, body.keep_weekly, body.keep_monthly, body.keep_yearly,
         body.restic_tag, body.backup_script, body.log_file, body.wp_resource_id,
         body.schedule_cron or "", body.storage_type, body.local_repo_path,
         json.dumps(path_specs), int(body.include_db), body.backup_excludes or "",
         body.notify_email or "",
         project_id),
    )
    conn.commit()
    conn.close()
    project = get_project_or_404(project_id)
    _sync_schedule(project)
    return {"id": project_id, "name": body.name}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str, user: str = Depends(verify_auth)):
    get_project_or_404(project_id)
    job_id = f"backup_{project_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
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
            env=engine.get_project_restic_env(project), capture_output=True, text=True, timeout=30,
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
            success, output = r.returncode == 0, r.stdout + r.stderr
        else:
            success, output = engine.run_app_backup(project)
        notify.send_backup_result(project, success, output)
        return {"success": success, "output": output}
    except subprocess.TimeoutExpired:
        notify.send_backup_result(project, False, "Backup timed out (5 min limit)")
        return JSONResponse({"error": "Backup timed out (5 min limit)"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/projects/{project_id}/snapshots/{snapshot_id}/prepare-download")
def prepare_download(project_id: str, snapshot_id: str, user: str = Depends(verify_auth)):
    project = get_project_or_404(project_id)
    restore_dir  = DOWNLOAD_DIR / snapshot_id
    archive_path = DOWNLOAD_DIR / f"{snapshot_id}.tar.gz"

    if archive_path.exists():
        return {"ready": True, "size": archive_path.stat().st_size, "filename": f"{snapshot_id}.tar.gz"}
    if restore_dir.exists():
        shutil.rmtree(restore_dir)

    try:
        r = subprocess.run(
            ["restic", "restore", snapshot_id, "--target", str(restore_dir)],
            env=engine.get_project_restic_env(project), capture_output=True, text=True, timeout=600,
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


class RestorePayload(BaseModel):
    scope: str = "all"   # "all" | "db" | "files"


@app.post("/api/projects/{project_id}/snapshots/{snapshot_id}/restore")
def restore_snapshot(
    project_id: str,
    snapshot_id: str,
    body: Optional[RestorePayload] = None,
    scope: Optional[str] = Query(default=None),
    user: str = Depends(verify_auth),
):
    project = get_project_or_404(project_id)
    # Scope may arrive as a query param or in the JSON body; default to "all".
    chosen = scope or (body.scope if body else None) or "all"
    if chosen not in ("all", "db", "files"):
        raise HTTPException(status_code=400, detail=f"Invalid restore scope: {chosen}")
    success, output = engine.run_restore(project, snapshot_id, scope=chosen)
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
        log_path = Path(engine.get_log_file(project))
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
