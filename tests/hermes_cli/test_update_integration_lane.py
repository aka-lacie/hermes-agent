"""Personal runtime integration remains safe through updater decomposition."""

from tests.hermes_cli.test_update_target_identity import update_tree  # noqa: F401 — shared fixture
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


@pytest.mark.parametrize("conflict", [False, True])
def test_configured_integration_uses_real_refs_and_new_completion(update_tree, monkeypatch, conflict):
    from hermes_cli import main as cli_main
    from tests.hermes_cli.test_update_target_identity import git

    t = update_tree
    git(t.clone, "checkout", "-qb", "dev2")
    personal = t.clone / ("content.txt" if conflict else "personal.txt")
    personal.write_text("personal behavior\n")
    git(t.clone, "add", personal.name)
    git(t.clone, "-c", "commit.gpgsign=false", "commit", "-qm", "personal")
    before = git(t.clone, "rev-parse", "HEAD")
    git(t.clone, "remote", "rename", "origin", "source")
    monkeypatch.setattr("hermes_cli.main_install_repair._configured_update_target_value",
                        lambda key, default: {"integration_branch": "dev2", "remote": "source",
                                              "branch": "main"}.get(key, default))
    # A default stable channel must not detach an explicitly configured integration lane.
    if conflict:
        with pytest.raises(SystemExit) as exc:
            cli_main.cmd_update(t.args)
        assert exc.value.code == 1
        assert git(t.clone, "rev-parse", "HEAD") == before
        assert not (t.clone / ".git" / "MERGE_HEAD").exists()
        assert not t.requests
    else:
        cli_main.cmd_update(t.args)
        git(t.clone, "merge-base", "--is-ancestor", before, "HEAD")
        git(t.clone, "merge-base", "--is-ancestor", t.newer, "HEAD")
        assert t.requests[-1]["expected_sha"] == git(t.clone, "rev-parse", "HEAD")
        assert personal.read_text() == "personal behavior\n"
    assert git(t.clone, "branch", "--show-current") == "dev2"
