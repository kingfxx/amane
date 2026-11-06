"""通用缓存授权、就绪标记与任务引用保护; 快照内容由插件管理."""

import asyncio
import os
import shutil
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from ..db.models import TaskStatus, TaskType
from ..db.repository import Repository
from ..utils.path import nfc_path
from ..utils.threads import in_thread
from .api import ImportProvider, SnapshotImportProvider
from .import_models import ImportContext, ImportProgress, PreparedImport
from .importing import _checked, read_import, validate_import_candidate
from .legacy_import_cache import read_legacy_snapshot


class ImportSnapshot(BaseModel):
    cache_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    snapshot: PreparedImport


class _CacheEntry(BaseModel):
    version: Literal[2] = 2
    source_id: str
    root: str
    source_path: str
    source_hash: str


def _validate_prepared(prepared: PreparedImport, context: ImportContext, safe_dirs: Sequence[Path] | None) -> None:
    validate_import_candidate(prepared.candidate, context.source, context.root, safe_dirs, require_exists=False)
    paths = {nfc_path(path) for path in prepared.candidate.media_paths}
    if {item.path for item in prepared.media} != paths or len(prepared.media) != len(paths):
        raise ValueError("快照媒体信息与候选路径不匹配")
    for image in prepared.images:
        path = Path(image.path).resolve()
        if not path.is_relative_to(context.cache_dir) or not path.is_file():
            raise ValueError("预览缓存图片缺失或路径无效, 请重新预览")
        image.path = str(path)


def _ignore_progress(progress: ImportProgress) -> None:
    pass


class ImportCache:
    def __init__(self, data_dir: Path) -> None:
        self.root = (data_dir / "import-cache").resolve()
        self.lock = asyncio.Lock()

    async def stage(
        self,
        provider: ImportProvider,
        source_id: str,
        source: Path,
        root: Path,
        safe_dirs: Sequence[Path] | None,
        report: Callable[[ImportProgress], None] = _ignore_progress,
    ) -> ImportSnapshot:
        return await self._stage(self.root, provider, source_id, source, root, safe_dirs, report)

    @staticmethod
    @in_thread
    def _stage(
        cache_root: Path,
        provider: ImportProvider,
        source_id: str,
        source: Path,
        root: Path,
        safe_dirs: Sequence[Path] | None,
        report: Callable[[ImportProgress], None],
    ) -> ImportSnapshot:
        if not isinstance(provider, SnapshotImportProvider):
            candidate = read_import.sync(provider, source, root, safe_dirs)
            return ImportSnapshot(snapshot=PreparedImport(candidate=candidate, images=candidate.images, media=[]))
        source = _checked(source, root, safe_dirs)
        directory = cache_root / uuid4().hex
        directory.mkdir(parents=True)
        context = ImportContext(root=root, source=source, cache_dir=directory)
        try:
            prepared = provider.prepare(context, report)
            validate_import_candidate(prepared.candidate, source, root, safe_dirs)
            _validate_prepared(prepared, context, safe_dirs)
            entry = _CacheEntry(
                source_id=source_id, root=str(root), source_path=str(source), source_hash=prepared.candidate.source_hash
            )
            pending = directory / "entry.pending"
            pending.write_text(entry.model_dump_json(), encoding="utf-8")
            pending.replace(directory / "entry.json")
            return ImportSnapshot(cache_id=directory.name, snapshot=prepared)
        except BaseException:
            if directory.resolve().parent == cache_root:
                shutil.rmtree(directory)
            raise

    async def load(
        self,
        cache_id: str,
        source_id: str,
        root: str,
        source_path: str,
        source_hash: str,
        safe_dirs: Sequence[Path] | None,
        provider: ImportProvider | None = None,
    ) -> PreparedImport:
        return await self._load(self.root, cache_id, source_id, root, source_path, source_hash, safe_dirs, provider)

    @staticmethod
    @in_thread
    def _load(
        cache_root: Path,
        cache_id: str,
        source_id: str,
        root: str,
        source_path: str,
        source_hash: str,
        safe_dirs: Sequence[Path] | None,
        provider: ImportProvider | None,
    ) -> PreparedImport:
        if len(cache_id) != 32 or any(char not in "0123456789abcdef" for char in cache_id):
            raise ValueError("预览缓存标识无效")
        directory = cache_root / cache_id
        if directory.resolve().parent != cache_root:
            raise PermissionError("预览缓存超出缓存目录")
        context = ImportContext(root=Path(root), source=Path(source_path), cache_dir=directory)
        try:
            if (directory / "entry.json").is_file():
                try:
                    entry = _CacheEntry.model_validate_json((directory / "entry.json").read_text(encoding="utf-8"))
                except ValidationError as exc:
                    raise ValueError("缓存记录版本或格式不兼容, 请重新预览") from exc
                if (entry.source_id, entry.root, entry.source_path, entry.source_hash) != (
                    source_id,
                    root,
                    source_path,
                    source_hash,
                ):
                    raise ValueError("预览缓存与提交内容不匹配, 请重新预览")
                if not isinstance(provider, SnapshotImportProvider):
                    raise ValueError("当前插件不支持恢复此快照, 请安装兼容版本")
                _checked(context.source, context.root, safe_dirs, require_exists=False)
                prepared = provider.restore(context)
            else:
                legacy = read_legacy_snapshot(directory)
                if (legacy.source_id, legacy.root, legacy.candidate.source_path, legacy.candidate.source_hash) != (
                    source_id,
                    root,
                    source_path,
                    source_hash,
                ):
                    raise ValueError("预览缓存与提交内容不匹配, 请重新预览")
                prepared = legacy
            if prepared.candidate.source_hash != source_hash:
                raise ValueError("快照来源散列与提交内容不匹配")
            _validate_prepared(prepared, context, safe_dirs)
            os.utime(directory)
            return prepared
        except PermissionError:
            raise
        except OSError as exc:
            raise ValueError("预览缓存不存在或已清理, 请重新预览") from exc

    async def cleanup(self, repo: Repository) -> None:
        async with self.lock:
            protected: set[str] = set()
            offset = 0
            while True:
                tasks = await repo.list_tasks(
                    statuses=[TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.FAILED],
                    task_types=[TaskType.PLUGIN_IMPORT],
                    limit=500,
                    offset=offset,
                )
                for task in tasks:
                    cache_id = task.payload.get("cache_id")
                    if isinstance(cache_id, str):
                        protected.add(cache_id)
                if len(tasks) < 500:
                    break
                offset += len(tasks)
            await self._cleanup(self.root, protected)

    @staticmethod
    @in_thread
    def _cleanup(cache_root: Path, protected: set[str]) -> None:
        if not cache_root.exists():
            return
        cutoff = time.time() - 7 * 24 * 60 * 60
        for directory in cache_root.iterdir():
            if directory.name in protected or directory.is_symlink() or not directory.is_dir():
                continue
            if directory.stat().st_mtime < cutoff and directory.resolve().parent == cache_root:
                shutil.rmtree(directory)
