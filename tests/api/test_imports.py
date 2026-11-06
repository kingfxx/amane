import asyncio
from collections.abc import Callable, Sequence
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from httpx2 import AsyncClient

from amane.api.routes.imports import ImportPreviewProgress, _PreviewUpdates
from amane.plugins.api import ImportProvider
from amane.plugins.import_models import ImportProgress


@pytest.mark.asyncio
async def test_import_api_install_preview_execute_and_errors(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = BytesIO()
    with ZipFile(archive, "w") as target:
        target.write(Path(__file__).resolve().parents[2] / "extensions/nfo-import/plugin.py", "plugin.py")
    installed = await client.post("plugins", files={"file": ("local.nfo.zip", archive.getvalue(), "application/zip")})
    assert installed.status_code == 201, installed.text
    directory = tmp_path / "files" / "nfo"
    directory.mkdir()
    source = directory / "JUR-840.nfo"
    source.write_text("<movie><num>JUR-840</num><title>JUR-840 标题</title></movie>", encoding="utf-8")
    request = {"source_id": "local.nfo", "path": str(directory)}
    preview = await client.post("imports/preview", json=request)
    assert preview.status_code == 200, preview.text
    candidate = preview.json()["items"][0]["candidate"]
    (directory / "invalid.nfo").write_text("<movie>", encoding="utf-8")
    streamed = await client.post("imports/preview-stream", json=request)
    assert streamed.status_code == 200, streamed.text
    assert streamed.headers["content-type"].startswith("text/event-stream")
    events = [
        ImportPreviewProgress.model_validate_json(line.removeprefix("data: "))
        for line in streamed.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[0].processed == 0 and events[-1].processed == 2 and events[-1].complete
    assert all(event.total == 2 for event in events if not event.discovering)
    assert [event.processed for event in events if event.item is not None] == [1, 2]
    assert {event.plugin_progress.phase for event in events if event.plugin_progress is not None} >= {
        "discover",
        "read",
        "cache",
    }
    results = {Path(event.item.path).name: event.item for event in events if event.item is not None}
    assert results["JUR-840.nfo"].candidate is not None
    assert results["JUR-840.nfo"].candidate.metadata.number == "JUR-840"
    assert "XML 格式" in (results["invalid.nfo"].error or "")
    empty = directory / "empty"
    empty.mkdir()
    empty_stream = await client.post("imports/preview-stream", json={**request, "path": str(empty)})
    empty_events = [
        ImportPreviewProgress.model_validate_json(line.removeprefix("data: "))
        for line in empty_stream.text.splitlines()
        if line.startswith("data: ")
    ]
    assert all(event.total == event.processed == 0 and event.item is None for event in empty_events)
    assert empty_events[-1].complete

    async def unavailable(
        provider: ImportProvider,
        root: Path,
        recursive: bool,
        safe_dirs: Sequence[Path] | None,
        report: Callable[[ImportProgress], None] | None = None,
    ) -> tuple[Path, ...]:
        raise OSError("挂载目录不可用")

    with monkeypatch.context() as patch:
        patch.setattr("amane.api.routes.imports.discover_imports", unavailable)
        failed = await client.post("imports/preview-stream", json=request)
    assert failed.status_code == 200
    failed_events = [
        ImportPreviewProgress.model_validate_json(line.removeprefix("data: "))
        for line in failed.text.splitlines()
        if line.startswith("data: ")
    ]
    assert failed_events[-1].error == "挂载目录不可用"
    assert all(not event.complete and event.item is None for event in failed_events)
    body = {
        "items": [
            {
                "source_id": "local.nfo",
                "root": str(directory),
                "source_path": str(source),
                "source_hash": candidate["source_hash"],
                "cache_id": preview.json()["items"][0]["cache_id"],
                "overwrite_existing": True,
            }
        ]
    }
    executed = await client.post("imports/execute", json=body)
    assert executed.status_code == 202, executed.text
    task_id = executed.json()["task_ids"][0]
    async with asyncio.timeout(30):
        while True:
            task = (await client.get(f"tasks/{task_id}")).json()
            if task["status"] in {"done", "failed"}:
                break
            await asyncio.sleep(0.05)
    assert task["status"] == "done", task
    assert task["payload"]["overwrite_existing"] is True
    metadata_id = task["result"]["metadata_id"]
    records = await client.get(f"imports/{metadata_id}/records")
    assert records.status_code == 200 and records.json()[0]["source_path"] == str(source.resolve())
    source.write_text("<movie><num>JUR-840</num><title>变化</title></movie>", encoding="utf-8")
    assert (await client.post("imports/execute", json=body)).status_code == 202
    legacy = {**body["items"][0], "cache_id": None}
    assert (await client.post("imports/execute", json={"items": [legacy]})).status_code == 409
    mismatch = {**body["items"][0], "source_hash": "0" * 64}
    assert (await client.post("imports/execute", json={"items": [mismatch]})).status_code == 422
    assert (await client.post("imports/preview", json={**request, "path": str(tmp_path)})).status_code == 403
    assert (await client.post("imports/preview-stream", json={**request, "path": str(tmp_path)})).status_code == 403
    assert (await client.post("imports/execute", json={"items": []})).status_code == 422
    oversized = await client.post("imports/execute", json={"items": body["items"] * 501})
    assert oversized.status_code == 422
    assert oversized.json()["detail"][0]["type"] == "too_long"
    assert (
        await client.post("imports/execute", json={"items": [{**body["items"][0], "overwrite_existing": "invalid"}]})
    ).status_code == 422
    assert (await client.patch("plugins/local.nfo", json={"enabled": False})).status_code == 200
    assert (await client.post("imports/preview", json=request)).status_code == 422
    assert (await client.post("imports/preview-stream", json=request)).status_code == 422


@pytest.mark.asyncio
async def test_preview_progress_cancels_unfinished_work() -> None:
    updates = _PreviewUpdates()
    stopped = asyncio.Event()
    gate = asyncio.Event()

    async def work() -> int:
        try:
            updates.report(ImportProgress(phase="cache", processed=1, total=2))
            await gate.wait()
            return 2
        finally:
            stopped.set()

    stream = updates.follow(work())
    first = await anext(stream)
    assert isinstance(first, ImportProgress) and first.processed == 1
    assert not stopped.is_set()
    await stream.aclose()
    assert stopped.is_set()
    with pytest.raises(asyncio.CancelledError):
        updates.report(ImportProgress(phase="cache", processed=2, total=2))

    async def failed() -> int:
        raise ValueError("准备失败")

    with pytest.raises(ValueError, match="准备失败"):
        async for _ in _PreviewUpdates().follow(failed()):
            pass
