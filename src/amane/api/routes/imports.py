from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ...app.runtime import AppRuntime
from ...db.models import TaskType
from ...db.repository import Repository
from ...handlers.plugin_import import PluginImportPayload
from ...plugins.api import ImportProvider
from ...plugins.import_models import ImportCandidate
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


class ImportPreviewResponse(BaseModel):
    root: str
    items: list[ImportPreviewItem]


class ImportPreviewProgress(BaseModel):
    root: str
    total: int
    processed: int
    item: ImportPreviewItem | None = None
    complete: bool = False


class _PreviewStream(StreamingResponse):
    media_type = "text/event-stream"


class ImportExecuteRequest(BaseModel):
    items: list[PluginImportPayload] = Field(min_length=1, max_length=500)


class ImportExecuteResponse(BaseModel):
    task_ids: list[int]


async def _preview_sources(
    req: ImportPreviewRequest, runtime: AppRuntime, manager: PluginManager
) -> tuple[Path, ImportProvider, tuple[Path, ...]]:
    root = await validate_directory_path(req.path, runtime.safe_dirs)
    try:
        provider = import_provider(manager, runtime.config.hot, req.source_id)
        sources = await discover_imports(provider, root, req.recursive, runtime.safe_dirs)
    except PermissionError as exc:
        raise HTTPException(403, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    return root, provider, sources


async def _preview_item(
    provider: ImportProvider, path: Path, root: Path, safe_dirs: Sequence[Path] | None, repo: Repository
) -> ImportPreviewItem:
    try:
        candidate = await read_import(provider, path, root, safe_dirs)
        existing = await repo.get_metadata_by_number(candidate.metadata.number)
        return ImportPreviewItem(
            path=str(path), candidate=candidate, existing_metadata_id=existing.id if existing else None
        )
    except (OSError, ValueError) as exc:
        return ImportPreviewItem(path=str(path), error=str(exc))


@router.post("/preview")
async def preview_import(
    req: ImportPreviewRequest, repo: RepoDep, runtime: RuntimeDep, manager: PluginManagerDep
) -> ImportPreviewResponse:
    root, provider, sources = await _preview_sources(req, runtime, manager)
    items = [await _preview_item(provider, path, root, runtime.safe_dirs, repo) for path in sources]
    return ImportPreviewResponse(root=str(root), items=items)


@router.post("/preview-stream", response_class=_PreviewStream, responses={200: {"model": ImportPreviewProgress}})
async def preview_import_stream(
    req: ImportPreviewRequest, repo: RepoDep, runtime: RuntimeDep, manager: PluginManagerDep
) -> StreamingResponse:
    root, provider, sources = await _preview_sources(req, runtime, manager)

    async def events() -> AsyncIterator[str]:
        progress = ImportPreviewProgress(root=str(root), total=len(sources), processed=0)
        yield f"data: {progress.model_dump_json()}\n\n"
        for path in sources:
            progress.item = await _preview_item(provider, path, root, runtime.safe_dirs, repo)
            progress.processed += 1
            yield f"data: {progress.model_dump_json()}\n\n"
        progress.item = None
        progress.complete = True
        yield f"data: {progress.model_dump_json()}\n\n"

    return _PreviewStream(events(), headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/execute", status_code=202)
async def execute_import(
    req: ImportExecuteRequest, repo: RepoDep, runtime: RuntimeDep, manager: PluginManagerDep
) -> ImportExecuteResponse:
    # 全部候选先校验再入队; Worker 执行前仍重新读取, 不能信任预览时的文件状态.
    for item in req.items:
        root = await validate_directory_path(item.root, runtime.safe_dirs)
        try:
            provider = import_provider(manager, runtime.config.hot, item.source_id)
            candidate = await read_import(provider, Path(item.source_path), root, runtime.safe_dirs)
        except PermissionError as exc:
            raise HTTPException(403, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        if candidate.source_hash != item.source_hash:
            raise HTTPException(409, detail="来源文件在预览后发生变化, 请重新预览")
        if item.library_id is not None and await repo.get_library(item.library_id) is None:
            raise HTTPException(404, detail="所选媒体库不存在")
        item.root = str(root)
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
