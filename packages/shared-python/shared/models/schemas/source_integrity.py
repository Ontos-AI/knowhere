from base64 import b64encode

from pydantic import BaseModel, ConfigDict, Field


class SourceIntegrity(BaseModel):
    """Expected identity of the original bytes for a guarded file upload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    size_bytes: int = Field(ge=0, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def checksum_base64(self) -> str:
        return b64encode(bytes.fromhex(self.sha256)).decode("ascii")
