"""The small subset of native cluster state needed for index lifecycle decisions."""

from typing import Literal

from pydantic import BaseModel, Field

from shared.services.retrieval.lexical.error import LexicalIndexError


class _NativeNode(BaseModel):
    ephemeral_id: str = Field(min_length=1)


class _IndexIdentity(BaseModel):
    uuid: str = Field(min_length=1)


class _IndexSettings(BaseModel):
    index: _IndexIdentity


class _IndexMetadata(BaseModel):
    state: Literal["open", "close"]
    settings: _IndexSettings


class _Metadata(BaseModel):
    indices: dict[str, _IndexMetadata] = Field(default_factory=dict)


class _Shard(BaseModel):
    primary: bool
    state: str


class _IndexRouting(BaseModel):
    shards: dict[str, tuple[_Shard, ...]]


class _RoutingTable(BaseModel):
    indices: dict[str, _IndexRouting] = Field(default_factory=dict)


class _Block(BaseModel):
    levels: frozenset[str]


class _Blocks(BaseModel):
    globalBlocks: dict[str, _Block] = Field(alias="global", default_factory=dict)
    indices: dict[str, dict[str, _Block]] = Field(default_factory=dict)


class NativeLifecycleSnapshot(BaseModel):
    cluster_uuid: str = Field(min_length=1)
    nodes: dict[str, _NativeNode]
    metadata: _Metadata = Field(default_factory=_Metadata)
    routing_table: _RoutingTable = Field(default_factory=_RoutingTable)
    blocks: _Blocks = Field(default_factory=_Blocks)

    def getIncarnation(self) -> str:
        if len(self.nodes) != 1 or self.cluster_uuid == "_na_":
            raise LexicalIndexError(
                "Lexical lifecycle requires one elected native node", isRetryable=True
            )
        return next(iter(self.nodes.values())).ephemeral_id

    def getIndexUuid(self, name: str) -> str | None:
        entry: _IndexMetadata | None = self.metadata.indices.get(name)
        return entry.settings.index.uuid if entry else None

    def isOpen(self, name: str) -> bool:
        entry: _IndexMetadata | None = self.metadata.indices.get(name)
        return entry is not None and entry.state == "open"

    def isClosed(self, name: str) -> bool:
        entry: _IndexMetadata | None = self.metadata.indices.get(name)
        return (
            entry is not None
            and entry.state == "close"
            and "4" in self.blocks.indices.get(name, {})
        )

    def isReady(self, name: str) -> bool:
        routing: _IndexRouting | None = self.routing_table.indices.get(name)
        blocks: tuple[_Block, ...] = (
            *self.blocks.globalBlocks.values(),
            *self.blocks.indices.get(name, {}).values(),
        )
        return bool(
            self.isOpen(name)
            and routing
            and routing.shards
            and not any(block.levels & {"read", "write"} for block in blocks)
            and all(
                any(
                    shard.primary and shard.state in ("STARTED", "RELOCATING")
                    for shard in copies
                )
                for copies in routing.shards.values()
            )
        )
