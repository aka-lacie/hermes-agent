"""Profile-owned public plugin extensions for code execution."""
import re

from hermes_cli.plugins_loader import _serialized_replacement


class CodeExtensionContextMethods:
    def _code_entries(self, attr):
        if not hasattr(self._manager, attr):
            setattr(self._manager, attr, {})
        return getattr(self._manager, attr)

    @_serialized_replacement
    def register_code_namespace(self, name, stub_source, handlers, contract_version=1, description=""):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or name == "tools":
            raise ValueError("Invalid or reserved code namespace")
        if contract_version != 1 or len(stub_source.encode()) > 131072:
            raise ValueError("Unsupported code namespace contract or oversized stub")
        if not all(callable(value) for value in handlers.values()):
            raise ValueError("Code namespace handlers must be callable")
        value = {"source": stub_source, "handlers": dict(handlers), "owner": self.plugin_id,
                 "description": str(description)[:4000]}
        return self._register_entry("code_namespace", name, self._code_entries("_code_namespaces"),
                                    value, "Plugin %s registered code namespace")

    @_serialized_replacement
    def register_cell_entry_point(self, name, schema, handler):
        from agent.code_cells import dispatch_cell_entry
        handle = self.register_tool(
            name=name, toolset=self.plugin_id, schema=schema,
            handler=lambda args, **kw: dispatch_cell_entry(name, args),
        )
        if handle is None:
            raise ValueError(f"Cell entry point {name!r} could not be registered")
        return self._register_entry("cell_entry", name, self._code_entries("_cell_entries"),
                                    {"handler": handler, "owner": self.plugin_id},
                                    "Plugin %s registered cell entry")

    @_serialized_replacement
    def register_workflow_validator(self, handler):
        if not callable(handler):
            raise ValueError("Workflow validator must be callable")
        return self._register_entry("workflow_validator", self.plugin_id,
                                    self._code_entries("_workflow_validators"), handler,
                                    "Plugin %s registered static workflow validator")
