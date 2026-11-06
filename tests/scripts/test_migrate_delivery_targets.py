"""Offline delivery-store migration never touches a session DB or platform sender."""
import json
from pathlib import Path

import pytest

from scripts import migrate_delivery_targets as migration


TARGET = {"kind": "session", "selector": "discord:dm:123", "attach": False, "wake_policy": "immediate"}


def stores(tmp_path):
    jobs = tmp_path / "jobs.json"
    hooks = tmp_path / "subscriptions.json"
    jobs.write_text(json.dumps({"version": 9, "opaque": {"keep": True}, "jobs": [
        {"id": "a", "job_type": "reminder", "delivery_mode": "internal_turn", "deliver": "origin",
         "origin": {"platform": "discord", "chat_id": "123", "chat_type": "dm"},
         "prompt": "  literal\n", "enabled": True, "next_run_at": "unchanged", "repeat": {"times": 4}},
        {"id": "b", "deliver": "discord:456", "enabled": False}]}))
    hooks.write_text(json.dumps({"route": {"delivery_mode": "internal_turn", "deliver": "discord",
                                         "deliver_extra": {"chat_id": "123"}, "secret": "test-only",
                                         "fallback_providers": [{"provider": "test", "model": "test"}]}}))
    return jobs, hooks


def test_apply_preserves_fields_backups_and_second_run_bytes(tmp_path):
    jobs, hooks = stores(tmp_path)
    originals = {p: p.read_bytes() for p in (jobs, hooks)}
    before = {p: json.loads(raw) for p, raw in originals.items()}
    preview = migration.migrate_files(jobs, hooks)
    assert preview["changed_files"] == 2
    assert all(p.read_bytes() == raw for p, raw in originals.items())
    assert not list(tmp_path.glob("*.pre-dev3-parity"))
    receipt = migration.migrate_files(jobs, hooks, apply=True)
    assert receipt["cron"]["converted"] == receipt["webhook"]["converted"] == 1
    for p, raw in originals.items():
        backup = p.with_name(p.name + ".pre-dev3-parity")
        assert backup.read_bytes() == raw
        assert backup.stat().st_mode & 0o777 == 0o600
        assert p.stat().st_mode & 0o777 == 0o600
    after_jobs, after_hooks = json.loads(jobs.read_bytes()), json.loads(hooks.read_bytes())
    old_job = before[jobs]["jobs"][0]
    assert after_jobs["jobs"][0] == {**old_job, "delivery_target": TARGET}
    assert after_jobs["jobs"][1] == before[jobs]["jobs"][1]
    old_hook = before[hooks]["route"]
    assert after_hooks["route"] == {**old_hook, "delivery_target": TARGET}
    first = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (jobs, hooks)}
    second = migration.migrate_files(jobs, hooks, apply=True)
    assert second["changed_files"] == 0
    assert second["cron"]["converted"] == 0
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == saved for p, saved in first.items())


def test_preflight_both_before_any_write(tmp_path):
    jobs, hooks = stores(tmp_path)
    hooks.write_text('{"route": {"delivery_mode": "internal_turn", "deliver": "log"}}')
    original = jobs.read_bytes()
    with pytest.raises(migration.MigrationError):
        migration.migrate_files(jobs, hooks, apply=True)
    assert jobs.read_bytes() == original
    assert not list(tmp_path.glob("*.pre-dev3-parity"))


def test_rollback_partial_publication_keeps_original_backups(tmp_path, monkeypatch):
    jobs, hooks = stores(tmp_path)
    original = {p: p.read_bytes() for p in (jobs, hooks)}
    publish = migration._publish
    def fail_second(path, data):
        if path == hooks:
            raise OSError("injected failure")
        publish(path, data)
    monkeypatch.setattr(migration, "_publish", fail_second)
    with pytest.raises(OSError):
        migration.migrate_files(jobs, hooks, apply=True)
    assert all(p.read_bytes() == raw for p, raw in original.items())
    assert all(p.with_name(p.name + ".pre-dev3-parity").read_bytes() == raw for p, raw in original.items())


def test_explicit_targets_and_pause_are_separate(tmp_path):
    jobs, hooks = stores(tmp_path)
    data = json.loads(jobs.read_bytes())
    data["jobs"][0]["origin"]["chat_type"] = "group"
    jobs.write_text(json.dumps(data))
    with pytest.raises(migration.MigrationError, match="mapping"):
        migration.migrate_files(jobs, hooks)
    selected = {**TARGET, "selector": "discord:group:123", "attach": True}
    migration.migrate_files(jobs, hooks, targets={"cron": {"a": selected}}, apply=True, pause_all=True)
    changed = json.loads(jobs.read_bytes())
    assert changed["jobs"][0]["delivery_target"] == selected
    assert all(j["enabled"] is False for j in changed["jobs"])
    assert json.loads(hooks.read_bytes())["route"]["enabled"] is False


@pytest.mark.parametrize("target", [
    {**TARGET, "kind": "platform"}, {**TARGET, "attach": "false"},
    {**TARGET, "wake_policy": "next_turn"}, {k: v for k, v in TARGET.items() if k != "attach"}])
def test_refuse_unsafe_target(target):
    with pytest.raises(ValueError):
        migration.convert_document([{"id": "a", "delivery_mode": "internal_turn"}], "cron", {"a": target})


def test_bad_or_duplicate_records_refused():
    with pytest.raises(migration.MigrationError):
        migration.convert_document([{"id": "a"}, {"id": "a"}], "cron")
    with pytest.raises(migration.MigrationError):
        migration.convert_document({"subscriptions": []}, "webhook")


def test_existing_backup_not_overwritten(tmp_path):
    jobs, hooks = stores(tmp_path)
    backup = jobs.with_name(jobs.name + ".pre-dev3-parity")
    backup.write_bytes(b"old evidence")
    with pytest.raises(migration.MigrationError):
        migration.migrate_files(jobs, hooks, apply=True)
    assert backup.read_bytes() == b"old evidence"


def test_live_guard_before_read(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    with pytest.raises(SystemExit) as exc:
        migration.main(["--cron-jobs", str(tmp_path / "cron/jobs.json"),
                        "--webhook-subscriptions", str(tmp_path / "webhook_subscriptions.json")])
    assert exc.value.code == 2


def test_cli_offline_receipt_and_home_alias(tmp_path, capsys):
    jobs, hooks = stores(tmp_path)
    hooks.write_text(json.dumps({"route": {"delivery_mode": "internal_turn", "deliver": "discord"}}))
    assert migration.main(["--cron-jobs", str(jobs), "--webhook-subscriptions", str(hooks), "--apply"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["changed_files"] == 2
    assert json.loads(hooks.read_bytes())["route"]["delivery_target"]["selector"] == "home:discord"
