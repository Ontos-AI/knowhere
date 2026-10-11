"""Public parse-result ZIP contract and compatibility reader."""

from .models import Chunks, ChunkRecord, DocNav, Manifest, SCHEMA_VERSION
from .validation import (
    ValidatedParseResult,
    validate_parse_result,
    validate_parse_result_archive,
)
