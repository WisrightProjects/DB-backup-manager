# User Story: File & Directory Backups — Back up volumes and host paths alongside (or instead of) databases

**Story ID:** BKP-1
**Epic:** Backup coverage beyond databases
**Feature:** Generalize a project from "DB dump (+ WordPress uploads)" into "optional DB dump + a list of file paths" captured in one restic snapshot.
**Priority:** P1 (High)
**Effort:** 3 days (23 hours)
**Sprint:** Phase 2 — Generalized backup engine
**Status:** Ready for Development
**Depends On:** None

> **Locked decisions** (recommended defaults — change here if you disagree):
> 1. **Host mount:** a single parent root `HOST_BACKUP_ROOT` (default `/srv`) bind-mounted **1:1** (same path inside the container) read-write; host paths must resolve under it (allow-list).
> 2. **Restore granularity:** restore scope of `all` / `db` / `files`.
> 3. **Destructive file restore:** typed confirmation (user types the project name) before `rsync --delete`. Dry-run preview is a future enhancement.
> 4. **Excludes:** a per-project `--exclude` field shipped in v1.
> 5. **Large dirs:** raise the backup timeout via `BACKUP_TIMEOUT` (default 1800s) in v1; async/background jobs are future work.

---

## Story Overview

**As a** self-hoster managing several apps on a VPS
**I want** to back up Docker volumes and host directories (uploads, media, config) — with or without a database
**So that** restic protects my files and databases together in one snapshot, not just databases.

**As an** operator restoring after an incident
**I want** to choose whether a restore touches the database, the files, or both
**So that** I can recover precisely without overwriting the half that is still healthy.

---

## Why This Feature?

### Current Gap:
- A project can only back up a **database dump**. Files are supported in exactly one hardcoded case: WordPress `wp-uploads` (`app/main.py:471-472`, `:555-556`).
- There is no way to back up a Docker volume that is not WordPress uploads, or any host directory.
- `include_db` is implicit and always on — a files-only project is impossible.

### Real-World Use Case (Media-heavy app):
A team runs a Postgres app plus a `media_vol` Docker volume of user uploads and a `/srv/app/config` directory on the host.
- They need the DB dump **and** both file sets in the same snapshot, on the same retention policy.
- After a bad deploy they want to restore **only the files**, leaving the (already-fixed) database alone.

This cannot be done with the current implementation.

### Solution:
Extend the existing restic backup/restore engine (`run_app_backup`, `run_restore`) to support:
- **`backup_paths`** — a JSON list of `{source, value}` entries (`source` = `volume` | `host`).
- **`include_db`** — make the database dump optional.
- **Restore scope** — `all` / `db` / `files`.
- **Excludes** — restic `--exclude` patterns per project.
- **Backward compatible** — existing `database` and `wordpress` projects behave exactly as before.

---

## User Personas

### Primary: Ravi — The Solo Self-Hoster
- **Role:** Runs ~6 apps on one Contabo VPS via Coolify.
- **Goal:** One dashboard that backs up databases *and* the volumes/dirs that hold uploads and config.
- **Pain Point:** "My DB is safe but my users' uploaded files aren't backed up at all unless it's WordPress."

### Secondary: Logesh — The Operator on Call
- **Role:** Handles restores when something breaks.
- **Goal:** Restore just the files after a bad deploy without clobbering the live database.
- **Pain Point:** "Restore is all-or-nothing today — I can't recover only the part that broke."

---

## Detailed Sub-Stories

### Sub-Story 1: Data model for paths + optional DB
**Story ID:** BKP-1.1 · **Points:** 3 · **Effort:** 3h
```gherkin
As a developer
I want backup_paths (JSON) and include_db columns plus payload fields
So that a project can store a list of file sources and toggle the DB dump
```

### Sub-Story 2: Path resolution module + host mount
**Story ID:** BKP-1.2 · **Points:** 3 · **Effort:** 3h
```gherkin
As a developer
I want a helper that resolves and validates volume/host paths against HOST_BACKUP_ROOT
So that backups read real locations and host paths cannot escape the allowed root
```

### Sub-Story 3: Backup engine generalization
**Story ID:** BKP-1.3 · **Points:** 5 · **Effort:** 4h
```gherkin
As a self-hoster
I want backups to include my configured paths (and skip the DB when include_db is off)
So that one snapshot holds exactly the data I chose, with excludes applied
```

### Sub-Story 4: Restore engine + scope
**Story ID:** BKP-1.4 · **Points:** 5 · **Effort:** 5h
```gherkin
As an operator
I want to restore all, only the DB, or only the files from a snapshot
So that I can recover precisely without overwriting healthy data
```

### Sub-Story 5: "Backup contents" form section
**Story ID:** BKP-1.5 · **Points:** 5 · **Effort:** 4h
```gherkin
As a self-hoster
I want the add/edit slide-over to let me toggle the DB and add path rows + excludes
So that I can configure file backups without editing the database by hand
```

### Sub-Story 6: Restore modal (scope + typed confirm)
**Story ID:** BKP-1.6 · **Points:** 5 · **Effort:** 4h
```gherkin
As an operator
I want the restore dialog to choose a scope and require typing the project name
So that destructive file restores are deliberate, not accidental
```

---

## Acceptance Criteria

### AC1: Create a files-only project
```gherkin
GIVEN I open the Add Project slide-over
WHEN I uncheck "Back up database" and add one volume path
THEN the project saves with include_db=false and one backup_paths entry
AND no database connection fields are required
```

### AC2: Create a DB + paths project
```gherkin
GIVEN I am adding a project
WHEN I keep "Back up database" checked and add a host path and a volume path
THEN the project saves with include_db=true and two backup_paths entries
```

### AC3: Backup includes a Docker volume
```gherkin
GIVEN a project with a backup_paths entry {source:"volume", value:"media_vol"}
WHEN a backup runs
THEN restic backs up /var/lib/docker/volumes/media_vol/_data under the project tag
```

### AC4: Backup includes a host directory under the root
```gherkin
GIVEN HOST_BACKUP_ROOT=/srv and a backup_paths entry {source:"host", value:"/srv/app/config"}
WHEN a backup runs
THEN restic backs up /srv/app/config in the snapshot
```

### AC5: Host path outside the root is rejected
```gherkin
GIVEN HOST_BACKUP_ROOT=/srv
WHEN I add a host path "/etc/shadow" or "/srv/../etc"
THEN saving fails with a clear error and no backup is attempted
```

### AC6: include_db=false skips the dump
```gherkin
GIVEN a project with include_db=false
WHEN a backup runs
THEN no pg_dump/mysqldump is executed and the snapshot contains only the file paths
```

### AC7: Restore scope "files only"
```gherkin
GIVEN a snapshot containing a DB dump and file paths
WHEN I restore with scope "files only"
THEN the files rsync back to their targets
AND the database is NOT imported
```

### AC8: Restore scope "db only"
```gherkin
GIVEN a snapshot containing a DB dump and file paths
WHEN I restore with scope "db only"
THEN the database is imported
AND no files are written back
```

### AC9: Destructive restore requires typed confirmation
```gherkin
GIVEN I trigger a file or full restore
WHEN the confirm dialog appears
THEN the Restore button stays disabled until I type the exact project name
```

### AC10: Excludes are honored
```gherkin
GIVEN a project with excludes "node_modules,*.log"
WHEN a backup runs
THEN restic is invoked with matching --exclude flags
```

### AC11: Backward compatibility
```gherkin
GIVEN an existing database or wordpress project created before this feature
WHEN it is loaded, backed up, and restored
THEN behavior is unchanged (DB dump, and WP uploads for wordpress) with no migration error
```

### AC12: Large backup within raised timeout
```gherkin
GIVEN BACKUP_TIMEOUT=1800
WHEN a backup of a large directory runs
THEN it is allowed up to 1800s before timing out
```

---

## Technical Implementation

### Part 1: Data model (3h)

#### Task 1.1: Migrations — extend `init_db()`
**File:** `app/main.py` (migration list at `:91-93`)
```python
for migration in [
    "ALTER TABLE projects ADD COLUMN schedule_cron TEXT DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN storage_type TEXT DEFAULT 's3'",
    "ALTER TABLE projects ADD COLUMN local_repo_path TEXT",
    # BKP-1
    "ALTER TABLE projects ADD COLUMN backup_paths TEXT DEFAULT '[]'",
    "ALTER TABLE projects ADD COLUMN include_db INTEGER DEFAULT 1",
    "ALTER TABLE projects ADD COLUMN backup_excludes TEXT DEFAULT ''",
]:
```

#### Task 1.2: Payload — extend `ProjectPayload`
**File:** `app/main.py:161`
```python
class PathSpec(BaseModel):
    source: str            # "volume" | "host"
    value:  str

class ProjectPayload(BaseModel):
    # ...existing fields...
    backup_paths:    list[PathSpec] = []
    include_db:      bool = True
    backup_excludes: Optional[str] = ""
```

#### Task 1.3: CRUD — persist new fields
**File:** `app/main.py` (INSERT `:~625`, UPDATE `:~659`)
Serialize on write (`json.dumps([p.dict() for p in body.backup_paths])`, `int(body.include_db)`) and parse on read in `get_project_or_404` (`json.loads(row["backup_paths"] or "[]")`).

### Part 2: Path resolution (3h) — keeps `main.py` from growing past 815 lines

#### Task 2.1: New module
**File:** `app/backup_paths.py` **(NEW)**
```python
"""Resolve and validate backup path specs (volume | host)."""
import os
from pathlib import Path

HOST_BACKUP_ROOT = os.environ.get("HOST_BACKUP_ROOT", "/srv")

def resolve(spec: dict) -> str:
    """Return the in-container path restic should back up."""
    if spec["source"] == "volume":
        return f"/var/lib/docker/volumes/{spec['value']}/_data"
    if spec["source"] == "host":
        p = os.path.normpath(spec["value"])
        if not (p == HOST_BACKUP_ROOT or p.startswith(HOST_BACKUP_ROOT + "/")):
            raise ValueError(f"Host path must be under {HOST_BACKUP_ROOT}: {p}")
        return p                       # mounted 1:1 into the container
    raise ValueError(f"Unknown path source: {spec['source']}")

def validate(specs: list[dict]) -> None:
    for s in specs:
        resolve(s)                     # raises on bad source / escaped root
```

#### Task 2.2: Compose mount + env
**File:** `docker-compose.yml`
```yaml
    environment:
      - HOST_BACKUP_ROOT=${HOST_BACKUP_ROOT:-/srv}
    volumes:
      - ${HOST_BACKUP_ROOT:-/srv}:${HOST_BACKUP_ROOT:-/srv}:rw   # backup + restore host dirs
```

### Part 3: Backup engine (4h)

#### Task 3.1: Generalize `run_app_backup`
**File:** `app/main.py:450` (replaces the WP-only block at `:471-472`)
```python
backup_paths = []
if project.get("include_db", 1):
    ok, err = _dump_db(project, dump_file)
    if not ok:
        return False, f"DB dump failed: {err}"
    backup_paths.append(str(dump_file))

for spec in project.get("backup_paths", []):
    path = backup_paths_mod.resolve(spec)        # app/backup_paths.py
    if Path(path).exists():
        backup_paths.append(path)
    else:
        append_log(log_file, f"WARN: path missing, skipped: {path}")

# legacy WP preset still works
if project["project_type"] == "wordpress" and project.get("wp_resource_id"):
    uploads = Path(f"/var/lib/docker/volumes/{project['wp_resource_id']}_wp-uploads/_data")
    if uploads.exists():
        backup_paths.append(str(uploads))

excludes = [e.strip() for e in (project.get("backup_excludes") or "").split(",") if e.strip()]
exclude_flags = sum([["--exclude", e] for e in excludes], [])
r = subprocess.run(
    ["restic", "backup"] + backup_paths + exclude_flags + ["--tag", tag],
    env=restic_env, capture_output=True, text=True,
    timeout=int(os.environ.get("BACKUP_TIMEOUT", 1800)),
)
```

### Part 4: Restore engine + scope (5h)

#### Task 4.1: Add scope to `run_restore`
**File:** `app/main.py:531`
```python
def run_restore(project: dict, snapshot_id: str, scope: str = "all") -> tuple[bool, str]:
    # ...restic restore to restore_dir...
    if scope in ("all", "db") and project.get("include_db", 1):
        sql = next(iter(restore_dir.rglob("*.sql")), None)
        if sql: _import_db(project, sql)
    if scope in ("all", "files"):
        for spec in project.get("backup_paths", []):
            target = backup_paths_mod.resolve(spec)
            src = restore_dir / target.lstrip("/")
            if src.exists():
                subprocess.run(["rsync", "-a", "--delete", f"{src}/", f"{target}/"], ...)
```

#### Task 4.2: Pass scope from the endpoint
**File:** `app/main.py` (`restore_snapshot` route `:~777`) — accept `scope` query/body param, default `all`.

### Part 5: "Backup contents" form section (4h)
**File:** `app/static/forms.js`
Add a section after "Connection": a `Back up database` checkbox (`include_db`), a repeatable list of path rows (`<select>` source = Volume/Host + value input + remove), and an excludes input. Extend `saveProjectForm()` to emit `backup_paths`, `include_db`, `backup_excludes`. When `include_db` is unchecked, relax the DB-connection requirement.

### Part 6: Restore modal (4h)
**File:** `app/static/app.js` (`restoreSnapshot`, `confirmDialog`)
Replace the plain confirm with a restore dialog: a scope radio (`Everything` / `Database only` / `Files only`) and a text input that must equal the project name to enable the Restore button. Pass `scope` to `POST /restore`.

---

## File Summary

| File | Action | Approximate Lines |
|------|--------|-------------------|
| `app/backup_paths.py` | **NEW** — path resolve/validate | ~40 lines |
| `app/main.py` | Modify — migrations, payload, CRUD, backup + restore loops, restore scope | +90 lines |
| `docker-compose.yml` | Modify — `HOST_BACKUP_ROOT` env + 1:1 mount | +2 lines |
| `app/static/forms.js` | Modify — backup-contents section, save logic | +70 lines |
| `app/static/app.js` | Modify — restore scope modal + typed confirm | +55 lines |

**Backend impact:** 3 new SQLite columns via the existing idempotent ALTER pattern (no destructive migration); one new helper module to keep `main.py` under control. No new dependencies (restic/rsync already in the image).

---

## UI Test Setup

| Field | Value |
|-------|-------|
| **App URL** | http://127.0.0.1:8100 |
| **Test Route** | `/` — Add Project slide-over; project detail → Restore |
| **Login as** | `admin` / `admin` (env `AUTH_USERNAME` / `AUTH_PASSWORD`) |
| **Test Data** | A Docker volume present locally (e.g. `media_vol`) and a host dir under `HOST_BACKUP_ROOT` (e.g. `/srv/app/config`); `RESTIC_PASSWORD` set; local storage so no S3 needed |
| **Non-testable ACs** | AC12 (timeout — config/behavioral, not UI-visible); AC3/AC4 verified via snapshot "Contents" column rather than filesystem |
