"""Consistent skill views with static workflow descriptors and publication revisions."""
import json


def view_with_workflow_revision(name, file_path, task_id, preprocess, render, *, include_workflows=False):
    from tools.skill_packages import resolve_skill_package
    from tools.skill_manager_tool import _skill_mutation_lock
    from tools.workflow_packages import snapshot_skill
    from tools.workflow_inspection import inspect_snapshot
    try:
        package = resolve_skill_package(name)
    except ValueError as exc:
        from hermes_cli.plugins import get_plugin_manager
        if ":" in name and get_plugin_manager().is_plugin_skill_namespace(name.split(":", 1)[0]):
            return json.dumps({"success": False, "error": str(exc)})
        return render(name, file_path, task_id, preprocess)
    with _skill_mutation_lock(package["path"]):
        result = json.loads(render(name, file_path, task_id, preprocess))
        if result.get("success"):
            result["read_only"] = package["read_only"]
            if include_workflows or (package["path"] / "workflows.json").is_file():
                try:
                    snapshot = snapshot_skill(name)
                    result["workflow_revision"] = snapshot["revision"]
                    result["workflows"] = inspect_snapshot(snapshot)["workflows"]
                except (ValueError, OSError) as exc:
                    result["workflow_error"] = str(exc)
        return json.dumps(result, ensure_ascii=False)
