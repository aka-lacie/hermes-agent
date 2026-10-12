"""Consistent managed skill views with a publication base revision."""
import json


def view_with_workflow_revision(name, file_path, task_id, preprocess, render):
    from tools import skills_tool as skills
    from tools.skill_manager_tool import _skill_mutation_lock
    from tools.workflow_packages import read_package, package_revision, materialize_snapshot
    if skills._skill_lookup_path_error(name) or ":" in name:
        return render(name, file_path, task_id, preprocess)
    roots, _ = skills._skill_search_dirs()
    error, directory, _ = skills._locate_skill(name, None, roots)
    if error is not None or directory is None:
        return render(name, file_path, task_id, preprocess)
    with _skill_mutation_lock(directory):
        raw = render(name, file_path, task_id, preprocess)
        result = json.loads(raw)
        if result.get("success") and (directory / "workflows.json").is_file():
            files = read_package(directory)
            result["workflow_revision"] = package_revision(files)
            materialize_snapshot(files)
            return json.dumps(result, ensure_ascii=False)
        return raw
