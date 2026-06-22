"""Test bootstrap for BKP-1.

Sets the env the app reads at *import time* (it runs init_db() and starts the
scheduler on import) BEFORE app.main is imported, so the test DB lives in a
throwaway temp file and HOST_BACKUP_ROOT is a known value the AC5/AC4 tests
can reason about.
"""
import os
import tempfile

# Must be set before `from app import main` runs anywhere.
_TMP_DB = os.path.join(tempfile.gettempdir(), "bkp1-test-projects.db")
os.environ.setdefault("DB_PATH", _TMP_DB)
os.environ.setdefault("HOST_BACKUP_ROOT", "/srv")
os.environ.setdefault("AUTH_USERNAME", "admin")
os.environ.setdefault("AUTH_PASSWORD", "admin")
os.environ.setdefault("RESTIC_PASSWORD", "localtest")
