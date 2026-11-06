"""每个来源文件独立执行与提交, 按配置派生演员刮削任务."""

from collections.abc import Sequence
from pathlib import Path
from typing import cast

from ..config import HotSettings
from ..db.models import ImportRecord, MediaFile, MediaFileStatus
from ..db.repo_types import MetadataFields
from ..db.repository import Repository
from ..enums import MetadataField
from ..library import has_companion_subtitle
from ..media import ResourceStore
from ..plugins.import_cache import ImportCache
from ..plugins.importing import import_provider, read_import
from ..plugins.manager import PluginManager
from ..utils.path import nfc_path, resolved_path
from ..utils.threads import in_thread
from ._common import actor_scrape_followups
from .models import PluginImportPayload, PluginImportResult
from .protocol import TaskHandler, TaskResult


@in_thread
def _media_path(path: str, library_path: str) -> tuple[str, int]:
    candidate = resolved_path(path)
    if not candidate.is_relative_to(resolved_path(library_path)) or not candidate.is_file():
        raise ValueError("关联正片不在所选媒体库内或不是文件")
    return nfc_path(str(candidate)), candidate.stat().st_size


class PluginImportHandler(TaskHandler[PluginImportPayload, PluginImportResult]):
    def __init__(
        self,
        repo: Repository,
        store: ResourceStore,
        manager: PluginManager | None,
        hot: HotSettings,
        safe_dirs: Sequence[Path] | None,
        import_cache: ImportCache | None = None,
    ) -> None:
        super().__init__(PluginImportPayload, PluginImportResult)
        self._repo = repo
        self._store = store
        self._manager = manager
        self._hot = hot
        self._safe_dirs = safe_dirs
        self._cache = import_cache or ImportCache(store.data_dir)

    async def handle(self, payload: PluginImportPayload) -> TaskResult[PluginImportResult]:
        if self._manager is None:
            raise ValueError("导入插件目录未初始化")
        provider = import_provider(self._manager, self._hot, payload.source_id)
        snapshot = None
        if payload.cache_id is not None:
            async with self._cache.lock:
                snapshot = await self._cache.load(
                    payload.cache_id,
                    payload.source_id,
                    payload.root,
                    payload.source_path,
                    payload.source_hash,
                    self._safe_dirs,
                    provider,
                )
            candidate = snapshot.candidate
        else:
            candidate = await read_import(provider, Path(payload.source_path), Path(payload.root), self._safe_dirs)
        if candidate.source_hash != payload.source_hash:
            raise ValueError("来源文件在预览后发生变化, 请重新预览")
        files: list[MediaFile] = []
        if payload.library_id is not None:
            library = await self._repo.get_library(payload.library_id)
            if library is None:
                raise ValueError("所选媒体库不存在")
            for path in candidate.media_paths:
                if snapshot is not None:
                    cached_media = next(item for item in snapshot.media if item.path == nfc_path(path))
                    if not Path(cached_media.path).is_relative_to(Path(library.path)):
                        raise ValueError("关联正片不在所选媒体库内")
                    actual, size = cached_media.path, cached_media.size
                    has_subtitle = bool(set(cached_media.subtitle_extensions) & set(library.subtitle_extensions))
                else:
                    actual, size = await _media_path(path, library.path)
                    has_subtitle = await has_companion_subtitle(Path(actual), library.subtitle_extensions)
                files.append(
                    MediaFile(
                        library_id=payload.library_id,
                        path=actual,
                        size=size,
                        number=candidate.metadata.number,
                        status=MediaFileStatus.SCRAPED,
                        has_external_subtitle=has_subtitle,
                    )
                )
        images: dict[str, list[str]] = {"poster": [], "thumb": [], "still": []}
        for index, image in enumerate(snapshot.images if snapshot is not None else candidate.images):
            images[image.kind].append(await self._store.import_image(Path(image.path)))
            await self.report_progress(index + 1, len(candidate.images) + 1, "import")
        data = candidate.metadata
        sources = {
            key: payload.source_id
            for key, value in data.model_dump().items()
            if key in {str(field) for field in MetadataField} and value not in (None, "", [], {})
        }
        for key, kind in (("poster_urls", "poster"), ("thumb_urls", "thumb"), ("extrafanart", "still")):
            if images[kind]:
                sources[key] = payload.source_id
        fields: MetadataFields = {
            "title": data.title,
            "actors": [actor.name for actor in data.actors],
            "studio": data.studio,
            "publisher": data.publisher,
            "release": data.release,
            "runtime": data.runtime,
            "tags": data.tags,
            "series": data.series,
            "plot": data.plot,
            "directors": data.directors,
            "poster_urls": list(dict.fromkeys([*images["poster"], *data.poster_urls])),
            "thumb_urls": list(dict.fromkeys([*images["thumb"], *data.thumb_urls])),
            "trailer_urls": data.trailer_urls,
            "extrafanart_urls": {payload.source_id: [*images["still"], *data.extrafanart]}
            if images["still"] or data.extrafanart
            else {},
            "scores": {payload.source_id: data.score} if data.score is not None else {},
            "raw": {payload.source_id: data.model_dump(mode="json")},
            "field_sources": sources,
        }
        if payload.overwrite_existing:
            fields = cast(
                "MetadataFields", {key: value for key, value in fields.items() if value not in (None, "", [], {})}
            )
        meta = await self._repo.upsert_metadata(
            number=data.number,
            fill_only=not payload.overwrite_existing,
            merge_raw=True,
            **fields,
            import_record=ImportRecord(
                metadata_id=0,
                source_id=payload.source_id,
                source_path=candidate.source_path,
                source_hash=candidate.source_hash,
                local_trailer_paths=candidate.local_trailer_paths,
            ),
            import_files=files,
        )
        assert meta.id is not None
        return TaskResult(
            success=True,
            followups=await actor_scrape_followups(self._repo, self._hot, meta.actors),
            result=PluginImportResult(
                metadata_id=meta.id, images=len(candidate.images), local_trailer_paths=candidate.local_trailer_paths
            ),
        )
