from __future__ import annotations

CHANNEL_WEIGHT_PATH = 1.0
CHANNEL_WEIGHT_CONTENT = 2.0
CHANNEL_WEIGHT_TERM = 1.5
INTERNAL_RECALL_K_MULTIPLIER = 2
RRF_K = 60
DEFAULT_TOP_K = 10

DATA_TYPE_ALLOWED_CHUNK_TYPES: dict[int, set[str] | None] = {
    1: None,
    2: {'text'},
    3: {'image'},
    4: {'table'},
    5: {'text', 'image'},
    6: {'text', 'table'},
}


def resolve_allowed_chunk_types(data_type: int) -> set[str] | None:
    return DATA_TYPE_ALLOWED_CHUNK_TYPES.get(data_type)
