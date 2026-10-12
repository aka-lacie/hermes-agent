"""Source embedded in kernels for generic current-session tools."""

SOURCE = '''
from types import SimpleNamespace as _CodeResult
import hashlib as _artifact_hashlib
import json as _artifact_json

class _ToolsNamespace:
    def search(self, query, limit=10):
        return _call("tools.search", {"query": query, "limit": limit})

    def describe(self, name):
        return _CodeResult(**_call("tools.describe", {"name": name}))

    def call(self, name, arguments, expected_fingerprint=None):
        result = _call("tools.call", {"name": name, "arguments": arguments,
                                      "expected_fingerprint": expected_fingerprint})
        return _CodeResult(**result)

    def _open_artifact(self, handle):
        # The host authorizes each read, including a cached local/remote path.
        # Paths supplied in caller handles are never trusted.
        prepared = _call("tools.read_artifact", {"handle": handle})
        stream = open(prepared["path"], "rb")
        try:
            digest = _artifact_hashlib.sha256()
            size = 0
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
            if size != prepared["size_bytes"] or digest.hexdigest() != prepared["sha256"]:
                raise ToolCallError("artifact_changed", "Materialized artifact failed integrity verification.")
            stream.seek(0)
            return stream, prepared
        except BaseException:
            stream.close()
            raise

    def open_artifact(self, handle):
        return self._open_artifact(handle)[0]

    def read_artifact(self, handle):
        stream, prepared = self._open_artifact(handle)
        with stream:
            if prepared["media_type"] == "application/json":
                return _artifact_json.load(stream)
            return stream.read()

tools = _ToolsNamespace()
'''
