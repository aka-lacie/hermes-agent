"""Static package descriptors through registered validators, never candidate imports."""
import json


def inspect_snapshot(snapshot):
    from hermes_cli.plugins import get_plugin_manager
    from tools.workflow_packages import package_revision, read_package
    files = read_package(snapshot["path"])
    if package_revision(files) != snapshot["revision"]:
        raise ValueError("workflow_snapshot_corrupt: captured package changed")
    reports = []
    if "workflows.json" in files:
        validators = getattr(get_plugin_manager(), "_workflow_validators", {})
        if not validators:
            raise ValueError("workflow_validation_unavailable: enable a static workflow validator")
        for validator in list(validators.values()):
            report = validator(snapshot["path"])
            if isinstance(report, dict) and isinstance(report.get("workflows"), dict):
                reports.append(report["workflows"])
        if not reports:
            raise ValueError("workflow_inspection_unavailable: validator supplied no workflow descriptors")
    if package_revision(read_package(snapshot["path"])) != snapshot["revision"]:
        raise ValueError("workflow_snapshot_corrupt: validator changed the package")
    specs = {}
    for report in reports:
        for name, spec in report.items():
            if name in specs and specs[name] != spec:
                raise ValueError("workflow_inspection_ambiguous: validators disagree")
            specs[name] = spec
    examples = [dict(json.loads(content), file=path) for path, content in sorted(files.items())
                if path.startswith("examples/") and path.endswith(".json") and specs]
    descriptors = [{**spec, "name": snapshot["name"] + ":" + name,
                    "output_schema": spec.get("output_schema"), "revision": snapshot["revision"],
                    "read_only": snapshot["read_only"],
                    "examples": [e for e in examples if e["workflow"] == name]}
                   for name, spec in sorted(specs.items())]
    return {"name": snapshot["name"], "revision": snapshot["revision"],
            "read_only": snapshot["read_only"], "workflows": descriptors}


def inspect_skill_package(name):
    from tools.workflow_packages import snapshot_skill
    return inspect_snapshot(snapshot_skill(name))
