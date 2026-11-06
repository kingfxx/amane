import asyncio
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from httpx2 import AsyncClient

from amane.api.routes.imports import ImportPreviewProgress


@pytest.mark.asyncio
async def test_import_api_install_preview_execute_and_errors(client: AsyncClient, tmp_path: Path) -> None:
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
    assert [(event.processed, event.total, event.complete) for event in events] == [
        (0, 2, False),
        (0, 2, False),
        (1, 2, False),
        (1, 2, False),
        (2, 2, False),
        (2, 2, True),
    ]
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
    assert [(event.total, event.processed, event.complete) for event in empty_events] == [(0, 0, False), (0, 0, True)]
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
