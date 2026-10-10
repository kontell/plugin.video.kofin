"""A validator for the companion contract's JSON Schema, the subset it uses:
``$ref`` into ``definitions``, ``type`` (one or several), ``required``,
``properties``, ``additionalProperties`` (a schema or false), ``items`` and
``enum``. Vendored on both sides of the contract, so a field added on one
side fails the other's tests."""

import json
from typing import Any, Dict, List, Optional

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def load(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    assert isinstance(data, dict)
    return data


def _resolve(schema: Dict[str, Any], root: Dict[str, Any]) -> Dict[str, Any]:
    ref = schema.get("$ref")
    if not ref:
        return schema
    assert ref.startswith("#/"), ref
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    assert isinstance(node, dict)
    return _resolve(node, root)


def _type_ok(value: Any, type_name: str) -> bool:
    expected = _TYPES[type_name]
    if type_name == "integer" and isinstance(value, bool):
        return False
    if type_name == "number" and isinstance(value, bool):
        return False
    return isinstance(value, expected)


def validate(
    instance: Any, schema: Dict[str, Any], root: Dict[str, Any], path: str = "$"
) -> List[str]:
    """Every violation as ``path: reason``; empty when the instance conforms."""
    schema = _resolve(schema, root)
    errors: List[str] = []
    types = schema.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        if not any(_type_ok(instance, name) for name in names):
            errors.append("%s: expected %s" % (path, "/".join(names)))
            return errors
    if "enum" in schema and instance not in schema["enum"]:
        errors.append("%s: %r not in %r" % (path, instance, schema["enum"]))
    if isinstance(instance, dict):
        for key in schema.get("required", ()):
            if key not in instance:
                errors.append("%s: missing %r" % (path, key))
        properties = schema.get("properties", {})
        for key, value in instance.items():
            child = path + "." + key
            if key in properties:
                errors.extend(validate(value, properties[key], root, child))
            else:
                extra = schema.get("additionalProperties", True)
                if extra is False:
                    errors.append("%s: unexpected" % child)
                elif isinstance(extra, dict):
                    errors.extend(validate(value, extra, root, child))
    if isinstance(instance, list) and "items" in schema:
        for index, value in enumerate(instance):
            errors.extend(
                validate(value, schema["items"], root, "%s[%d]" % (path, index))
            )
    return errors


def check_document(document: Dict[str, Any], schema: Dict[str, Any]) -> None:
    """The request document against the schema, each request also against
    its kind's own schema (``x-kinds``); raises with every violation."""
    errors = validate(document, schema, schema)
    kinds: Dict[str, Any] = schema.get("x-kinds", {})
    for index, request in enumerate(document.get("requests", [])):
        kind = request.get("kind") if isinstance(request, dict) else None
        if kind in kinds:
            errors.extend(
                validate(request, kinds[kind], schema, "$.requests[%d]" % index)
            )
    assert not errors, "\n".join(errors)


def check_status(status: Dict[str, Any], schema: Dict[str, Any]) -> None:
    errors = validate(status, schema["x-status"], schema, "$")
    assert not errors, "\n".join(errors)


def kind_schema(schema: Dict[str, Any], kind: str) -> Optional[Dict[str, Any]]:
    kinds: Dict[str, Any] = schema.get("x-kinds", {})
    return kinds.get(kind)
