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

---

## Pipeline Log

### Review — 2026-06-22 11:26 — Verdict: APPROVED_WITH_CONCERNS

**Reviewer:** Senior engineer pre-dev review (no code written; story file only).
**Pipeline learnings:** none found for this project (gstack learnings.jsonl absent).

#### Dependencies
- **None declared.** Confirmed: story is self-contained against the current single-file FastAPI app. No external story dependencies to verify.

#### Verdict rationale
Story is well-researched and almost all cited line numbers/functions are accurate against the real code. Not BLOCKED — no hard dependency or missing-consume-file issue. Downgraded from APPROVED to APPROVED_WITH_CONCERNS because of (a) a real QA/testability gap in the compose plan, (b) two file-plan omissions (`index.html`, `docker-compose.local.yml`), and (c) several security/correctness risks in the locked decisions that the implementer must handle explicitly.

#### File plan (verified against codebase)
- **CREATE:** `app/backup_paths.py` — confirmed does not exist. Module keeps `main.py` (currently exactly 815 lines) from blowing the 600-line cap; note main.py is ALREADY over the 600-line global rule, so the +90 lines must be watched — see risks.
- **MODIFY (all confirmed to exist):**
  - `app/main.py` (815 lines) — `init_db()` migration list at **:90-94** (story says :91-93; close, the list literal spans 90-94). `ProjectPayload` at **:161** ✅. `_dump_db` **:350** ✅. `_import_db` **:397** ✅. `run_app_backup` **:450** with WP-only file block at **:471-474** ✅ (story cites :471-472; the block is actually :471-475). `run_restore` **:531** ✅ with uploads rsync at **:555-569** ✅. INSERT is at **:619-643** (story says ~625 ✅). UPDATE at **:651-677** (story says ~659 ✅). `restore_snapshot` route at **:784-790** (story says ~777; drifted by ~7 lines — see Reference drift).
  - `docker-compose.yml` — confirmed; currently has NO `HOST_BACKUP_ROOT` env and NO `/srv` mount. Has `/var/lib/docker/volumes:...:rw` (line 33) so AC3 volume backups already have their mount in prod.
  - `app/static/forms.js` (223 lines) — `saveProjectForm()` at **:173**, `formHTML()`/`updateFormVisibility()` present ✅.
  - `app/static/app.js` (354 lines) — `restoreSnapshot` at **:302**, `confirmDialog` at **:324** ✅.
- **MISSING FROM FILE PLAN (must be added):**
  - `app/templates/index.html` — the restore-scope modal (Part 6, AC9) needs DOM that does not exist. The current `#confirmWrap` modal (index.html :72-81) has only a title, a body `<p>`, and Cancel/Confirm buttons — **no scope radios and no project-name text input**. Either index.html must be modified to add a richer restore modal, OR app.js must inject this markup at runtime into `#confirmBody`. The story implies the latter ("Replace the plain confirm with a restore dialog") but does not say index.html is untouched on purpose. Implementer must pick one; if injecting into `#confirmBody`, the typed-confirm input and scope radios must be wired up inside `confirmDialog` or a new function. Flag: AC9 requires the OK button to stay disabled until the name matches — `#confirmOk` is a static button; the new dialog must manage its `disabled` state and reset it on close.
  - `app/docker-compose.local.yml` — **this is the file QA actually uses** (it sets `127.0.0.1:8100`, `admin/admin`, `RESTIC_PASSWORD=localtest`, local storage — exactly the "UI Test Setup" table). It does NOT mount `/var/lib/docker/volumes` and does NOT mount any host root. As written, AC3 (volume backup) and AC4 (host-dir backup) **cannot be exercised in the documented QA environment** because the paths won't exist in the container. The mount/env change must also be applied to `docker-compose.local.yml` (or QA setup updated), otherwise AC3/AC4 are untestable in the only local harness.
- **DO NOT TOUCH:** No other-story ownership boundaries exist (this is the only story file; no `docs/stories/` convention beyond it). `app/static/styles.css` is not in the plan but the new form rows + restore modal will likely need styles — minor, flag below.

#### Reference drift
- `restore_snapshot` route — story says `:~777`; actual decorator/def is at **:784-785**. Minor drift (~7 lines), still findable.
- `init_db` migration list — story says `:91-93`; the list literal is **:90-94** (3 existing migrations at :91-93, brackets at :90/:94). Accurate enough.
- WP-only block — story says `:471-472` and `:555-556`; actual spans are **:471-475** (backup) and **:555-569** (restore). The cited start lines are correct.
- WP volume name — story Task 3.1 preserves `{wp_resource_id}_wp-uploads/_data`, which **matches** the real code at :472/:556. Good — backward compat (AC11) is correctly specified.
- `get_project_or_404` (:270-276) currently returns a raw `dict(row)` with **no JSON parsing**. Task 1.3 says to parse `backup_paths` there — correct and necessary, since `run_app_backup`/`run_restore` and `_backup_job` (:113-122, which builds `project = dict(row)` directly from a raw row, NOT via `get_project_or_404`) consume the dict. **Risk:** the scheduler path (`_backup_job`) and `trigger_backup` (:722, uses `get_project_or_404`) take different routes to the project dict. If parsing only happens in `get_project_or_404`, scheduled backups will see `backup_paths` as a raw JSON **string**, not a list, and `project.get("backup_paths", [])` will iterate characters. Parsing must live in a shared helper or be applied in `_backup_job` too. This is the single most likely correctness bug — call it out to the implementer.

#### Acceptance criteria notes
- **Unit-testable (pure logic, no app):** AC5 (path escape rejection — `backup_paths.resolve/validate`), AC10 (exclude-flag construction), the path-resolution for AC3/AC4 (resolve() output). These are the high-value tests; note the project has **NO test suite and no tests/ dir** — any unit test is net-new scaffolding, out of scope unless the implementer adds it.
- **Needs running app + real infra:** AC3 (needs a Docker volume mounted), AC4 (needs host root mounted), AC6 (skip dump), AC7/AC8 (restore scope — need a real snapshot with both a `.sql` and file paths), AC11 (backward compat), AC12 (timeout).
- **Manual / UI only:** AC1, AC2 (form behavior), AC9 (typed-confirm disabled-button behavior).
- **AC quality issues:**
  - **AC5 is under-specified.** "saving fails with a clear error" — but the current CRUD endpoints (`create_project` :619, `update_project` :651) do **no** server-side validation and would happily persist a bad path; validation today would only run at backup time inside `resolve()`. The story's `validate()` helper exists but the story never wires it into the POST/PUT handlers. To satisfy "saving fails" (not "backup fails"), the implementer MUST call `backup_paths.validate()` in `create_project`/`update_project` and return HTTP 400. As written this is ambiguous — clarify that validation is at save time, server-side, not just client-side. Note `saveProjectForm` (forms.js :206-207) only validates name/tag client-side; client checks are bypassable.
  - **AC6** says "no pg_dump/mysqldump is executed and the snapshot contains only the file paths." Edge case the story's Task 3.1 snippet handles (guards `_dump_db` behind `include_db`) but does NOT handle: if `include_db=false` AND `backup_paths` is empty AND not WordPress, `backup_paths` list is empty and `restic backup` is invoked with zero source paths → restic errors. Add an empty-source guard (fail early with a clear message).
  - **AC7/AC8 restore path-mapping is the riskiest logic and the story's snippet is hand-wavy** (`src = restore_dir / target.lstrip("/")`, ends in `...`). restic restores preserve absolute path structure under `--target`, so a host path `/srv/app/config` lands at `restore_dir/srv/app/config` and a volume at `restore_dir/var/lib/docker/volumes/<vol>/_data`. The `lstrip("/")` mapping is roughly right ONLY if `resolve()` returns the same absolute path used at backup time — which it does for host (1:1) and volume. But the existing WP restore (:557) finds uploads via `rglob("_data")`, which would now collide with volume restores (multiple `_data` dirs). The implementer must reconcile the new per-spec mapping with the legacy WP `rglob("_data")` logic so AC11 (WP still works) and AC7 (new volumes) don't clash. Mark AC7/AC8 as needing careful integration testing, not just unit.

#### Risks & notes for the implementer
1. **Host-mount security model is the biggest risk (locked decision #1).** Bind-mounting `HOST_BACKUP_ROOT` (default `/srv`) read-WRITE 1:1 means the container can both read and `rsync --delete` over anything under `/srv` on the host. Combined with the existing `/var/run/docker.sock` mount (already full host-root-equivalent access) the threat surface is already high, but the new RW host mount + a restore path-mapping bug could `rsync --delete` the wrong directory. Recommend: (a) restore writes should re-validate the resolved target through `validate()` before any `rsync --delete`; (b) consider mounting read-only for backup and only RW for the restore code path, or at least document that `/srv` RW is deliberate. The `os.path.normpath` + `startswith(HOST_BACKUP_ROOT + "/")` check in the snippet is correct against `..` traversal, but does NOT resolve symlinks — a symlink under `/srv` pointing to `/etc` would pass `resolve()` yet `rsync` would follow it. Use `os.path.realpath` (not just `normpath`) for the allow-list check, or `rsync` will escape the root via symlinks. **This is a concrete hole in the AC5 implementation as drafted.**
2. **`rsync --delete` with typed confirm (AC9) is destructive and irreversible.** Locked decision #3 defers dry-run. Acceptable for v1, but the typed-confirm MUST be enforced server-side too, or at minimum the destructive scope (`files`/`all`) should be the only path that runs `--delete`. A `db`-only restore must never touch files (AC8) — verify the scope guard wraps the entire rsync block.
3. **main.py is already at 815 lines, over the 600-line global cap.** The new module (`backup_paths.py`) helps, but +90 lines pushes main.py toward 900. Per global rules the implementer should extract the restore/backup engine into `app/services/` if it keeps growing; at minimum do not add more than necessary inline. Flag, not a blocker.
4. **forms.js (223) and app.js (354) are under caps**; +70 and +55 lines keep them under 600. Fine. But `forms.js` `formHTML()` is one large template — adding a repeatable path-row UI (Part 5) risks pushing that single function past readability; consider a `pathRowHTML()` helper.
5. **`include_db` default and backward compat:** migration default `INTEGER DEFAULT 1` is correct so existing rows behave as DB-on (AC11). But `project.get("include_db", 1)` returns an int from SQLite; the Task 3.1 snippet treats it as truthy — fine for 0/1, but if the payload writes a Python `bool` via `int(body.include_db)` it stays 0/1. Consistent. Just ensure the read path coerces (0 is falsy, ok).
6. **Snapshot "Contents" column (app.js :236) uses path heuristics** (`includes('upload')`, `endsWith('.sql')`). New volume/host paths will render as the last path segment (e.g. `_data` for every volume — indistinguishable). The UI Test Setup says AC3/AC4 are "verified via snapshot Contents column" — but `_data` for all volumes is not a useful verification signal. Minor UX gap; consider improving the contents formatter or verifying AC3/AC4 via `restic ls` instead.
7. **No server-side payload validation today** (CRUD endpoints trust the body). New fields `backup_paths`/`backup_excludes` are free-form strings going into shell-adjacent contexts (restic `--exclude`, rsync paths). Ensure exclude patterns and path values are passed as argv elements (the snippet does use list-form `subprocess.run`, good — no shell=True), and that volume `value` cannot contain `../` to escape `/var/lib/docker/volumes/` (the resolve() for `source=volume` does NO validation at all — `value="../../etc"` yields `/var/lib/docker/volumes/../../etc/_data`). **Add the same normpath/realpath allow-list check to the volume branch**, not just host.
8. **`docker-compose.local.yml` and QA:** as noted, update it or AC3/AC4 are unverifiable locally. Also `styles.css` likely needs additions for the new form section + restore modal radios — not in the plan but expected.

**Summary for implementer:** proceed. Must-fix before "done": (1) parse `backup_paths` on every read path incl. `_backup_job`, (2) server-side `validate()` in CRUD for AC5, (3) `realpath` + volume-branch allow-list to close symlink/traversal holes, (4) empty-source guard for AC6, (5) reconcile new file-restore mapping with legacy WP `rglob("_data")`, (6) add the restore-modal DOM (index.html or runtime), (7) apply the mount/env to `docker-compose.local.yml`.

---

### Implementation — 2026-06-22 11:46

**Agent:** Integration agent. The story was built in 3 parallel waves by 5 section agents; this entry combines them and records the integration pass.

**Parallel section breakdown:**
- **Wave 1 (parallel):** `path-module` → `app/backup_paths.py` (NEW); `compose-mount` → `docker-compose.yml` + `docker-compose.local.yml`.
- **Wave 2 (single, file-exclusive):** `engine-main` → `app/main.py` (migrations, payload, CRUD validation, backup + restore engines, restore scope route). Deliberately not parallelized to honor file-exclusivity.
- **Wave 3 (parallel):** `form-contents` → `app/static/forms.js` + `app/static/styles.css` (styles.css owned exclusively here); `restore-modal` → `app/templates/index.html` + `app/static/app.js`.

#### Files created
- `app/backup_paths.py` — path resolve/validate module; realpath-based allow-list on BOTH host and volume branches; `resolve()/validate()/HOST_BACKUP_ROOT` public contract (~66 lines).

#### Files modified
- `app/main.py` — 815 → 986 lines. `init_db` migrations (`backup_paths`, `include_db`, `backup_excludes`); `PathSpec`/`ProjectPayload` fields; `_hydrate_project` shared helper used by BOTH `get_project_or_404` AND `_backup_job`; `run_app_backup` (optional DB dump, per-spec resolve, excludes, timeout, empty-source guard, legacy WP preset); `run_restore`/`_restore_files`/`_rsync_into_target` (scope all/db/files, re-validate before `--delete`, explicit WP uploads path); `create_project`/`update_project` (`validate()` → 400); `restore_snapshot` route (`scope` from query or body).
- `app/static/forms.js` — 223 → 312 lines. "Backup contents" section (DB toggle, repeatable path rows, excludes), `collectBackupPaths()`, files-only visibility + client guard, emits `backup_paths`/`include_db`/`backup_excludes`.
- `app/static/styles.css` — 207 → 227 lines. `.path-row`/`.path-source`/`.path-value`/`.path-remove`/`.path-add` + (unused but harmless) `.restore-scope`/`.confirm-name-*` classes.
- `app/static/app.js` — 354 → 395 lines. `restoreDialog()` + rewritten `restoreSnapshot()` POSTing `{scope}`.
- `app/templates/index.html` — added static `#restoreWrap` restore modal (scope radios + typed-confirm + disabled Restore button).
- `docker-compose.yml` — `HOST_BACKUP_ROOT` env + 1:1 host-root mount (volumes mount already present).
- `docker-compose.local.yml` — `HOST_BACKUP_ROOT` env + 1:1 host-root mount + NEW `/var/lib/docker/volumes` mount (makes AC3/AC4 testable in the QA harness).

#### Glue fixes applied during integration
- **None required.** All five sections integrated cleanly on the agreed seams:
  - Body field-name contract matches end-to-end: forms.js writes `backup_paths` (array of `{source,value}`), `include_db` (bool), `backup_excludes` (comma string); `ProjectPayload` reads the same names. Verified via direct route-handler test.
  - Restore scope contract matches: app.js POSTs `{scope}` ∈ `all|db|files`; `RestorePayload`/route accept it from body or query and 400 on bad values.
  - Restore-modal DOM IDs in `index.html` (`#restoreWrap`, `#restoreBody`, `#restoreName`, `#restoreConfirmInput`, `#restoreOk`, `#restoreCancel`, radio `name="restoreScope"`) match every selector in `app.js restoreDialog()`.
  - All CSS classes the restore modal reuses (`.modal-wrap`/`.modal`/`.seg`/`.finput`/`.flabel`/`.fhint`/`.fgroup`/`.btn:disabled`/`.show`) exist in `styles.css` after the form-contents diff landed.
  - Wave-1 `backup_paths` API is imported and consumed verbatim by `main.py` (`from app import backup_paths`); `resolve`/`validate`/`HOST_BACKUP_ROOT` are never redeclared. The shipped module uses the hardened realpath + dual-branch allow-list (NOT the naive normpath snippet), so AC5 and review MUST-FIX #3 hold.

#### Contracts preserved
- Legacy WordPress backup/restore via explicit `{wp_resource_id}_wp-uploads/_data` path — NOT blind `rglob('_data')`, which would now collide with new volume `_data` dirs (MUST-FIX #5 / AC11).
- Existing `database`/`wordpress` projects default to `include_db=1`/True via migration + `_hydrate_project` — behavior unchanged (AC11), verified with a legacy row that lacks the new columns.
- `subprocess.run` list-form throughout (no `shell=True`); excludes/paths passed as argv elements.
- All 7 review MUST-FIXes confirmed present: (1) shared `_hydrate_project` on every read path incl. `_backup_job`; (2) `validate()` wired into create+update → 400; (3) realpath + volume-branch allow-list; (4) empty-source guard; (5) WP/volume restore reconciliation; (6) restore-modal DOM; (7) mount/env in both compose files.

#### Deviations from the story
- **`app/main.py` remains over the 600-line global cap (986 lines).** It was already 815 lines on arrival (pre-existing violation). Extracting the backup/restore engine to `app/services/` is the correct follow-up but is a large, non-surgical refactor outside the integration mandate; deferred and flagged here rather than performed post-merge.
- Restore modal implemented as a dedicated static `#restoreWrap` in index.html (not runtime-injected into the delete-confirm `<p>`), because injecting block-level radios into a `<p>` is invalid HTML and would entangle the still-used `deleteProject` confirm flow. Functionally equivalent to the story intent.
- `styles.css` gained `.restore-scope`/`.confirm-name-*` classes (planner-suggested names) that the restore-modal section ultimately did not use (it reused existing `.seg`/`.finput`/etc.). Harmless dead CSS; left as-is to avoid editing a file outside a needed fix.

#### Build/typecheck status
- `python -m py_compile app/main.py app/backup_paths.py` → OK.
- `node --check app/static/forms.js app/static/app.js` → OK.
- `yaml.safe_load` on both compose files → OK.
- Runtime import of `app.main` (venv, clean env via PowerShell) + wiring assertions → OK (`PathSpec`, `RestorePayload`, `run_restore(scope=...)`, shared `_hydrate_project` raw-string→list / legacy-row defaults, wave-1 `backup_paths` contract).
- Direct route-handler integration test (TestClient unavailable — no `httpx` dep — so handlers called directly against a fresh DB): AC2 create+read-back, AC1 files-only, AC5 host+volume escape rejection (create AND update → 400), AC11 legacy-row backward-compat, restore scope validation → ALL PASSED.

#### Scope / ownership verification
- `git status`: only plan files are dirty — `app/backup_paths.py` (new), `app/main.py`, `app/static/{app.js,forms.js,styles.css}`, `app/templates/index.html`, `docker-compose.yml`, `docker-compose.local.yml`. `ui-redesign/` is pre-existing untracked scratch work, NOT touched by any section. Nothing outside the plan was modified. No commits made (working tree left dirty).

#### Notes for the tester
- Run locally: `docker compose -f docker-compose.local.yml up` → http://127.0.0.1:8100, login `admin`/`admin`. On a Windows host set `HOST_BACKUP_ROOT` to an existing local dir before `up` (default `/srv` won't exist).
- Or bare: `uvicorn app.main:app --port 8000` from project root (the `app` package import requires running from root; deps are in `.venv`, and `httpx` is needed only if you want `TestClient`).
- Verify AC3/AC4 (volume/host backup) via `restic ls <snapshot>`, NOT the UI snapshot "Contents" column (it renders every volume as `_data` — unchanged, out of scope).
- AC1/AC2/AC9 are manual UI. AC9: Restore button stays disabled until the typed text exactly equals the project name; reopening resets scope to "Everything", clears input, re-disables. Confirm the POST body carries `scope` (DevTools): db→`{"scope":"db"}`, files→`{"scope":"files"}`, all→`{"scope":"all"}`.
- AC6/AC7/AC8/AC11/AC12 need the running container with real restic + the volumes/host mounts. The `/srv` env on Windows Git-Bash gets path-mangled (a shell artifact, not a logic bug); POSIX/container semantics are correct.

### Test — Round 1 — 2026-06-22 11:52 — PASS

## Test Report: BKP-1

**Overall:** PASS

### Per-AC results
- AC1: PASS (data-layer slice) — files-only project persists `include_db=false` + one `backup_paths` entry via `create_project`. Full slide-over UI (uncheck "Back up database", DB fields not required) is NOT AUTOMATED — manual: open Add Project, uncheck "Back up database", add one volume path, save; confirm the connection section hides and save succeeds.
- AC2: PASS (data-layer slice) — DB + host + volume project persists `include_db=true` with two `backup_paths` entries. UI portion NOT AUTOMATED — manual: keep "Back up database" checked, add one host + one volume path, save; confirm two rows persist on reopen.
- AC3: PASS — `backup_paths.resolve({source:volume, value:media_vol})` returns `.../media_vol/_data` under the docker volumes root; volume value `../../etc` is rejected (no escape). End-to-end restic capture of the live volume needs the running container — manual: run a backup, then `restic ls <snap>` shows `/var/lib/docker/volumes/media_vol/_data`.
- AC4: PASS — host path `/srv/app/config` under `HOST_BACKUP_ROOT=/srv` resolves and is accepted; root itself accepted. Live restic capture needs the running container — manual: `restic ls <snap>` shows `/srv/app/config`.
- AC5: PASS — `/etc/shadow` and `/srv/../etc` rejected with `ValueError`; unknown source rejected; `validate()` raises on first bad spec; `create_project` AND `update_project` return HTTP 400 and persist nothing. Save-time + server-side + symlink/traversal-safe (realpath) + applies to the volume branch too. All MUST-FIX #2/#3 behaviours confirmed.
- AC6: PASS — `include_db=false` + no paths + non-WP triggers the empty-source guard ("Nothing to back up…") and `subprocess.run` is never reached (restic not invoked); `_dump_db` is not called. With one path + `include_db=false`, `_dump_db` is never called and the restic argv contains no `.sql` source.
- AC7: PASS — `run_restore(scope="files")` calls `_restore_files` and does NOT call `_import_db`.
- AC8: PASS — `run_restore(scope="db")` calls `_import_db` and does NOT call `_restore_files` (rsync/--delete block fully bypassed); invalid scope rejected with 400 at the route.
- AC9: NOT AUTOMATED (manual UI) — restore modal DOM verified present in `index.html` (`#restoreWrap`, scope radios `all|db|files`, `#restoreName`, `#restoreConfirmInput`, `#restoreOk[disabled]`) and `app.js restoreDialog()` wires disable/reset logic. Manual: open Restore; the Restore button stays disabled until the typed text exactly equals the project name; reopening resets scope to "Everything", clears the input, re-disables.
- AC10: PASS — excludes `node_modules,*.log` become `--exclude node_modules --exclude *.log` in the restic backup argv (each pattern preceded by `--exclude`); empty excludes => no `--exclude` flag.
- AC11: PASS — a legacy row missing the new columns hydrates to `include_db=True` / `backup_paths=[]` with no error; raw JSON string parses to a list; `include_db=0` coerces to `False`. Shared `_hydrate_project` (MUST-FIX #1) used on both read paths. Live WP backup/restore unchanged needs the running container — manual: back up + restore an existing wordpress project; uploads restore via the explicit `{wp_resource_id}_wp-uploads/_data` path.
- AC12: NOT AUTOMATED (config/behavioural) — code reads `int(os.environ.get("BACKUP_TIMEOUT", 1800))` and passes it as the restic backup `timeout`. Manual: set `BACKUP_TIMEOUT=1800`, run a large backup, confirm it is allowed up to 1800s.

### Test files written
- `D:/Wisright/DB-backup-manager/tests/conftest.py` — sets import-time env (DB_PATH temp file, HOST_BACKUP_ROOT) before `app.main` import.
- `D:/Wisright/DB-backup-manager/tests/test_bkp1.py` — 22 tests covering AC1/AC2 (persistence slice), AC3, AC4, AC5, AC6, AC7, AC8, AC10, AC11.

### Failures (empty if PASS)
- None.

### Suite output summary
- `22 passed, 35 warnings in 0.60s` — command: `.venv/Scripts/python.exe -m pytest tests/ -q`.
- Warnings are pre-existing Pydantic v2 (`.dict()` deprecation in `app/main.py:762,803`) and starlette `asyncio.iscoroutinefunction` deprecations — not failures.
- Regression guards re-run green: `py_compile app/main.py app/backup_paths.py` OK; `node --check app/static/{forms.js,app.js}` OK.

**Environment notes for maintainers:**
- The project had NO test harness; `pytest` was installed into `.venv` (not added to `requirements.txt`) and a `tests/` dir created. No app/runtime deps were changed.
- AC3/AC4/AC6/AC7/AC8 logic is unit-verified by exercising `backup_paths.resolve`/`validate` and by monkeypatching `subprocess.run`/`_dump_db`/`_import_db`/`_restore_files` to assert the dispatch and argv contract. Real restic/rsync execution against live volumes/host dirs still requires the running container (`docker compose -f docker-compose.local.yml up`) and is the manual verification noted per-AC above. Both compose files now carry `HOST_BACKUP_ROOT` env + the 1:1 host mount + `/var/lib/docker/volumes` mount, so that manual run is possible.
- Tests assert path decisions structurally (segments / under-root / ValueError) because `resolve()` uses `os.path.realpath` + `os.sep`; on the Windows dev box `/srv` resolves to `D:\srv`, but the accept/reject contract is identical to the Linux container.

### QA — 2026-06-22 12:05 — ISSUES

## QA Report: BKP-1 (Browser)

**Overall:** ISSUES (1 environment issue: a stale dev server; no app-code defects found)

### Scope
Verified the UI-only / "needs running app" ACs in gstack's headless Chromium: **AC1, AC2, AC5 (save-time UI), AC9**. AC3/AC4/AC6/AC7/AC8/AC10/AC11/AC12 are not browser-observable on this host — they need real restic/rsync/Docker volumes (restic is not on PATH on this Windows box → `WinError 2` when listing snapshots) and are already PASS in the unit suite.

### Per-AC results
- **AC1: PASS** — Add Project → unchecked "Back up database": the entire CONNECTION section hid (`#sec_conn` computed `display:none`), added one Volume path `media_vol`, saved → toast "Project added". Persisted `include_db=false`, exactly one `backup_paths` entry `{source:volume, value:media_vol}`, `connection_string=null`. Screenshot: `C:/Users/nithi/AppData/Local/Temp/bkp1-ac1-form.png`.
- **AC2: PASS** — kept "Back up database" checked, filled a connection string, added a Host path + a Volume path, saved. Persisted `include_db=true` with exactly two `backup_paths` entries (host + volume). Reopened via Edit: DB still checked, both path rows re-rendered with their values. Screenshot: `bkp1-ac2-edit-reopen.png`.
- **AC5: PASS (save-time, UI)** — added a Host path outside `HOST_BACKUP_ROOT`; Save was rejected with a clear toast "Path must be under <root>: <path>", the slide-over stayed open, and nothing persisted (detail → HTTP 404). Frontend surfaces the server's 400 `detail`. Screenshot: `bkp1-ac5-reject.png`.
- **AC9: PASS** — restore modal opens with scope defaulting to "Everything"; Restore button disabled for empty / partial / extra-char / wrong-case input; enables only on an exact project-name match; all three scope radios (all/db/files) selectable; clicking Restore resolves the chosen scope (verified `{"scope":"files"}`) and closes. Reopen resets scope→all, clears input, re-disables. Screenshots: `bkp1-ac9-initial.png` (disabled), `bkp1-ac9-enabled.png` (enabled).
- AC3, AC4, AC6, AC7, AC8, AC10, AC11, AC12: **NOT VERIFIABLE (browser)** on this host — require live restic/rsync/Docker volumes (restic absent → `WinError 2`). Data-layer logic for these is PASS in the unit suite; run them via the container (`docker compose -f docker-compose.local.yml up`) for end-to-end confirmation.

### Issues found
- **[ENV, not an app defect] A stale dev server was serving old backend code on the documented URL (http://127.0.0.1:8100).** The bare uvicorn process on :8100 (PID 32268, started 09:32) predates the BKP-1 edits to `app/main.py` (11:37) and `app/backup_paths.py` (11:33) and was launched without `--reload`, so it served pre-BKP-1 Python: project-detail responses omitted `include_db`/`backup_paths`/`backup_excludes` and its SQLite DB lacked the new columns. The frontend (app.js/forms.js/index.html) IS current because static files are read from disk per request — so the form behavior was real, but persistence observed against :8100 was wrong. Repro: `curl -u admin:admin http://127.0.0.1:8100/api/projects/<id>` → no new fields. **Fix: restart the :8100 uvicorn against the current code** (or run with `--reload`). I worked around it by starting a fresh server on :8101 with current code + a clean DB, where all ACs above passed cleanly.
- **[Test-harness artifact, not an app defect] Embedded-credentials auth breaks in-page `fetch`.** Navigating with `http://admin:admin@127.0.0.1:8100/` (creds in URL) makes the page's relative `fetch('/api/...')` throw `TypeError: Request cannot be constructed from a URL that includes credentials`, which surfaced as "Failed to save project" in the UI. This is purely how the headless browser was authenticated. Workaround: set an `Authorization: Basic` request header and navigate to the clean URL — fetches then return 200. Not reproducible for a real user (browsers send Basic auth via the credential cache, not the document URL).

### Servers started by this QA run (still running)
- `python -m uvicorn app.main:app --host 127.0.0.1 --port 8101` (fresh, current code; DB `local-data/qa-bkp1.db`, `HOST_BACKUP_ROOT=local-data/srv`, `admin/admin`). QA projects were deleted; DB is empty. Log: `local-data/qa-uvicorn-8101.log`. **I cannot stop it (Windows taskkill is disallowed) — please stop it from your terminal.**
- The pre-existing :8100 uvicorn (PID 32268, NOT started by this run, serves stale code) is also still running.

### Viewports tested
- Desktop 1280x800 (all flows), Tablet 768x1024 (form layout check — backup-contents section renders and is reachable).

### Security Audit — 2026-06-22 12:07 — Verdict: FINDINGS (0 critical/high)

## Security Audit: BKP-1

**Verdict:** FINDINGS

Audited the uncommitted working-tree changes: `app/backup_paths.py` (NEW), `app/main.py`, `app/static/{forms.js,app.js,styles.css}`, `app/templates/index.html`, `docker-compose.yml`, `docker-compose.local.yml`. New attack surface this story adds: (1) the `backup_paths`/`include_db`/`backup_excludes` fields on create/update project, (2) the `scope` param on the restore endpoint, (3) the path-resolution allow-list, (4) a new file-restore code path that runs `rsync --delete`, (5) a new RW host bind-mount.

### Critical / High
- None.

### Medium / Low
- [app/main.py:651-660] **Legacy WordPress restore target is not re-validated through the allow-list before `rsync --delete`.** `_restore_files()` re-resolves every *new* per-spec path through `backup_paths.resolve()` (good), but the legacy WP branch builds `uploads_target = /var/lib/docker/volumes/{wp_resource_id}_wp-uploads/_data` by raw string interpolation and passes it straight to `_rsync_into_target()` (which does `mkdir -p` + `rsync -a --delete target/`). `wp_resource_id` is free-form, server-side-unvalidated operator input (`ProjectPayload.wp_resource_id`, persisted verbatim at main.py:783/825). Attack path: an authenticated operator (or anyone who can reach the create/update endpoints) sets `wp_resource_id` to a value containing `../` (e.g. `../../../../srv/x` → target `/var/lib/docker/volumes/../../../../srv/x_wp-uploads/_data`) and triggers a `files`/`all` restore, causing `rsync --delete` to mirror restored content onto a directory outside the Docker volumes root. Severity held to Low/Medium because (a) it requires authenticated access to a single-user admin tool, (b) the same trust in `wp_resource_id` existed pre-feature, and (c) `_data`/`_wp-uploads` suffixes constrain the final segment. Fix: route `uploads_target` through `backup_paths._ensure_under(uploads_target, VOLUME_ROOT)` (or `resolve({"source":"volume","value": f"{wp_resource_id}_wp-uploads"})`) before the rsync, exactly as the per-spec branch already does.
- [docker-compose.yml:34 / docker-compose.local.yml] **`${HOST_BACKUP_ROOT:-/srv}` bind-mounted read-WRITE 1:1.** The container can read AND (via `rsync --delete` on file restore) overwrite/delete anything under the configured root on the host. This is the story's locked decision #1 and is gated by the allow-list + typed-confirm, so it is by-design, but it widens blast radius: combined with the pre-existing `/var/run/docker.sock` and `/var/lib/docker/volumes:rw` mounts, a path-mapping bug (see finding above) becomes host data loss. Fix/mitigation: mount read-only for the backup path and only mount RW where restore truly needs it, or document the RW `/srv` as a deliberate, audited risk.

### Reviewed and acceptable
- [app/backup_paths.py:23-57] Path allow-list (`_ensure_under` + `resolve`). Verified against `../` traversal, absolute-path injection in volume `value` (`os.path.join` discards the `VOLUME_ROOT` prefix, but realpath + `startswith(root + os.sep)` then rejects it), prefix-confusion (`/srvmalicious` rejected because it lacks the trailing separator), and symlink escape (`realpath` resolves symlinks before the check). Applied to BOTH host and volume branches. Correct.
- [app/main.py:411,432,615 etc.] All `subprocess.run` calls are list-form, no `shell=True`. New `--exclude` flags are emitted as `["--exclude", e]` so an exclude value starting with `-` is consumed as the option argument, not a new flag — no argv-option injection. Resolved source paths are absolute (post-realpath) so they cannot be mistaken for options. Safe.
- [app/static/app.js:347-349] `restoreDialog()` injects `snapId` and `project.name` into `#restoreBody` via `innerHTML` but both are wrapped in `esc()`; `#restoreName` uses `textContent`. No XSS. The snapshot "Contents" render (app.js:236-241) escapes `paths` via `esc()`. No new XSS sink introduced.
- [app/main.py:758,799,938] New/modified endpoints (`create_project`, `update_project`, `restore_snapshot`) all carry `Depends(verify_auth)` (constant-time Basic-auth check at :322). No new unauthenticated surface; restore `scope` is strictly allow-listed to `all|db|files` with a 400 otherwise.
- [app/main.py:308-309] `_hydrate_project` wraps `json.loads(backup_paths)` in try/except and coerces `include_db` to bool — malformed stored JSON degrades to `[]` rather than crashing the scheduler. Acceptable.
- Dependencies: no `requirements.txt` change; no new runtime packages. A06 has no new surface. `tests/` is new but test-only scaffolding.

### OWASP / STRIDE pass notes
- Skipped A02 (no new crypto; plaintext DB-password storage in SQLite is pre-existing, not touched), A09 (logging unchanged in shape; backup/restore log to the same per-project log file), A10 (no user-controlled outbound fetch added — `connection_string` SSRF-to-internal-DB is pre-existing behavior, not introduced here). A07 reviewed: single-user model unchanged. A08: no deserialization of attacker data beyond the guarded `json.loads` above.
- STRIDE on the two new entry points (project create/update with `backup_paths`; restore with `scope`): Tampering/EoP via path values is the only real lever, mitigated by the allow-list except for the WP-legacy gap noted above; DoS via a huge configured directory is bounded by `BACKUP_TIMEOUT` (default 1800s); Repudiation is partial (restore writes to the per-project log, but no per-actor audit since it is single-user).

### Pre-existing (not introduced by this story)
- [app/main.py:534] The backup-side WP uploads path uses the same unvalidated `wp_resource_id` interpolation; it only feeds a read (`restic backup`), so lower risk than the restore-side `--delete`, but it shares the root cause. Mentioned for completeness; pre-existing.
