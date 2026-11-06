"""导入插件返回的候选; 文件读取与数据库写入由主机分别授权和执行."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..crawlers.models import MediaMetadata


class ImportImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["poster", "thumb", "still"]
    path: str


class ImportCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: str
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata: MediaMetadata
    images: list[ImportImage] = Field(default_factory=list, max_length=500)
    local_trailer_paths: list[str] = Field(default_factory=list, max_length=100)
    media_paths: list[str] = Field(default_factory=list, max_length=100)
    warnings: list[str] = Field(default_factory=list, max_length=100)
    source_bytes: bytes | None = Field(default=None, exclude=True)
