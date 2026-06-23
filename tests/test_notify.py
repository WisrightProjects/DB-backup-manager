"""Tests for backup email notifications (notify service + persistence).

Covers the per-project notify_email round-trip through the create/update
endpoints and the pure gating logic in app.services.notify (NOTIFY_ON filter,
recipient resolution, SMTP-disabled no-op). The actual SMTP wire send is mocked
— these tests never open a socket.
"""
import os

import pytest

from app import backup_paths
from app import main
from app.services import notify


# Reuse the persistence harness conventions from test_bkp1.
@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db_file = tmp_path / "projects.db"
    monkeypatch.setattr(main, "DB_PATH", str(db_file))
    main.init_db()
    return str(db_file)


def _payload(**overrides):
    data = dict(
        name="Notify Project",
        db_engine="postgres",
        project_type="database",
        connection_type="connection_string",
        connection_string="postgresql://u:p@localhost:5432/db",
        restic_tag="notify-tag",
        storage_type="local",
    )
    data.update(overrides)
    return main.ProjectPayload(**data)


# ---------------------------------------------------------------------------
# Persistence: notify_email survives create + update
# ---------------------------------------------------------------------------
class TestNotifyEmailPersistence:
    def test_create_persists_notify_email(self, fresh_db):
        created = main.create_project(
            _payload(notify_email="a@x.com, b@x.com"), user="admin")
        proj = main.get_project_or_404(created["id"])
        assert proj["notify_email"] == "a@x.com, b@x.com"

    def test_update_changes_notify_email(self, fresh_db):
        created = main.create_project(_payload(notify_email="a@x.com"), user="admin")
        main.update_project(
            created["id"], _payload(notify_email="changed@x.com"), user="admin")
        proj = main.get_project_or_404(created["id"])
        assert proj["notify_email"] == "changed@x.com"

    def test_default_notify_email_is_empty(self, fresh_db):
        created = main.create_project(_payload(), user="admin")
        proj = main.get_project_or_404(created["id"])
        assert (proj["notify_email"] or "") == ""


# ---------------------------------------------------------------------------
# Gating: NOTIFY_ON filter
# ---------------------------------------------------------------------------
class TestShouldSend:
    def test_all_sends_both(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_ON", "all")
        assert notify._should_send(True) is True
        assert notify._should_send(False) is True

    def test_failure_only(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_ON", "failure")
        assert notify._should_send(False) is True
        assert notify._should_send(True) is False

    def test_success_only(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_ON", "success")
        assert notify._should_send(True) is True
        assert notify._should_send(False) is False

    def test_default_is_all(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_ON", raising=False)
        assert notify._should_send(True) is True
        assert notify._should_send(False) is True


# ---------------------------------------------------------------------------
# Recipients: per-project first, global NOTIFY_EMAIL fallback
# ---------------------------------------------------------------------------
class TestRecipients:
    def test_project_overrides_global(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_EMAIL", "global@x.com")
        assert notify._recipients({"notify_email": "p1@x.com, p2@x.com"}) == \
            ["p1@x.com", "p2@x.com"]

    def test_falls_back_to_global(self, monkeypatch):
        monkeypatch.setenv("NOTIFY_EMAIL", "g1@x.com, g2@x.com")
        assert notify._recipients({"notify_email": ""}) == ["g1@x.com", "g2@x.com"]

    def test_empty_when_nothing_configured(self, monkeypatch):
        monkeypatch.delenv("NOTIFY_EMAIL", raising=False)
        assert notify._recipients({}) == []


# ---------------------------------------------------------------------------
# send_backup_result: no-op / dispatch behaviour (SMTP mocked)
# ---------------------------------------------------------------------------
class TestSendBackupResult:
    def test_noop_when_smtp_host_unset(self, monkeypatch):
        monkeypatch.delenv("SMTP_HOST", raising=False)
        sent = []
        monkeypatch.setattr(notify, "_smtp_send", lambda m, r: sent.append(r))
        notify.send_backup_result({"notify_email": "a@x.com"}, True, "ok")
        assert sent == []

    def test_noop_when_no_recipients(self, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.delenv("NOTIFY_EMAIL", raising=False)
        sent = []
        monkeypatch.setattr(notify, "_smtp_send", lambda m, r: sent.append(r))
        notify.send_backup_result({"notify_email": ""}, True, "ok")
        assert sent == []

    def test_sends_to_project_recipients(self, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("NOTIFY_ON", "all")
        captured = {}
        monkeypatch.setattr(notify, "_smtp_send",
                            lambda msg, rcpts: captured.update(msg=msg, rcpts=rcpts))
        notify.send_backup_result(
            {"name": "Proj", "notify_email": "ops@x.com"}, False, "boom")
        assert captured["rcpts"] == ["ops@x.com"]
        assert "FAILED" in captured["msg"]["Subject"]

    def test_failure_filter_suppresses_success(self, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("NOTIFY_ON", "failure")
        sent = []
        monkeypatch.setattr(notify, "_smtp_send", lambda m, r: sent.append(r))
        notify.send_backup_result({"notify_email": "a@x.com"}, True, "ok")
        assert sent == []

    def test_smtp_error_is_swallowed(self, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("NOTIFY_ON", "all")

        def boom(msg, rcpts):
            raise RuntimeError("smtp down")
        monkeypatch.setattr(notify, "_smtp_send", boom)
        # Must not raise — a dead mailserver can never fail a backup.
        notify.send_backup_result({"notify_email": "a@x.com"}, True, "ok")
