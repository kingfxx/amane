from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...db.models import TaskType
from ...handlers.plugin_import import PluginImportPayload
from ...plugins.import_models import ImportCandidate
from ...plugins.importing import discover_imports, import_provider, read_import
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


class ImportExecuteRequest(BaseModel):
    items: list[PluginImportPayload] = Field(min_length=1, max_length=500)


class ImportExecuteResponse(BaseModel):
    task_ids: list[int]


@router.post("/preview")
async def preview_import(
    req: ImportPreviewRequest, repo: RepoDep, runtime: RuntimeDep, manager: PluginManagerDep
) -> ImportPreviewResponse:
    root = await validate_directory_path(req.path, runtime.safe_dirs)
    try:
        provider = import_provider(manager, runtime.config.hot, req.source_id)
        sources = await discover_imports(provider, root, req.recursive, runtime.safe_dirs)
    except PermissionError as exc:
        raise HTTPException(403, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    items: list[ImportPreviewItem] = []
    for path in sources:
        try:
            candidate = await read_import(provider, path, root, runtime.safe_dirs)
            existing = await repo.get_metadata_by_number(candidate.metadata.number)
            items.append(
                ImportPreviewItem(
                    path=str(path), candidate=candidate, existing_metadata_id=existing.id if existing else None
                )
            )
        except (OSError, ValueError) as exc:
            items.append(ImportPreviewItem(path=str(path), error=str(exc)))
    return ImportPreviewResponse(root=str(root), items=items)


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
