import os
import json
import subprocess
import shutil
import tempfile
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.status import HTTP_401_UNAUTHORIZED

app = FastAPI(title="Backup Manager")
security = HTTPBasic()

# Config from environment
RESTIC_REPO = os.environ.get("RESTIC_REPO", "s3:https://eu2.contabostorage.com/wisright-backups")
RESTIC_PASSWORD_FILE = os.environ.get("RESTIC_PASSWORD_FILE", "/opt/backups/.restic-pass")
BACKUP_SCRIPT = os.environ.get("BACKUP_SCRIPT", "/opt/backups/backup-prod.sh")
BACKUP_LOG = os.environ.get("BACKUP_LOG", "/opt/backups/backup.log")
AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "changeme")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")

DOWNLOAD_DIR = Path("/tmp/backup-downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)


def get_restic_env():
    env = os.environ.copy()
    env["RESTIC_REPOSITORY"] = RESTIC_REPO
    env["RESTIC_PASSWORD_FILE"] = RESTIC_PASSWORD_FILE
    env["AWS_ACCESS_KEY_ID"] = AWS_ACCESS_KEY_ID
    env["AWS_SECRET_ACCESS_KEY"] = AWS_SECRET_ACCESS_KEY
    return env


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


@app.get("/api/snapshots")
def list_snapshots(user: str = Depends(verify_auth)):
    try:
        result = subprocess.run(
            ["restic", "snapshots", "--json"],
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
        return {"snapshots": formatted}
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "Timeout connecting to backup repository"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/snapshots/{snapshot_id}/stats")
def snapshot_stats(snapshot_id: str, user: str = Depends(verify_auth)):
    try:
        result = subprocess.run(
            ["restic", "stats", snapshot_id, "--json"],
            env=get_restic_env(),
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return JSONResponse({"error": result.stderr.strip()}, status_code=500)
        return json.loads(result.stdout)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/backup")
def trigger_backup(user: str = Depends(verify_auth)):
    try:
        result = subprocess.run(
            ["bash", BACKUP_SCRIPT],
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


@app.post("/api/snapshots/{snapshot_id}/prepare-download")
def prepare_download(snapshot_id: str, user: str = Depends(verify_auth)):
    """Restore snapshot to temp dir and create a tar.gz for download."""
    restore_dir = DOWNLOAD_DIR / snapshot_id
    archive_path = DOWNLOAD_DIR / f"{snapshot_id}.tar.gz"

    # If archive already exists, skip restore
    if archive_path.exists():
        size = archive_path.stat().st_size
        return {"ready": True, "size": size, "filename": f"{snapshot_id}.tar.gz"}

    # Clean any partial restore
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

        # Create tar.gz
        subprocess.run(
            ["tar", "-czf", str(archive_path), "-C", str(restore_dir), "."],
            capture_output=True, text=True, timeout=600,
        )

        # Clean up restore dir
        shutil.rmtree(restore_dir, ignore_errors=True)

        size = archive_path.stat().st_size
        return {"ready": True, "size": size, "filename": f"{snapshot_id}.tar.gz"}
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "Download preparation timed out"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/snapshots/{snapshot_id}/download")
def download_snapshot(snapshot_id: str, user: str = Depends(verify_auth)):
    archive_path = DOWNLOAD_DIR / f"{snapshot_id}.tar.gz"
    if not archive_path.exists():
        raise HTTPException(status_code=404, detail="Archive not ready. Call prepare-download first.")
    return FileResponse(
        path=str(archive_path),
        filename=f"backup-{snapshot_id}.tar.gz",
        media_type="application/gzip",
    )


@app.post("/api/snapshots/{snapshot_id}/restore")
def restore_snapshot(snapshot_id: str, user: str = Depends(verify_auth)):
    """Restore DB and uploads from a snapshot."""
    restore_dir = Path(tempfile.mkdtemp(prefix="restore-"))

    try:
        # Step 1: Restore snapshot
        result = subprocess.run(
            ["restic", "restore", snapshot_id, "--target", str(restore_dir)],
            env=get_restic_env(),
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            return JSONResponse({"error": f"Restore failed: {result.stderr}"}, status_code=500)

        output_lines = []

        # Step 2: Find and import SQL dump
        sql_files = list(restore_dir.rglob("drive-ev-db.*.sql"))
        if sql_files:
            sql_file = sql_files[0]
            # Read DB credentials from backup script
            db_container = os.environ.get("DB_CONTAINER_NAME", "ac0k8s488okksoo4cggo4wc4")
            db_user = os.environ.get("DB_USER", "mariadb")
            db_pass = os.environ.get("DB_PASS", "")
            db_name = os.environ.get("DB_NAME", "driveev_prod")

            import_cmd = f"docker exec -i {db_container} mariadb -u {db_user} -p{db_pass} {db_name}"
            with open(sql_file, "r") as f:
                result = subprocess.run(
                    import_cmd.split(),
                    stdin=f,
                    capture_output=True, text=True, timeout=120,
                )
            if result.returncode == 0:
                output_lines.append("Database restored successfully.")
            else:
                output_lines.append(f"Database restore error: {result.stderr}")
        else:
            output_lines.append("No SQL dump found in snapshot.")

        # Step 3: Restore uploads
        wp_resource_id = os.environ.get("WP_RESOURCE_ID", "j4gcw00koks488wkscs448kc")
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


@app.get("/api/logs")
def get_logs(lines: int = 100, user: str = Depends(verify_auth)):
    try:
        log_path = Path(BACKUP_LOG)
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
