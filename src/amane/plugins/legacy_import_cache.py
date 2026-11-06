"""读取主机 v1 快照; 不创建新快照, 不访问来源目录."""

import hashlib
from pathlib import Path

from .import_models import PreparedImport


class LegacySnapshot(PreparedImport):
    source_id: str
    root: str
    source_file: str


def read_legacy_snapshot(directory: Path) -> LegacySnapshot:
    snapshot = LegacySnapshot.model_validate_json((directory / "snapshot.json").read_text(encoding="utf-8"))
    source = (directory / snapshot.source_file).resolve()
    if (
        not source.is_relative_to(directory)
        or hashlib.sha256(source.read_bytes()).hexdigest() != snapshot.candidate.source_hash
    ):
        raise ValueError("预览缓存来源内容无效, 请重新预览")
    for image in snapshot.images:
        image.path = str(directory / image.path)
    return snapshot
