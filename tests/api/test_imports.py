import asyncio
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from httpx2 import AsyncClient


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
    body = {
        "items": [
            {
                "source_id": "local.nfo",
                "root": str(directory),
                "source_path": str(source),
                "source_hash": candidate["source_hash"],
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
    assert (await client.post("imports/execute", json=body)).status_code == 409
    assert (await client.post("imports/preview", json={**request, "path": str(tmp_path)})).status_code == 403
    assert (await client.post("imports/execute", json={"items": []})).status_code == 422
    assert (
        await client.post("imports/execute", json={"items": [{**body["items"][0], "overwrite_existing": "invalid"}]})
    ).status_code == 422
    assert (await client.patch("plugins/local.nfo", json={"enabled": False})).status_code == 200
    assert (await client.post("imports/preview", json=request)).status_code == 422
