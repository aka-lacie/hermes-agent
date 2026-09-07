"""Personal runtime integration remains safe through updater decomposition."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import update_cmd


def test_integration_guard_never_switches_even_when_fully_merged():
    with patch.object(update_cmd, "_git_run") as git:
        assert update_cmd._apply_parked_branch_guard(
            ["git"], "main", "dev2", switch_branch=False,
            _windows_gateway_resume=[], integration_branch="dev2",
        ) == (False, True, None)
    git.assert_not_called()


@pytest.mark.parametrize("current", ["main", "HEAD", "feat/example"])
def test_integration_guard_refuses_wrong_checkout(current):
    with patch.object(update_cmd, "_git_run") as git, \
         patch("hermes_cli.main._resume_windows_gateways_after_update"), \
         pytest.raises(SystemExit) as exc:
        update_cmd._apply_parked_branch_guard(
            ["git"], "main", current, switch_branch=True,
            _windows_gateway_resume=[], integration_branch="dev2",
        )
    assert exc.value.code == 1
    git.assert_not_called()


def test_conflicted_integration_merge_aborts_without_reset(capsys):
    calls = []

    def git(_cmd, args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            returncode=1 if args[:2] == ["merge", "--no-edit"] else 0,
            stdout="cron/jobs.py\n" if args[0] == "diff" else "", stderr="conflict",
        )

    with patch.object(update_cmd, "_git_run", side_effect=git), \
         patch.object(update_cmd, "_capture_head_sha", return_value="before"), \
         pytest.raises(SystemExit):
        update_cmd._pull_updates(
            ["git"], "main", None, prompt_for_restore=False, gw_input_fn=None,
            discard_local_changes=False, keep_stash=False, remote="source",
            in_place_update=True,
        )
    assert calls == [["merge", "--no-edit", "source/main"],
                     ["diff", "--name-only", "--diff-filter=U"], ["merge", "--abort"]]
    assert "cron/jobs.py" in capsys.readouterr().out
