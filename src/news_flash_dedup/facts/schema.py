"""The frozen single-text extraction contract from 09 §7.4."""

from __future__ import annotations

from copy import deepcopy


def _object(required: list[str], properties: dict) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _slot(*, decimal: bool = False) -> dict:
    required = ["status", "raw_value", "evidence"]
    properties = {
        "status": {"enum": ["present", "missing", "uncertain"]},
        "raw_value": {"type": ["string", "null"], "minLength": 1},
        "evidence": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
    }
    if decimal:
        required.insert(2, "value")
        properties["value"] = {
            "type": ["string", "null"],
            "pattern": r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$",
        }
    schema = _object(required, properties)
    schema["allOf"] = [
        {
            "if": {"properties": {"status": {"const": "present"}}},
            "then": {
                "properties": {
                    "raw_value": {"type": "string", "minLength": 1},
                    "evidence": {"minItems": 1},
                }
            },
        },
        {
            "if": {"properties": {"status": {"const": "missing"}}},
            "then": {
                "properties": {
                    "raw_value": {"type": "null"},
                    "evidence": {"maxItems": 0},
                }
            },
        },
    ]
    if decimal:
        schema["allOf"][1]["then"]["properties"]["value"] = {"type": "null"}
    return schema


_EVIDENCE = _object(
    ["record_id", "field", "quote", "start", "end"],
    {
        "record_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "field": {"type": "string", "minLength": 1},
        "quote": {"type": "string", "minLength": 1},
        "start": {"type": "integer", "minimum": 0},
        "end": {"type": "integer", "minimum": 1},
    },
)

_NUMERIC_FIELDS = [
    "numeric_id", "evidence", "metric", "value", "range_end", "magnitude",
    "unit", "currency", "role", "comparator", "direction", "time",
]
_NUMERIC_PROPERTIES = {
    "numeric_id": {"type": "string", "pattern": "^n[1-9][0-9]*$"},
    "evidence": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/evidence"}},
}
for _name in _NUMERIC_FIELDS[2:]:
    _NUMERIC_PROPERTIES[_name] = {"$ref": "#/$defs/decimal_slot" if _name in {"value", "range_end"} else "#/$defs/slot"}

FACT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version", "record_id", "offset_unit", "extraction_status",
        "facts", "unparsed_spans", "uncertainties",
    ],
    "properties": {
        "schema_version": {"const": "1.0"},
        "record_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "offset_unit": {"const": "unicode_code_point"},
        "extraction_status": {"enum": ["complete", "partial", "failed"]},
        "facts": {"type": "array", "items": {"$ref": "#/$defs/fact"}},
        "unparsed_spans": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
        "uncertainties": {"type": "array", "items": {"$ref": "#/$defs/uncertainty"}},
    },
    "$defs": {
        "evidence": _EVIDENCE,
        "slot": _slot(),
        "decimal_slot": _slot(decimal=True),
        "event_state": _object(
            ["predicate", "polarity", "modality", "attribution"],
            {name: {"$ref": "#/$defs/slot"} for name in ["predicate", "polarity", "modality", "attribution"]},
        ),
        "time": _object(
            ["expression", "stage", "anchor"],
            {name: {"$ref": "#/$defs/slot"} for name in ["expression", "stage", "anchor"]},
        ),
        "numeric": _object(_NUMERIC_FIELDS, _NUMERIC_PROPERTIES),
        "fact": _object(
            ["fact_id", "evidence", "fact_type", "subject", "event_state", "time", "key_object", "numerics"],
            {
                "fact_id": {"type": "string", "pattern": "^f[1-9][0-9]*$"},
                "evidence": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/evidence"}},
                "fact_type": {"$ref": "#/$defs/slot"},
                "subject": {"$ref": "#/$defs/slot"},
                "event_state": {"$ref": "#/$defs/event_state"},
                "time": {"$ref": "#/$defs/time"},
                "key_object": {"$ref": "#/$defs/slot"},
                "numerics": {"type": "array", "items": {"$ref": "#/$defs/numeric"}},
            },
        ),
        "uncertainty": _object(
            ["field", "issue", "evidence"],
            {
                "field": {"type": "string", "minLength": 1},
                "issue": {"type": "string", "minLength": 1},
                "evidence": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
            },
        ),
    },
}


def schema_for_model() -> dict:
    """Return an isolated copy so model adapters cannot mutate the contract."""
    return deepcopy(FACT_SCHEMA)
