import os
import json
import subprocess
import shutil
import tempfile
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.status import HTTP_401_UNAUTHORIZED

app = FastAPI(title="Backup Manager")
security = HTTPBasic()

# Config from environment
RESTIC_REPO = os.environ.get("RESTIC_REPO", "s3:https://eu2.contabostorage.com/wisright-backups")
RESTIC_PASSWORD_FILE = os.environ.get("RESTIC_PASSWORD_FILE", "/opt/backups/.restic-pass")
AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "changeme")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")

# Project definitions
PROJECTS = {
    "wordpress": {
        "name": "Drive EV WordPress",
        "tag": "drive-ev",
        "type": "wordpress",
        "backup_script": "/opt/backups/backup-prod.sh",
        "log_file": "/opt/backups/backup.log",
        "db_container": os.environ.get("DB_CONTAINER_NAME", "ac0k8s488okksoo4cggo4wc4"),
        "db_user": os.environ.get("DB_USER", "mariadb"),
        "db_pass": os.environ.get("DB_PASS", ""),
        "db_name": os.environ.get("DB_NAME", "driveev_prod"),
        "db_engine": "mariadb",
        "wp_resource_id": os.environ.get("WP_RESOURCE_ID", "j4gcw00koks488wkscs448kc"),
        "dump_pattern": "drive-ev-db.*.sql",
    },
    "postgres": {
        "name": "Drive EV Lead DB",
        "tag": "drive-ev-lead",
        "type": "postgres",
        "backup_script": "/opt/backups/backup-postgres.sh",
        "log_file": "/opt/backups/backup-postgres.log",
        "db_container": os.environ.get("PG_CONTAINER", "nckow088o4kss4ksw8k0ck80"),
        "db_user": os.environ.get("PG_USER", "postgres"),
        "db_pass": os.environ.get("PG_PASS", "NQgAJ0s0XXYSuLqoNjx5k80ck6AdV6VX46PH3AHIhJagDITpbGFTjMudHYHV8Z2L"),
        "db_name": os.environ.get("PG_DB", "postgres"),
        "db_engine": "postgres",
        "dump_pattern": "drive-ev-lead-db.*.sql",
    },
}

DOWNLOAD_DIR = Path("/tmp/backup-downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)


def get_restic_env():
    env = os.environ.copy()
    env["RESTIC_REPOSITORY"] = RESTIC_REPO
    env["RESTIC_PASSWORD_FILE"] = RESTIC_PASSWORD_FILE
    env["AWS_ACCESS_KEY_ID"] = AWS_ACCESS_KEY_ID
    env["AWS_SECRET_ACCESS_KEY"] = AWS_SECRET_ACCESS_KEY
    return env


def get_project(project_id: str):
    if project_id not in PROJECTS:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return PROJECTS[project_id]


def verify_auth(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = secrets.compare_digest(credentials.username, AUTH_USERNAME)
    correct_pass = secrets.compare_digest(credentials.password, AUTH_PASSWORD)
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@app.get("/", response_class=HTMLResponse)
def dashboard(user: str = Depends(verify_auth)):
    html_path = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(content=html_path.read_text())


@app.get("/api/projects")
def list_projects(user: str = Depends(verify_auth)):
    return {
        "projects": [
            {"id": pid, "name": p["name"], "type": p["type"], "tag": p["tag"]}
            for pid, p in PROJECTS.items()
        ]
    }


@app.get("/api/projects/{project_id}/snapshots")
def list_snapshots(project_id: str, user: str = Depends(verify_auth)):
    project = get_project(project_id)
    try:
        result = subprocess.run(
            ["restic", "snapshots", "--json", "--tag", project["tag"]],
            env=get_restic_env(),
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return JSONResponse({"error": result.stderr.strip()}, status_code=500)

        snapshots = json.loads(result.stdout) if result.stdout.strip() else []
        formatted = []
        for s in snapshots:
            formatted.append({
                "id": s["short_id"],
                "full_id": s["id"],
                "time": s["time"],
                "hostname": s.get("hostname", ""),
                "tags": s.get("tags", []),
                "paths": s.get("paths", []),
            })
        formatted.sort(key=lambda x: x["time"], reverse=True)
        return {"snapshots": formatted, "project": project["name"]}
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "Timeout connecting to backup repository"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/projects/{project_id}/backup")
def trigger_backup(project_id: str, user: str = Depends(verify_auth)):
    project = get_project(project_id)
    try:
        result = subprocess.run(
            ["bash", project["backup_script"]],
            capture_output=True, text=True, timeout=300,
        )
        return {
            "success": result.returncode == 0,
            "output": result.stdout + result.stderr,
        }
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "Backup timed out (5 min limit)"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/projects/{project_id}/snapshots/{snapshot_id}/prepare-download")
def prepare_download(project_id: str, snapshot_id: str, user: str = Depends(verify_auth)):
    get_project(project_id)
    restore_dir = DOWNLOAD_DIR / snapshot_id
    archive_path = DOWNLOAD_DIR / f"{snapshot_id}.tar.gz"

    if archive_path.exists():
        size = archive_path.stat().st_size
        return {"ready": True, "size": size, "filename": f"{snapshot_id}.tar.gz"}

    if restore_dir.exists():
        shutil.rmtree(restore_dir)

    try:
        result = subprocess.run(
            ["restic", "restore", snapshot_id, "--target", str(restore_dir)],
            env=get_restic_env(),
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            return JSONResponse({"error": result.stderr.strip()}, status_code=500)

        subprocess.run(
            ["tar", "-czf", str(archive_path), "-C", str(restore_dir), "."],
            capture_output=True, text=True, timeout=600,
        )
        shutil.rmtree(restore_dir, ignore_errors=True)

        size = archive_path.stat().st_size
        return {"ready": True, "size": size, "filename": f"{snapshot_id}.tar.gz"}
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "Download preparation timed out"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/projects/{project_id}/snapshots/{snapshot_id}/download")
def download_snapshot(project_id: str, snapshot_id: str, user: str = Depends(verify_auth)):
    get_project(project_id)
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
    project = get_project(project_id)
    restore_dir = Path(tempfile.mkdtemp(prefix="restore-"))

    try:
        result = subprocess.run(
            ["restic", "restore", snapshot_id, "--target", str(restore_dir)],
            env=get_restic_env(),
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            return JSONResponse({"error": f"Restore failed: {result.stderr}"}, status_code=500)

        output_lines = []

        # Find SQL dump
        sql_files = list(restore_dir.rglob(project["dump_pattern"]))
        if sql_files:
            sql_file = sql_files[0]

            if project["db_engine"] == "mariadb":
                import_cmd = [
                    "docker", "exec", "-i", project["db_container"],
                    "mariadb", "-u", project["db_user"],
                    f"-p{project['db_pass']}", project["db_name"],
                ]
            elif project["db_engine"] == "postgres":
                import_cmd = [
                    "docker", "exec", "-i",
                    "-e", f"PGPASSWORD={project['db_pass']}",
                    project["db_container"],
                    "psql", "-U", project["db_user"], "-d", project["db_name"],
                ]
            else:
                output_lines.append(f"Unknown DB engine: {project['db_engine']}")
                return {"success": False, "output": "\n".join(output_lines)}

            with open(sql_file, "r") as f:
                result = subprocess.run(
                    import_cmd, stdin=f,
                    capture_output=True, text=True, timeout=120,
                )
            if result.returncode == 0:
                output_lines.append("Database restored successfully.")
            else:
                output_lines.append(f"Database restore error: {result.stderr}")
        else:
            output_lines.append("No SQL dump found in snapshot.")

        # Restore uploads (WordPress only)
        if project["type"] == "wordpress":
            wp_resource_id = project.get("wp_resource_id", "")
            uploads_target = Path(f"/var/lib/docker/volumes/{wp_resource_id}_wp-uploads/_data")
            restored_uploads = list(restore_dir.rglob("_data"))

            if restored_uploads:
                uploads_source = restored_uploads[0]
                result = subprocess.run(
                    ["rsync", "-a", "--delete", f"{uploads_source}/", f"{uploads_target}/"],
                    capture_output=True, text=True, timeout=300,
                )
                if result.returncode == 0:
                    output_lines.append("WordPress uploads restored successfully.")
                else:
                    output_lines.append(f"Uploads restore error: {result.stderr}")
            else:
                output_lines.append("No uploads directory found in snapshot.")

        return {"success": True, "output": "\n".join(output_lines)}

    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "Restore timed out"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        shutil.rmtree(restore_dir, ignore_errors=True)


@app.get("/api/projects/{project_id}/logs")
def get_logs(project_id: str, lines: int = 100, user: str = Depends(verify_auth)):
    project = get_project(project_id)
    try:
        log_path = Path(project["log_file"])
        if not log_path.exists():
            return {"logs": "No log file found."}
        content = log_path.read_text()
        log_lines = content.strip().split("\n")
        return {"logs": "\n".join(log_lines[-lines:])}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/downloads/{snapshot_id}")
def cleanup_download(snapshot_id: str, user: str = Depends(verify_auth)):
    archive_path = DOWNLOAD_DIR / f"{snapshot_id}.tar.gz"
    if archive_path.exists():
        archive_path.unlink()
    return {"cleaned": True}
