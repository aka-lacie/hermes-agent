"""Execution-mode invariants every cron job definition must satisfy.

``create_job`` and ``update_job`` (``cron/jobs.py``) both run these, so an update cannot
bypass a rule the create door enforces.
"""
from typing import Optional

from hermes_constants import display_hermes_home


def normalize_job_type(value, *, no_agent=False) -> str:
    text = str(value or "").strip().lower() or ("script" if no_agent else "agent")
    if text not in {"agent", "script", "reminder"}:
        raise ValueError(f"Unknown cron job_type {value!r}; expected agent, script, or reminder.")
    return text


def job_uses_agent(job: dict) -> bool:
    """Literal reminders and script jobs never need inference or canonical agent admission."""
    return not job.get("no_agent") and normalize_job_type(job.get("job_type")) == "agent"


def normalize_delivery_mode(value) -> str:
    text = str(value or "direct").strip().lower() or "direct"
    if text not in {"direct", "internal_turn"}:
        raise ValueError(f"Unknown cron delivery_mode {value!r}; expected direct or internal_turn.")
    return text


def normalize_execution_update(job: dict, updates: dict) -> None:
    if "job_type" in updates:
        kind = normalize_job_type(updates["job_type"], no_agent=bool(updates.get("no_agent", job.get("no_agent"))))
        if updates.get("no_agent") and kind != "script":
            raise ValueError("no_agent=True is only compatible with job_type='script'.")
        updates.update(job_type=kind, no_agent=kind == "script")
    elif "no_agent" in updates:
        updates["job_type"] = "script" if updates["no_agent"] else "agent"
    if "delivery_mode" in updates:
        updates["delivery_mode"] = normalize_delivery_mode(updates["delivery_mode"])


def validate_reminder_payload(job: dict) -> None:
    if job.get("job_type") != "reminder":
        return
    if not str(job.get("prompt") or "").strip():
        raise ValueError("job_type='reminder' requires reminder text in prompt.")
    if job.get("script") or job.get("skills") or job.get("skill"):
        raise ValueError("Reminder jobs cannot use script or skills; they emit their prompt directly.")

NO_AGENT_WITHOUT_SCRIPT_ERROR = (
    "no_agent=True requires a script — with no agent and no script "
    "there is nothing for the job to run."
)

SCRIPT_IS_COMMAND_LINE_ERROR = (
    "script must name a file under {scripts_dir} (e.g. 'watchdog.sh'), not a shell command line: "
    "{script!r} has no such file. Write the command into a script there and pass its filename."
)


def _validate_job_mode_invariants(
    monitor_script: Optional[str],
    monitor_url: Optional[str],
    no_agent: bool,
    script: Optional[str],
    job_type: Optional[str] = None,
) -> None:
    """Execution-mode invariants shared by create_job and update_job (no bypass via the update
    door)."""
    if monitor_script and monitor_url:
        raise ValueError(
            "monitor_script and monitor_url are mutually exclusive — a job "
            "can only have one monitor source.")
    if (monitor_script or monitor_url) and no_agent:
        raise ValueError(
            "monitor_script/monitor_url cannot be combined with no_agent=True — "
            "the whole point of a monitor job is to suppress or wake the AGENT "
            "based on source changes. Use a plain no_agent script job instead.")
    if no_agent and not script:
        raise ValueError(NO_AGENT_WITHOUT_SCRIPT_ERROR)
    if (monitor_script or monitor_url) and job_type not in (None, "agent"):
        raise ValueError("monitor_script/monitor_url can only be combined with job_type='agent'.")
    if script and _script_is_command_line(script):
        raise ValueError(SCRIPT_IS_COMMAND_LINE_ERROR.format(
            scripts_dir=display_hermes_home() + "/scripts/", script=script))


def validate_schedule_knobs(
    schedule: dict, timezone: Optional[str], grace_minutes: Optional[int],
) -> None:
    """Per-job schedule knobs (``timezone``/``grace_minutes``) are validated at BOTH doors —
    ``create_job`` and ``update_job`` — so an update cannot store a schedule knob a create
    would reject.

    Raises ValueError for an unknown IANA zone, and for ``grace_minutes`` on a schedule that has
    no wall clock to be late for (``once``/``interval``): the catch-up window only means
    something to a recurring cron cadence, so accepting it silently would suggest an effect it
    cannot have.
    """
    from cron.jobs_time import normalize_grace_minutes, normalize_timezone

    normalize_timezone(timezone)
    minutes = normalize_grace_minutes(grace_minutes)
    if minutes is None:
        return
    kind = (schedule or {}).get("kind")
    if kind != "cron":
        raise ValueError(
            f"grace_minutes only applies to cron schedules (a fixed wall clock that can be "
            f"missed); this job's schedule kind is {kind!r}.")


def _script_is_command_line(script: str) -> bool:
    """A ``script`` with whitespace that resolves to no file is a command line (``echo hi``),
    not a path; run every tick it would deliver "Script not found" as the payload. A plain
    missing filename stays creatable (``hermes cron doctor`` reports it)."""
    if not any(ch.isspace() for ch in script.strip()):
        return False
    from cron.lifecycle_guard import _resolve_script_path
    path = _resolve_script_path(script)
    return path is None or not path.is_file()
