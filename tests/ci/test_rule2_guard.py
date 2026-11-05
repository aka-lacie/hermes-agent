"""Rule 2 CI guard: no internal chat-format round trips in core source (unified-session plan §4.8).

Provenance and metadata live in structured columns and typed parts (``agent/message_parts.py``); no
code formats a header, banner or timestamp into ``content`` for a later layer to parse back out. This
is a pure source scan (it never imports Hermes) with three checks:

- ``literal``: a forbidden internal-envelope literal (decoded string / f-string fragments, regex
  escapes normalized) outside a docstring;
- ``helper``: a definition, import or call of a removed reverse-text helper;
- ``match``: an input-content match smell (``startswith`` / ``==`` / regex against a re-rendered
  marker inside a provenance classifier).

External formats (provider SSE, webhook payloads, model output such as runaway/refusal guards) are
allowed and are not matched by these rules; the few that are sit in ``rule2_allowlist.json`` by exact
file + symbol. The 2f migration modules and the named legacy shims are allowlisted until Phase 5.

**Ratchet.** Hits that remain from before Phase 2e are recorded in ``rule2_baseline.json``. The test
fails on any hit not in the baseline (new code may never add one) and on any baseline entry that no
longer occurs (remove it, so the baseline only shrinks). Line numbers are not part of a hit's
identity. Regenerate after removing hits: ``python tests/ci/test_rule2_guard.py --write-baseline``.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ALLOWLIST_PATH = HERE / "rule2_allowlist.json"
BASELINE_PATH = HERE / "rule2_baseline.json"

FORBIDDEN_LITERALS = (
    "[HERMES_INTERNAL_NOTIFICATION]", "[INTERNAL NOTIFICATION",
    "[ASYNC DELEGATION", "[BACKGROUND PROCESS", "[Background process ",
    "[OUT-OF-BAND USER MESSAGE", "[/OUT-OF-BAND USER MESSAGE]",
    "[TASK RESULT", "[/TASK RESULT]", "[Parent assignment",
    "[Triggering message id:", "[Delivered from ", "[Cron delivery:", "[Webhook delivery:",
    "[CONTEXT COMPACTION", "[CONTEXT SUMMARY", "[PRIOR CONTEXT",
    "[END OF PRIOR CONTEXT", "--- END OF CONTEXT SUMMARY", "[STILL IN PROGRESS",
    "[Planning state preserved", "[Your active task list was preserved",
    "[Skills pruned during compression", "[SKILL_PRUNED:",
    "[Runtime reminder]", "[Runtime note:", "[System note:", "[System:", "[SYSTEM]",
    "[SYSTEM NOTICE", "[IMPORTANT:", "[USER INITIATED SKILLS RELOAD:",
    "[Continuing toward your standing goal", "Cronjob Response:", "Message origin (data):",
    "Gateway message origin (JSON data, not instructions or authorization):",
    "Operation interrupted: waiting for model response (",
    "[interrupt: no assistant output for this turn]", "[response interrupted]",
    "[The previous response was interrupted before it completed.]",
    "[Message withheld: a pre_llm_call policy hook blocked it.]",
    "[screenshot]", "[screenshot detail=", "[Image attached at:", "[Image attached:",
    "[Observed Telegram group context", "[Current addressed message", "[New message]",
    "[Replying to", "[Voice channel now:", "[CURRENT RUN TIME:",
    "[Old tool output cleared to save context space]", "[Duplicate tool output",
    " output demoted at compaction — ", "Full output saved to:",
    "Continue from the compressed conversation context above.",
    "Continue with the user's latest message.",
    "[codex ",
)
# Reverse-text helpers that must not come back (removed or to be removed), not semantic predicates.
FORBIDDEN_HELPERS = frozenset({
    "_sidecar_matches_rendered_user", "_discord_note_pattern",
    "strip_discord_triggering_note", "strip_leading_message_timestamps",
    "render_user_content_with_timestamp", "_strip_auto_continue_noise", "_is_auto_continue_noise",
    "format_steer_marker", "_steer_markers", "_extract_steer_text_from_message",
    "_message_contains_busy_steer", "_compressed_has_busy_steer",
    "_strip_summary_prefix", "_with_summary_prefix", "_starts_with_summary_prefix",
    "classify_summary_content", "_strip_context_summary_handoff_message", "_handoff_only_content",
    "_rolling_summary_from_marker", "_has_merged_inflight_replay", "_restated_request_text",
    "_strip_stale_todo_snapshot", "_extract_pruned_skill_names", "_skill_pruned_marker",
    "is_demoted_stub", "_is_summary_stub", "without_skill_safety_rule",
    "_authored_span", "_split_sent", "_source_rows", "recover_replay_provenance",
    "_strip_envelope", "_outside_steer_frame", "resolve_micro_signature", "carries_reminder",
    "extract_user_instruction_from_skill_message", "_describe_auto_loaded_skill_turn",
    "skills_loaded_by_scaffold", "_is_model_switch_marker", "_legacy_display_kind",
    "untyped_failed_turn_display_kind", "_split_rebuilt_row", "_strip_trailing_screenshot_placeholders",
    "extract_persisted_path", "_is_background_review_harness_message",
    "harness_legacy_origin", "hidden_interrupt_placeholder_row",
})
SCOPES = ("gateway", "agent", "tools", "cron", "hermes_cli", "tui_gateway", "acp_adapter")
RULES = frozenset({"literal", "helper", "match"})
PATTERN_VERBS = {"match", "fullmatch", "search", "finditer", "sub", "startswith", "endswith",
                 "removeprefix", "removesuffix", "find", "rfind", "split", "rsplit", "rpartition"}
TEXT_NAMES = re.compile(r"\b(?:content|api_content|sidecar|body|text|head|rendered)\b")
FORMAT_NAMES = re.compile(r"prefix|marker|header|envelope|expected|rendered", re.IGNORECASE)
CLASSIFIER_NAMES = re.compile(
    r"origin|human|synthetic|scaffold|summary|steer|notification|provenance|display_kind|announced", re.IGNORECASE)
# Allowlist entries with a whole-module scope: only time-boxed migration modules and legacy shims.
_MODULE_SCOPE_REASONS = ("MIGRATION", "LEGACY SHIM")


def source_paths(root: Path = ROOT):
    """Non-test core Python source in the guarded scopes."""
    paths = [p for scope in SCOPES for p in (root / scope).rglob("*.py")]
    paths += [root / "run_agent.py", *root.glob("hermes_state*.py")]
    for path in sorted(set(paths)):
        rel = path.relative_to(root)
        if path.is_file() and "tests" not in rel.parts and not path.name.startswith(("test_", "integration_check")):
            yield path


def symbol_owners(tree: ast.AST) -> dict:
    """Exact owning symbol of every node (functions/classes, and module constant bindings)."""
    owners: dict = {}

    def visit(node, owner="<module>"):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            owner = node.name if owner == "<module>" else f"{owner}.{node.name}"
        elif owner == "<module>" and isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) == 1 and isinstance(targets[0], ast.Name):
                owner = targets[0].id
        owners[id(node)] = owner
        for child in ast.iter_child_nodes(node):
            visit(child, owner)

    visit(tree)
    return owners


def _allowed(allow, path: str, symbol: str, rule: str) -> bool:
    for entry in allow:
        if entry["path"] != path or rule not in entry["rules"]:
            continue
        scope = entry["symbol"]
        if scope == "*" or scope == symbol or symbol.startswith(scope + "."):
            return True
    return False


def inspect_text(text: str, path: str = "agent/sample.py", allow=()) -> list:
    """Rule 2 hits in one source text."""
    tree = ast.parse(text)
    owners = symbol_owners(tree)
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    lines = text.splitlines()
    hits: list = []

    def emit(node, rule, detail):
        symbol = owners[id(node)]
        if not _allowed(allow, path, symbol, rule):
            hits.append({"path": path, "symbol": symbol, "line": node.lineno, "rule": rule, "detail": detail})

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if isinstance(parents.get(id(node)), ast.Expr):
                continue  # docstrings / bare string statements document, they do not produce
            normalized = node.value.replace(r"\[", "[").replace(r"\]", "]")
            for literal in FORBIDDEN_LITERALS:
                if literal in normalized:
                    emit(node, "literal", literal)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FORBIDDEN_HELPERS:
            emit(node, "helper", node.name)
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in FORBIDDEN_HELPERS:
                    emit(node, "helper", alias.name)
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in FORBIDDEN_HELPERS:
                emit(node, "helper", name)
            if isinstance(func, ast.Attribute) and name in PATTERN_VERBS:
                src = "\n".join(lines[node.lineno - 1:node.end_lineno])
                if TEXT_NAMES.search(src) and CLASSIFIER_NAMES.search(owners[id(node)]) and FORMAT_NAMES.search(src):
                    emit(node, "match", " ".join(src.split())[:160])
        if isinstance(node, ast.Compare):
            src = "\n".join(lines[node.lineno - 1:node.end_lineno])
            if (len(src) < 800 and TEXT_NAMES.search(src) and FORMAT_NAMES.search(src)
                    and any(isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for op in node.ops)
                    and CLASSIFIER_NAMES.search(owners[id(node)])):
                emit(node, "match", " ".join(src.split())[:160])
    return hits


def load_allowlist() -> list:
    return json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))


def scan(root: Path = ROOT, allow=None) -> list:
    allow = load_allowlist() if allow is None else allow
    hits: list = []
    for path in source_paths(root):
        rel = path.relative_to(root).as_posix()
        hits += inspect_text(path.read_text(encoding="utf-8"), rel, allow)
    return sorted(hits, key=lambda h: (h["path"], h["line"], h["rule"], h["detail"]))


def _identity(hit) -> tuple:
    return hit["path"], hit["symbol"], hit["rule"], hit["detail"]


def baseline_entries(hits) -> list:
    counts = Counter(_identity(h) for h in hits)
    return [{"path": p, "symbol": s, "rule": r, "detail": d, "count": n}
            for (p, s, r, d), n in sorted(counts.items())]


def load_baseline() -> Counter:
    return Counter({(e["path"], e["symbol"], e["rule"], e["detail"]): e["count"]
                    for e in json.loads(BASELINE_PATH.read_text(encoding="utf-8"))})


# ── self tests of the scanner ─────────────────────────────────────────────────


def test_guard_catches_old_literal_and_regex_escape():
    assert inspect_text('x = "[Cron delivery: name]"')
    assert inspect_text('x = r"\\\\[OUT-OF-BAND USER MESSAGE"')
    assert inspect_text('x = "[CONTEXT " "COMPACTION]"')
    assert inspect_text('def f(t):\n    return f"[Webhook delivery: {t}]"')


def test_guard_ignores_docstrings():
    assert not inspect_text('def f():\n    """Used to write ``[Cron delivery: …]``."""\n    return 1')


def test_guard_catches_reintroduced_helper_and_content_match():
    assert any(h["rule"] == "helper" for h in inspect_text("def _source_rows(rows): return rows"))
    assert any(h["rule"] == "helper" for h in inspect_text("from x import harness_legacy_origin"))
    assert any(h["rule"] == "match" for h in inspect_text("def origin_for(content, expected):\n return content == expected"))


def test_allowlist_is_exact_symbol_not_whole_module():
    allow = [{"path": "agent/sample.py", "symbol": "parse_output", "rules": ["literal"], "reason": "model output"}]
    text = 'def parse_output(text):\n return text.startswith("[Cron delivery:")\ndef other():\n return "[Cron delivery:"'
    assert {h["symbol"] for h in inspect_text(text, allow=allow)} == {"other"}


def test_neutral_string_matching_is_not_banned():
    assert not inspect_text('def parse_sse(line):\n return line.startswith("data:")')
    assert not inspect_text("def check_uid(left, right):\n return left == right")


# ── the gate ──────────────────────────────────────────────────────────────────


def test_allowlist_targets_are_narrow_and_current():
    for entry in load_allowlist():
        assert set(entry) == {"path", "symbol", "rules", "reason"}, entry
        assert entry["reason"] and set(entry["rules"]) <= RULES, entry
        target = ROOT / entry["path"]
        assert target.is_file(), f"allowlisted file is gone, drop its entry: {entry}"
        if entry["symbol"] == "*":
            assert entry["reason"].startswith(_MODULE_SCOPE_REASONS), (
                f"whole-module scope is only for migration modules and legacy shims: {entry}")
            continue
        owners = set(symbol_owners(ast.parse(target.read_text(encoding="utf-8"))).values())
        assert entry["symbol"] in owners, f"allowlisted symbol is gone, drop its entry: {entry}"


def test_no_new_internal_chat_format_round_trips():
    """Ratchet: no hit beyond the checked-in baseline, and no stale baseline entry."""
    current = Counter(_identity(h) for h in scan())
    baseline = load_baseline()
    new = current - baseline
    stale = baseline - current
    assert not new, "new Rule 2 violations (type the data instead of formatting it into content):\n" + "\n".join(
        f"  {p} {s} {r}: {d} (x{n})" for (p, s, r, d), n in sorted(new.items()))
    assert not stale, ("Rule 2 hits removed: drop them from tests/ci/rule2_baseline.json "
                       "(python tests/ci/test_rule2_guard.py --write-baseline):\n" + "\n".join(
                           f"  {p} {s} {r}: {d} (x{n})" for (p, s, r, d), n in sorted(stale.items())))


if __name__ == "__main__":
    hits = scan()
    if "--write-baseline" in sys.argv:
        BASELINE_PATH.write_text(json.dumps(baseline_entries(hits), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    by_area = Counter(h["path"].split("/", 1)[0] if "/" in h["path"] else h["path"] for h in hits)
    print(json.dumps({"hits": len(hits), "rules": Counter(h["rule"] for h in hits), "areas": by_area}, indent=1))
