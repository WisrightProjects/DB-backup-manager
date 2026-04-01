# Backup Manager — Multi-Project & Multi-VPS Plan

## Vision

A self-hosted, open-source backup management dashboard that handles multiple projects across multiple servers — like pgbackweb, but for any database type and file backups, powered by Restic + S3.

---

## Current State (v1)

- Single project: Drive EV WordPress (MariaDB + uploads)
- Single VPS: 72.61.170.5
- Single Restic repo on Contabo S3
- FastAPI + vanilla HTML/JS frontend
- Basic auth
- Manual backup, download, restore, log viewer
- System cron for scheduled backups (`backup-prod.sh`)

---

## Phase 2: Multi-Project (Single VPS)

### Goal
Manage multiple backup projects from one UI — each with its own DB type, schedule, retention, and restore capability.

### Supported Backup Types
| Type | Source | Dump Method |
|------|--------|-------------|
| MariaDB / MySQL | Docker container | `mariadb-dump` / `mysqldump` via `docker exec` |
| PostgreSQL | Docker container | `pg_dump` via `docker exec` |
| MongoDB | Docker container | `mongodump` via `docker exec` |
| Files / Volumes | Host path or Docker volume | Direct Restic backup |
| WordPress | Combo: DB container + uploads volume | DB dump + file backup |

### Project Configuration
Each project stores:
```json
{
  "id": "uuid",
  "name": "Drive EV Prod",
  "type": "wordpress",
  "server_id": "local",
  "db": {
    "engine": "mariadb",
    "container": "ac0k8s488okksoo4cggo4wc4",
    "user": "mariadb",
    "password": "encrypted-value",
    "database": "driveev_prod"
  },
  "files": [
    "/var/lib/docker/volumes/xxx_wp-uploads/_data"
  ],
  "restic": {
    "repo": "s3:https://eu2.contabostorage.com/wisright-backups",
    "password_file": "/opt/backups/.restic-pass",
    "tags": ["prod", "drive-ev"]
  },
  "schedule": "0 3 * * *",
  "retention": {
    "keep_daily": 7,
    "keep_weekly": 4,
    "keep_monthly": 6
  },
  "enabled": true
}
```

### Storage for Config
- **SQLite** database (`/data/backup-manager.db`)
- Tables: `projects`, `servers`, `backup_history`, `settings`
- Mounted as a Docker volume for persistence
- Passwords encrypted at rest using Fernet (key from env var)

### S3 / Restic Strategy
- **One Restic repo per S3 bucket** (shared across projects)
- Projects separated by **Restic tags** (e.g., `project:drive-ev`, `project:client-x`)
- Option to use separate repos if needed (configurable per project)
- Default repo + credentials set in global settings; projects can override

### Built-in Scheduler
- Replace system cron with **APScheduler** running inside the app
- Each project's schedule stored in DB
- Scheduler reads from DB on startup, reloads on config change
- Benefits: no system cron dependency, visible in UI, survives container restarts
- Shows next scheduled run time in dashboard

### UI Changes
- **Dashboard**: grid/list of all projects with status badges (healthy/stale/failing)
- **Project detail page**: snapshots, logs, config, manual actions
- **Add project wizard**: step-by-step form (pick type → enter connection → set schedule → test → save)
- **Settings page**: default S3 credentials, global retention defaults, encryption key status
- **Sidebar navigation**: Projects list, Dashboard, Settings, Logs

### API Endpoints (new/changed)
```
GET    /api/projects                     — list all projects
POST   /api/projects                     — create project
GET    /api/projects/{id}                — get project details
PUT    /api/projects/{id}                — update project
DELETE /api/projects/{id}                — delete project
POST   /api/projects/{id}/test           — test connection (verify container/DB accessible)
GET    /api/projects/{id}/snapshots      — list snapshots for project
POST   /api/projects/{id}/backup         — trigger manual backup
POST   /api/projects/{id}/restore/{snap} — restore a snapshot
GET    /api/projects/{id}/download/{snap}— download a snapshot
GET    /api/projects/{id}/logs           — project-specific logs
GET    /api/dashboard                    — aggregated stats for all projects
GET    /api/settings                     — global settings
PUT    /api/settings                     — update global settings
```

### Database Schema
```sql
CREATE TABLE servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'local',  -- 'local' or 'remote'
    host TEXT,
    agent_url TEXT,
    agent_api_key TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    server_id TEXT REFERENCES servers(id),
    type TEXT NOT NULL,              -- mariadb, postgres, mongodb, files, wordpress
    config JSON NOT NULL,            -- full config blob (db creds, paths, etc.)
    restic_repo TEXT,
    restic_password TEXT,            -- encrypted
    restic_tags JSON,
    schedule TEXT,                   -- cron expression
    retention JSON,                  -- {keep_daily, keep_weekly, keep_monthly}
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE backup_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT REFERENCES projects(id),
    snapshot_id TEXT,
    status TEXT NOT NULL,            -- success, failed, running
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    size_bytes INTEGER,
    output TEXT,
    error TEXT
);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

---

## Phase 3: Multi-VPS (Central Manager + Remote Agents)

### Architecture
```
┌─────────────────────────────────────────────────┐
│                  VPS-1 (Central)                │
│  ┌───────────────────────────────────────────┐  │
│  │          Backup Manager (UI)              │  │
│  │  - Dashboard, project config, scheduling  │  │
│  │  - Restic operations (store to S3)        │  │
│  │  - Manages local + remote projects        │  │
│  └──────────────┬────────────────────────────┘  │
│                 │                                │
│    Local Docker │  HTTPS (agent API)             │
│    socket       │                                │
└─────────────────┼────────────────────────────────┘
                  │
        ┌─────────┴─────────┐
        │                   │
┌───────▼──────┐   ┌───────▼──────┐
│   VPS-2      │   │   VPS-3      │
│ ┌──────────┐ │   │ ┌──────────┐ │
│ │  Agent   │ │   │ │  Agent   │ │
│ │  (small  │ │   │ │  (small  │ │
│ │  Docker  │ │   │ │  Docker  │ │
│ │  container││   │ │  container││
│ └──────────┘ │   │ └──────────┘ │
│  - Docker    │   │  - Docker    │
│    socket    │   │    socket    │
│  - DB access │   │  - DB access │
│  - File read │   │  - File read │
└──────────────┘   └──────────────┘
```

### Agent Design
A minimal Docker container that runs on each remote VPS.

**Agent responsibilities:**
- List running Docker containers (for project setup)
- Execute DB dumps (`docker exec` → dump → stream back)
- Read/stream files from host paths or Docker volumes
- Import DB dumps for restore
- Write files back for restore
- Health check endpoint

**Agent does NOT:**
- Store backups (central manager handles Restic + S3)
- Manage schedules (central manager triggers)
- Have any UI

**Agent API:**
```
GET    /health                          — agent health + version
GET    /containers                      — list running containers
POST   /dump                            — dump a database, returns file stream
POST   /restore                         — receive + import a dump file
GET    /files?path=/some/path           — stream files/directory as tar
POST   /files?path=/some/path           — receive + extract tar to path
```

**Agent security:**
- API key authentication (header: `X-Agent-Key: <key>`)
- HTTPS only (via Traefik/Coolify)
- Agent key generated on deploy, configured in central manager
- Optional: IP allowlist (only accept requests from central VPS)

### Agent Docker Image
```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y docker.io rsync
# Minimal FastAPI app
COPY agent/ /app/
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
```

Deploy on each remote VPS via Coolify as a simple Docker resource.

### Backup Flow (Remote)
```
1. Scheduler triggers backup for "Client X Postgres on VPS-2"
2. Manager sends POST /dump to VPS-2 agent
   → Agent runs: docker exec <container> pg_dump ...
   → Agent streams SQL dump back to manager
3. Manager saves dump to temp file
4. Manager runs: restic backup --tag project:client-x <dump-file>
5. Manager records result in backup_history
6. Manager cleans up temp file
```

### Restore Flow (Remote)
```
1. User clicks "Restore" on snapshot in UI
2. Manager runs: restic restore <snapshot-id> → temp dir
3. Manager sends POST /restore to VPS-2 agent with dump file
   → Agent runs: docker exec <container> psql < dump.sql
4. If files: Manager sends POST /files with tar stream
   → Agent extracts to target path
5. Manager records result, cleans up
```

### Adding a New VPS
1. Deploy agent container on the new VPS via Coolify
2. In Backup Manager UI → Settings → Servers → "Add Server"
3. Enter: name, agent URL (e.g., `https://agent.vps3.example.com`), API key
4. Manager calls `/health` to verify connectivity
5. Manager calls `/containers` to show available containers
6. User creates projects linked to this server

---

## Phase 4: Nice-to-Haves (Future)

### Notifications
- **Webhook support**: Microsoft Teams, Slack, Discord
- **Email alerts**: via SMTP config
- Configurable per project: notify on failure, notify on success, notify on stale
- Global notification settings + per-project overrides

### Monitoring & Metrics
- Backup duration trends (chart)
- Storage usage per project over time
- Failed backup streak alerts
- S3 bucket usage / cost estimate

### User Management
- Multiple user accounts with roles (admin, viewer)
- Audit log (who triggered what)
- OAuth / SSO integration

### Backup Verification
- Automatic test restore after backup (restore to temp, verify DB integrity)
- Checksum verification
- "Last verified" timestamp per project

### Import / Migration
- Import existing Restic repos
- Import from other backup tools
- Export project config for backup/migration of the manager itself

### CLI Tool
- `backup-cli projects list`
- `backup-cli backup <project-name>`
- Useful for scripting and CI/CD pipelines

---

## Tech Stack Summary

| Component | Technology |
|-----------|-----------|
| Backend | Python, FastAPI |
| Frontend | Vanilla HTML/CSS/JS (no framework, keeps it light) |
| Database | SQLite (config + history) |
| Backup Engine | Restic |
| Storage | S3-compatible (Contabo, AWS, MinIO, etc.) |
| Scheduler | APScheduler |
| Auth | Basic auth (Phase 2), user accounts (Phase 4) |
| Containerization | Docker, Docker Compose |
| Deployment | Coolify |
| Agent Communication | HTTPS + API key |
| Password Encryption | Fernet (cryptography library) |

---

## Implementation Priority

```
Phase 2 (Multi-Project, Single VPS)
├── SQLite database + models
├── Project CRUD API + UI
├── DB dump engines (MariaDB, Postgres, MongoDB)
├── Built-in scheduler (APScheduler)
├── Per-project snapshots, restore, download
├── Dashboard with all projects overview
├── Password encryption
└── Migrate current Drive EV config into DB

Phase 3 (Multi-VPS)
├── Agent Docker image
├── Agent API (dump, restore, files, containers)
├── Server management in UI
├── Remote backup/restore flows
└── Agent health monitoring

Phase 4 (Nice-to-Haves)
├── Webhook notifications (Teams, Slack)
├── Email alerts
├── Backup verification
├── User management
├── Metrics / charts
└── CLI tool
```

---

*Last updated: 2026-04-01*
