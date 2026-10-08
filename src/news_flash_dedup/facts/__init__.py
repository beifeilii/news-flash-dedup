"""Single-text Fact extraction contracts and original-text validation."""

from .core import (
    DeterministicFactModel,
    ExtractionRequest,
    FactExtractionService,
    FactIssue,
    FactValidationReport,
    NumericMention,
    scan_numeric_inventory,
    validate_fact_artifact,
)
from .schema import FACT_SCHEMA, schema_for_model

__all__ = [
    "FACT_SCHEMA",
    "DeterministicFactModel",
    "ExtractionRequest",
    "FactExtractionService",
    "FactIssue",
    "FactValidationReport",
    "NumericMention",
    "scan_numeric_inventory",
    "schema_for_model",
    "validate_fact_artifact",
]
