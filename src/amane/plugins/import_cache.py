"""预览快照持久化; 来源路径保留原值, 图片副本仅供导入任务读取."""

import asyncio
import hashlib
import os
import shutil
import time
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from ..db.models import TaskStatus, TaskType
from ..db.repository import Repository
from ..utils.path import nfc_path
from ..utils.threads import in_thread
from .api import ImportProvider
from .import_models import ImportCandidate, ImportImage
from .importing import read_import


class CachedMedia(BaseModel):
    path: str
    size: int
    subtitle_extensions: list[str]


class CachedImport(BaseModel):
    source_id: str
    root: str
    candidate: ImportCandidate
    images: list[ImportImage]
    media: list[CachedMedia]
    source_file: str


class ImportSnapshot(BaseModel):
    cache_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    snapshot: CachedImport


def _within(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


def _authorize(root: Path, path: Path, safe_dirs: Sequence[Path] | None) -> None:
    if not root.is_absolute() or not path.is_absolute() or ".." in (*root.parts, *path.parts):
        raise PermissionError("导入快照路径必须是规范的绝对路径")
    if not _within(path, root):
        raise PermissionError("导入文件超出所选目录")
    if safe_dirs is not None and not any(_within(path, directory) for directory in safe_dirs):
        raise PermissionError("导入文件不在允许的安全目录内")


def _copy_image(source: Path, dest: Path) -> None:
    with source.open("rb") as stream:
        data = stream.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("导入图片超过 32 MiB")
    dest.write_bytes(data)


class ImportCache:
    def __init__(self, data_dir: Path) -> None:
        self.root = (data_dir / "import-cache").resolve()
        # 提交和清理共用锁, 避免缓存刚被任务引用就被清理.
        self.lock = asyncio.Lock()

    async def stage(
        self, provider: ImportProvider, source_id: str, source: Path, root: Path, safe_dirs: Sequence[Path] | None
    ) -> ImportSnapshot:
        return await self._stage(self.root, provider, source_id, source, root, safe_dirs)

    @staticmethod
    @in_thread
    def _stage(
        cache_root: Path,
        provider: ImportProvider,
        source_id: str,
        source: Path,
        root: Path,
        safe_dirs: Sequence[Path] | None,
    ) -> ImportSnapshot:
        candidate = read_import.sync(provider, source, root, safe_dirs)
        source_data = candidate.source_bytes
        if source_data is None:
            # 兼容未返回原文的导入插件; 支持原文的插件只读取一次来源文件.
            with Path(candidate.source_path).open("rb") as stream:
                source_data = stream.read(16 * 1024 * 1024 + 1)
            if len(source_data) > 16 * 1024 * 1024:
                raise ValueError("导入来源文件超过 16 MiB")
        if hashlib.sha256(source_data).hexdigest() != candidate.source_hash:
            raise ValueError("来源文件在缓存时发生变化, 请重新预览")
        candidate.source_bytes = None
        cache_id = uuid4().hex
        directory = cache_root / cache_id
        directory.mkdir(parents=True)
        try:
            source_file = f"source{source.suffix}"
            (directory / source_file).write_bytes(source_data)
            images: list[ImportImage] = []
            copied: dict[str, str] = {}
            for image in candidate.images:
                name = copied.get(image.path)
                if name is None:
                    name = f"image-{len(copied)}{Path(image.path).suffix}"
                    _copy_image(Path(image.path), directory / name)
                    copied[image.path] = name
                images.append(ImportImage(kind=image.kind, path=name))
            subtitle_extensions: dict[Path, list[str]] = {}
            media: list[CachedMedia] = []
            for value in candidate.media_paths:
                path = Path(value)
                if path.parent not in subtitle_extensions:
                    subtitle_extensions[path.parent] = sorted(
                        {item.suffix.lower() for item in path.parent.iterdir() if item.is_file()}
                    )
                media.append(
                    CachedMedia(
                        path=nfc_path(value),
                        size=path.stat().st_size,
                        subtitle_extensions=subtitle_extensions[path.parent],
                    )
                )
            snapshot = CachedImport(
                source_id=source_id,
                root=str(root),
                candidate=candidate,
                images=images,
                media=media,
                source_file=source_file,
            )
            (directory / "snapshot.json").write_text(snapshot.model_dump_json(), encoding="utf-8")
            return ImportSnapshot(cache_id=cache_id, snapshot=snapshot)
        except BaseException:
            # 目录由本次调用创建, 删除边界固定在缓存根内.
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
    ) -> CachedImport:
        return await self._load(self.root, cache_id, source_id, root, source_path, source_hash, safe_dirs)

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
    ) -> CachedImport:
        if len(cache_id) != 32 or any(char not in "0123456789abcdef" for char in cache_id):
            raise ValueError("预览缓存标识无效")
        directory = cache_root / cache_id
        if directory.resolve().parent != cache_root:
            raise PermissionError("预览缓存超出缓存目录")
        try:
            snapshot = CachedImport.model_validate_json((directory / "snapshot.json").read_text(encoding="utf-8"))
            candidate = snapshot.candidate
            if (snapshot.source_id, snapshot.root, candidate.source_path, candidate.source_hash) != (
                source_id,
                root,
                source_path,
                source_hash,
            ):
                raise ValueError("预览缓存与提交内容不匹配, 请重新预览")
            _authorize(Path(snapshot.root), Path(candidate.source_path), safe_dirs)
            for media in snapshot.media:
                _authorize(Path(snapshot.root), Path(media.path), safe_dirs)
            for image in snapshot.images:
                path = (directory / image.path).resolve()
                if not path.is_relative_to(directory) or not path.is_file():
                    raise ValueError("预览缓存图片缺失或路径无效, 请重新预览")
                image.path = str(path)
            source = (directory / snapshot.source_file).resolve()
            if not source.is_relative_to(directory) or hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
                raise ValueError("预览缓存来源内容无效, 请重新预览")
            os.utime(directory)
            return snapshot
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
            if directory.stat().st_mtime >= cutoff:
                continue
            # 仅删除缓存根的真实直接子目录.
            if directory.resolve().parent == cache_root:
                shutil.rmtree(directory)
