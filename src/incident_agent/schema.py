"""Turn a Pydantic model into a schema the structured-output API accepts.

Structured outputs support a subset of JSON Schema: numeric bounds, string
lengths and array cardinality are rejected. Pydantic emits them from
``Field(ge=..., le=...)``, so they are stripped here and re-applied client-side
when the response is validated back into the model.
"""

from __future__ import annotations

from typing import Any

#: Validation keywords the structured-output API does not accept.
_UNSUPPORTED = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "format",
    "default",
}


def sanitise(node: Any) -> Any:
    """Recursively drop unsupported keywords and pin ``additionalProperties``."""
    if isinstance(node, list):
        return [sanitise(item) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {
        key: sanitise(value) for key, value in node.items() if key not in _UNSUPPORTED
    }

    if out.get("type") == "object" or "properties" in out:
        out.setdefault("additionalProperties", False)
        # Every declared property must be required; optionality is expressed by
        # allowing null in the property's own type instead.
        props = out.get("properties") or {}
        if props:
            out["required"] = list(props.keys())

    return out


def schema_for(model: type) -> dict[str, Any]:
    """JSON schema for a Pydantic model, accepted by structured outputs."""
    return sanitise(model.model_json_schema())
