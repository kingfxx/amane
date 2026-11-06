import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from contextlib import aclosing, suppress
from pathlib import Path
from threading import Event

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...app.runtime import AppRuntime
from ...db.models import TaskType
from ...db.repository import Repository
from ...handlers.plugin_import import PluginImportPayload
from ...plugins.api import ImportProvider
from ...plugins.import_cache import ImportCache
from ...plugins.import_models import ImportCandidate, ImportProgress
from ...plugins.importing import discover_imports, import_provider, read_import
from ...plugins.manager import PluginManager
from ..deps import PluginManagerDep, RepoDep, RuntimeDep
from ..support.path_validation import validate_directory_path

router = APIRouter(prefix="/imports", tags=["imports"])


class ImportPreviewRequest(BaseModel):
    source_id: str
    path: str
    recursive: bool = False


class ImportPreviewItem(BaseModel):
    path: str
    candidate: ImportCandidate | None = None
    existing_metadata_id: int | None = None
    error: str | None = None
    cache_id: str | None = None


class ImportPreviewResponse(BaseModel):
    root: str
    items: list[ImportPreviewItem]


class ImportPreviewProgress(BaseModel):
    root: str
    total: int
    processed: int
    item: ImportPreviewItem | None = None
    complete: bool = False
    current_path: str | None = None
    plugin_progress: ImportProgress | None = None
    discovering: bool = False
    error: str | None = None


class _PreviewStream(StreamingResponse):
    media_type = "text/event-stream"


class _PreviewUpdates:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[ImportProgress | None] = asyncio.Queue()
        self.loop = asyncio.get_running_loop()
        self.cancelled = Event()

    def report(self, progress: ImportProgress) -> None:
        if self.cancelled.is_set():
            raise asyncio.CancelledError
        self.loop.call_soon_threadsafe(self.queue.put_nowait, progress)

    def finish(self, task: object) -> None:
        self.queue.put_nowait(None)

    async def follow[T](self, work: Awaitable[T]) -> AsyncGenerator[ImportProgress | T]:
        task = asyncio.ensure_future(work)
        task.add_done_callback(self.finish)
        try:
            while (update := await self.queue.get()) is not None:
                yield update
            yield await task
        finally:
            self.cancelled.set()
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task


class ImportExecuteRequest(BaseModel):
    items: list[PluginImportPayload] = Field(min_length=1, max_length=500)


class ImportExecuteResponse(BaseModel):
    task_ids: list[int]


async def _preview_context(
    req: ImportPreviewRequest, runtime: AppRuntime, manager: PluginManager
) -> tuple[Path, ImportProvider]:
    root = await validate_directory_path(req.path, runtime.safe_dirs)
    await runtime.import_cache.cleanup(runtime.repo)
    try:
        provider = import_provider(manager, runtime.config.hot, req.source_id)
    except PermissionError as exc:
        raise HTTPException(403, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    return root, provider


async def _preview_item(
    provider: ImportProvider,
    path: Path,
    root: Path,
    safe_dirs: Sequence[Path] | None,
    repo: Repository,
    source_id: str,
    cache: ImportCache,
    report: Callable[[ImportProgress], None] | None = None,
) -> ImportPreviewItem:
    try:
        staged = (
            await cache.stage(provider, source_id, path, root, safe_dirs, report)
            if report
            else await cache.stage(provider, source_id, path, root, safe_dirs)
        )
        candidate = staged.snapshot.candidate
        existing = await repo.get_metadata_by_number(candidate.metadata.number)
        return ImportPreviewItem(
            path=str(path),
            candidate=candidate,
            existing_metadata_id=existing.id if existing else None,
            cache_id=staged.cache_id,
        )
    except (OSError, ValueError) as exc:
        return ImportPreviewItem(path=str(path), error=str(exc))


@router.post("/preview")
async def preview_import(
    req: ImportPreviewRequest, repo: RepoDep, runtime: RuntimeDep, manager: PluginManagerDep
) -> ImportPreviewResponse:
    root, provider = await _preview_context(req, runtime, manager)
    try:
        sources = await discover_imports(provider, root, req.recursive, runtime.safe_dirs)
    except PermissionError as exc:
        raise HTTPException(403, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    items = [
        await _preview_item(provider, path, root, runtime.safe_dirs, repo, req.source_id, runtime.import_cache)
        for path in sources
    ]
    return ImportPreviewResponse(root=str(root), items=items)


@router.post("/preview-stream", response_class=_PreviewStream, responses={200: {"model": ImportPreviewProgress}})
async def preview_import_stream(
    req: ImportPreviewRequest, repo: RepoDep, runtime: RuntimeDep, manager: PluginManagerDep
) -> StreamingResponse:
    root, provider = await _preview_context(req, runtime, manager)

    async def events() -> AsyncIterator[str]:
        progress = ImportPreviewProgress(root=str(root), total=0, processed=0, discovering=True)
        yield f"data: {progress.model_dump_json()}\n\n"
        discovery = _PreviewUpdates()
        sources: tuple[Path, ...] = ()
        try:
            async with aclosing(
                discovery.follow(discover_imports(provider, root, req.recursive, runtime.safe_dirs, discovery.report))
            ) as stream:
                async for update in stream:
                    if isinstance(update, ImportProgress):
                        progress.plugin_progress = update
                        yield f"data: {progress.model_dump_json()}\n\n"
                    else:
                        sources = update
        except (OSError, ValueError) as exc:
            progress.error = str(exc)
            yield f"data: {progress.model_dump_json()}\n\n"
            return
        progress.discovering = False
        progress.total = len(sources)
        progress.plugin_progress = None
        yield f"data: {progress.model_dump_json()}\n\n"
        for path in sources:
            progress.item = None
            progress.current_path = str(path)
            progress.plugin_progress = None
            yield f"data: {progress.model_dump_json()}\n\n"
            updates = _PreviewUpdates()
            async with aclosing(
                updates.follow(
                    _preview_item(
                        provider,
                        path,
                        root,
                        runtime.safe_dirs,
                        repo,
                        req.source_id,
                        runtime.import_cache,
                        updates.report,
                    )
                )
            ) as stream:
                async for update in stream:
                    if isinstance(update, ImportProgress):
                        progress.plugin_progress = update
                        yield f"data: {progress.model_dump_json()}\n\n"
                    else:
                        progress.item = update
            progress.processed += 1
            yield f"data: {progress.model_dump_json()}\n\n"
        progress.item = None
        progress.complete = True
        progress.current_path = None
        progress.plugin_progress = None
        yield f"data: {progress.model_dump_json()}\n\n"

    return _PreviewStream(events(), headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/execute", status_code=202)
async def execute_import(
    req: ImportExecuteRequest, repo: RepoDep, runtime: RuntimeDep, manager: PluginManagerDep
) -> ImportExecuteResponse:
    # 校验和入队期间保护缓存; 不接受客户端直接提交解析结果或本地缓存路径.
    async with runtime.import_cache.lock:
        for item in req.items:
            try:
                provider = import_provider(manager, runtime.config.hot, item.source_id)
                if item.cache_id is not None:
                    snapshot = await runtime.import_cache.load(
                        item.cache_id,
                        item.source_id,
                        item.root,
                        item.source_path,
                        item.source_hash,
                        runtime.safe_dirs,
                        provider,
                    )
                    candidate = snapshot.candidate
                else:
                    root = await validate_directory_path(item.root, runtime.safe_dirs)
                    candidate = await read_import(provider, Path(item.source_path), root, runtime.safe_dirs)
                    item.root = str(root)
            except PermissionError as exc:
                raise HTTPException(403, detail=str(exc)) from exc
            except (OSError, ValueError) as exc:
                raise HTTPException(422, detail=str(exc)) from exc
            if candidate.source_hash != item.source_hash:
                raise HTTPException(409, detail="来源文件在预览后发生变化, 请重新预览")
            if item.library_id is not None and await repo.get_library(item.library_id) is None:
                raise HTTPException(404, detail="所选媒体库不存在")
            item.source_path = candidate.source_path
        task_ids: list[int] = []
        for item in req.items:
            task = await repo.create_task(TaskType.PLUGIN_IMPORT, item)
            assert task.id is not None
            task_ids.append(task.id)
    return ImportExecuteResponse(task_ids=task_ids)


class ImportRecordResponse(BaseModel):
    source_id: str
    source_path: str
    local_trailer_paths: list[str]


@router.get("/{metadata_id}/records")
async def get_import_records(metadata_id: int, repo: RepoDep) -> list[ImportRecordResponse]:
    if await repo.get_metadata(metadata_id) is None:
        raise HTTPException(404, detail="影片不存在")
    return [
        ImportRecordResponse(
            source_id=record.source_id, source_path=record.source_path, local_trailer_paths=record.local_trailer_paths
        )
        for record in await repo.list_import_records(metadata_id)
    ]
