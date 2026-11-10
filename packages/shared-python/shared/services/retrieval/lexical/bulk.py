"""Pack complete lexical units into bounded UTF-8 NDJSON requests."""

from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
import json

from shared.services.retrieval.lexical.error import LexicalIndexError
from shared.services.retrieval.lexical.unit import LexicalUnit


@dataclass(frozen=True)
class LexicalBulk:
    textBody: str
    unitCount: int

    @classmethod
    def createRequests(
        cls, *, buildId: str, units: tuple[LexicalUnit, ...], maximumBytes: int
    ) -> Iterator["LexicalBulk"]:
        if maximumBytes <= 0:
            raise ValueError("Lexical bulk byte limit must be positive")
        records: list[str] = []
        byteCount: int = 0
        for unit in units:
            record: str = cls._encodeUnit(buildId, unit)
            recordBytes: int = len(record.encode("utf-8"))
            if recordBytes > maximumBytes:
                raise LexicalIndexError(
                    f"Lexical unit {unit.unitId!r} exceeds the bulk byte limit "
                    f"({recordBytes} > {maximumBytes})", isSizeLimit=True
                )
            if records and byteCount + recordBytes > maximumBytes:
                yield cls(textBody="".join(records), unitCount=len(records))
                records = []
                byteCount = 0
            records.append(record)
            byteCount += recordBytes
        if records:
            yield cls(textBody="".join(records), unitCount=len(records))

    @staticmethod
    def _encodeUnit(buildId: str, unit: LexicalUnit) -> str:
        # Preserve the existing build/unit identity across different packing.
        identity: str = sha256(
            json.dumps([buildId, unit.unitId], separators=(",", ":")).encode()
        ).hexdigest()
        action: str = json.dumps({"index": {"_id": identity}})
        source: str = json.dumps(
            dict(unit.model_dump(), buildId=buildId), ensure_ascii=False
        )
        return f"{action}\n{source}\n"
