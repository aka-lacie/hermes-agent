"""Saved contracts track validation drift, never per-session wording changes."""
from copy import deepcopy

import pytest

from agent.code_tool_catalog import schema_fingerprint, validate_arguments


def test_annotations_are_ignored_but_properties_named_like_annotations_are_data():
    schema = {"type": "object", "properties": {"description": {"type": "string"},
              "value": {"type": "integer", "default": 2}}, "required": ["value"]}
    changed = deepcopy(schema)
    changed.update(description="Session-specific wording", title="Label", examples=[{}], **{"$comment": "churn"})
    changed["properties"]["value"]["description"] = "Different wording"
    assert schema_fingerprint(changed) == schema_fingerprint(schema)
    changed["properties"]["description"]["type"] = "integer"
    assert schema_fingerprint(changed) != schema_fingerprint(schema)


@pytest.mark.parametrize("change", [{"default": 4}, {"minimum": 0}, {"enum": [1, 2]}, {"type": "string"}])
def test_validation_changes_invalidate_saved_contract(change):
    base = {"type": "integer", "default": 2}
    assert schema_fingerprint(base) != schema_fingerprint({**base, **change})


def test_resolved_reference_and_inline_schema_have_same_fingerprint():
    inline = {"type": "object", "properties": {"value": {"type": "integer"}}}
    referenced = {"type": "object", "properties": {"value": {"$ref": "#/$defs/Number"}},
                  "$defs": {"Number": {"type": "integer", "description": "ignored"}}}
    assert schema_fingerprint(referenced) == schema_fingerprint(inline)
    validate_arguments(referenced, {"value": 1})
    with pytest.raises(ValueError, match="invalid_arguments: value"):
        validate_arguments(referenced, {"value": "bad"})


def test_external_and_recursive_references_fail_without_resolution_network():
    with pytest.raises(ValueError, match="local resolvable"):
        schema_fingerprint({"$ref": "https://example.invalid/schema"})
    with pytest.raises(ValueError, match="recursive"):
        schema_fingerprint({"$defs": {"x": {"$ref": "#/$defs/x"}}, "$ref": "#/$defs/x"})
