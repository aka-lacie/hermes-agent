#!/usr/bin/env python3
"""Offline dev2 delivery contract migration. Dry-run by default; never opens a DB.

Run with a provisioned interpreter, while all store writers are stopped. Unknown
fields and enabled states are preserved unless --pause-all is explicit. Ambiguous
addresses require --targets (cron IDs / webhook names mapped to canonical targets).
Backups are exclusive, private and never overwritten. A second apply is a no-op.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class MigrationError(ValueError):
    pass


def _read_json(path: Path):
    if path.is_symlink() or not path.is_file():
        raise MigrationError(f"not a regular input file: {path.name}")
    raw = path.read_bytes()
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MigrationError("duplicate JSON key")
            result[key] = value
        return result
    return raw, json.loads(raw, object_pairs_hook=unique)


def _session(target):
    from gateway.delivery_targets import parse_delivery_target
    from gateway.session_selectors import validate_selector
    if not isinstance(target, dict) or target.get("kind") != "session":
        raise MigrationError("legacy internal delivery requires a session target")
    result = parse_delivery_target(target)
    if "selector" in result:
        validate_selector(result["selector"])
    if "attach" not in target or result.get("wake_policy") != "immediate":
        raise MigrationError("migration requires explicit attach and immediate wake")
    return result


def _dm_target(platform, chat_id, attach=False):
    from gateway.config import Platform
    Platform(platform)  # Validate the exact token; do not normalize identifiers.
    if not isinstance(chat_id, (str, int)) or isinstance(chat_id, bool):
        raise MigrationError("invalid conversation address")
    if not str(chat_id) or any(c in str(chat_id) for c in "{}\n\r:"):
        raise MigrationError("ambiguous or templated address: provide --targets")
    return _session({"kind": "session", "selector": f"{platform}:dm:{chat_id}",
                     "attach": attach, "wake_policy": "immediate"})


def infer_target(record, domain):
    """Only context-free addresses: never load ambient profile configuration."""
    attach = record.get("attach_to_session", False)
    if not isinstance(attach, bool):
        raise MigrationError("attach_to_session must be boolean")
    deliver = record.get("deliver", "origin" if domain == "cron" else "log")
    if domain == "cron" and deliver == "origin":
        origin = record.get("origin") or {}
        if origin.get("session_key"):
            return _session({"kind": "session", "selector": origin["session_key"],
                             "attach": attach, "wake_policy": "immediate"})
        if origin.get("thread_id") or origin.get("chat_type", "dm") != "dm":
            raise MigrationError("group/thread origin requires an approved --targets mapping")
        return _dm_target(origin.get("platform"), origin.get("chat_id"), attach)
    if domain == "cron":
        raise MigrationError("non-origin cron address requires an approved --targets mapping")
    extra = record.get("deliver_extra") or {}
    if extra.get("thread_id") or extra.get("message_thread_id") or extra.get("chat_type", "dm") != "dm":
        raise MigrationError("group/thread webhook requires an approved --targets mapping")
    if extra.get("chat_id") is not None:
        return _dm_target(deliver, extra["chat_id"], attach)
    from gateway.config import Platform
    if deliver in ("log", "github_comment"):
        raise MigrationError("internal delivery cannot target log or github_comment")
    Platform(deliver)
    return _session({"kind": "session", "selector": f"home:{deliver}",
                     "attach": attach, "wake_policy": "immediate"})


def convert_document(document, domain, targets=None, pause_all=False):
    """Return the converted complete document and aggregate receipt, without I/O."""
    result = deepcopy(document)
    targets = targets or {}
    if not isinstance(targets, dict):
        raise MigrationError("target mapping must be an object")
    if domain == "cron":
        records = result.get("jobs") if isinstance(result, dict) else result
        if not isinstance(records, list):
            raise MigrationError("cron document must be a list or an object with jobs list")
        entries = [(r.get("id") if isinstance(r, dict) else None, r) for r in records]
    elif domain == "webhook" and isinstance(result, dict):
        entries = list(result.items())
    else:
        raise MigrationError("webhook document must map route names to records")
    ids = [key for key, _ in entries]
    if any(not isinstance(key, str) or not key for key in ids) or len(ids) != len(set(ids)):
        raise MigrationError("missing or duplicate record identifier")
    if set(targets) - set(ids):
        raise MigrationError("target mapping names an unknown record")
    converted = paused = 0
    for key, record in entries:
        if not isinstance(record, dict):
            raise MigrationError("record must be an object")
        mode = record.get("delivery_mode")
        internal = mode == "internal_turn" or (domain == "cron" and record.get("job_type") == "reminder" and mode is None)
        if key in targets and not internal and record.get("delivery_target") != targets[key]:
            raise MigrationError("target mapping must not change a non-legacy record")
        if internal:
            original = deepcopy(record)
            target = targets.get(key, record.get("delivery_target"))
            if target is None:
                target = infer_target(record, domain)
            record["delivery_target"] = _session(target)
            # Keep the compatibility mode: webhook internal_turn also means final-only
            # handoff, whereas a bare canonical session target may admit interim notices.
            converted += int(record != original)
        if pause_all and record.get("enabled") is not False:
            record["enabled"] = False
            paused += 1
    return result, {"records": len(entries), "converted": converted, "paused": paused}


def _publish(path, data):
    fd, name = tempfile.mkstemp(prefix=".delivery-migration-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def migrate_files(jobs, subscriptions, *, targets=None, apply=False, pause_all=False):
    """Preflight BOTH files before backup/publication; rollback partial publication on error.

    Writers must be stopped. Byte comparisons detect changes since preflight, but are
    not a substitute for the operator's independent stopped-writer barrier.
    """
    paths = [Path(jobs), Path(subscriptions)]
    if paths[0].resolve() == paths[1].resolve():
        raise MigrationError("stores must be distinct")
    plans, receipt = [], {}
    for domain, path in zip(("cron", "webhook"), paths):
        raw, document = _read_json(path)
        updated, stats = convert_document(document, domain, (targets or {}).get(domain), pause_all)
        changed = updated != document
        receipt[domain] = {**stats, "changed": changed}
        if changed:
            data = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode()
            backup = path.with_name(path.name + ".pre-dev3-parity")
            if backup.exists() or backup.is_symlink():
                raise MigrationError(f"backup already exists for changed input: {path.name}")
            plans.append((path, raw, data, backup))
    if apply:
        for path, raw, _, _ in plans:
            if path.is_symlink() or path.read_bytes() != raw:
                raise MigrationError("input changed during preflight")
        for _, raw, _, backup in plans:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        published = []
        try:
            for path, raw, data, _ in plans:
                if path.is_symlink() or path.read_bytes() != raw:
                    raise MigrationError("input changed before publication")
                _publish(path, data)
                published.append((path, raw))
                if path.read_bytes() != data:
                    raise MigrationError("published readback mismatch")
        except BaseException:
            for path, raw in reversed(published):
                _publish(path, raw)
            raise
    receipt["applied"] = apply
    receipt["changed_files"] = len(plans)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cron-jobs", type=Path, required=True)
    parser.add_argument("--webhook-subscriptions", type=Path, required=True)
    parser.add_argument("--targets", type=Path, help="JSON {cron: {id: target}, webhook: {name: target}}")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--pause-all", action="store_true")
    parser.add_argument("--allow-live-stores", action="store_true", help="operator assertion: all writers stopped and snapshot verified")
    args = parser.parse_args(argv)
    from hermes_constants import get_hermes_home
    home = get_hermes_home().resolve()
    live = {home / "cron/jobs.json", home / "webhook_subscriptions.json"}
    if any(path.resolve() in live for path in (args.cron_jobs, args.webhook_subscriptions)) and not args.allow_live_stores:
        parser.error("live stores refused: use offline copies; --allow-live-stores requires a separately authorized window")
    targets = _read_json(args.targets)[1] if args.targets else {}
    if not isinstance(targets, dict) or set(targets) - {"cron", "webhook"}:
        parser.error("invalid targets document")
    try:
        receipt = migrate_files(args.cron_jobs, args.webhook_subscriptions, targets=targets,
                                apply=args.apply, pause_all=args.pause_all)
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
