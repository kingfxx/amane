import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from amane.config import HotSettings
from amane.handlers import OrganizeHandler, OrganizePayload

if TYPE_CHECKING:
    from amane.db.repository import Repository
    from amane.media import ResourceStore


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_organized", [False, True])
async def test_organize_marker_preserves_files_until_forced(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, initially_organized: bool
) -> None:
    root = tmp_path / "library"
    source = root / "original" / "ABC-123.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    old_nfo = source.with_suffix(".nfo")
    old_nfo.write_bytes(b"original nfo")
    library = await repo.create_library(name="t", path=str(root), video_template="first/{number}.{ext}")
    assert library.id is not None
    meta = await repo.upsert_metadata("ABC-123", title="title")
    assert meta.id is not None
    media = await repo.create_media_file(
        library.id, path=str(source), metadata_id=meta.id, organized=initially_organized
    )
    assert media.id is not None
    handler = OrganizeHandler(repo, HotSettings(), resource_store)
    payload = OrganizePayload(library_id=library.id, path=str(root))
    first = await handler.handle(payload)
    assert first.result is not None
    assert (first.result.organized, first.result.skipped) == (int(not initially_organized), int(initially_organized))
    indexed = await repo.get_media_file(media.id)
    assert indexed is not None and indexed.organized
    placed = Path(indexed.path)
    placed_nfo = placed.with_suffix(".nfo")
    placed_nfo.write_bytes(b"custom nfo")
    before = {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    await repo.update_library(library.id, video_template="second/{number}.{ext}")
    second = await handler.handle(payload)
    assert second.result is not None and second.result.skipped == 1 and second.result.organized == 0
    after = {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    assert before == after
    forced = await handler.handle(payload.model_copy(update={"force": True}))
    assert forced.result is not None and forced.result.organized == 1
    indexed = await repo.get_media_file(media.id)
    assert indexed is not None and indexed.organized and indexed.path == str(root / "second" / "ABC-123.mp4")
    assert Path(indexed.path).with_suffix(".nfo").read_bytes() != b"custom nfo"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["conflict", "exception", "cancel"])
async def test_forced_organize_failure_clears_marker(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, failure: str
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    source = root / "ABC-123.mp4"
    source.write_bytes(b"source")
    library = await repo.create_library(name="t", path=str(root), video_template="target/{number}.{ext}")
    assert library.id is not None
    meta = await repo.upsert_metadata("ABC-123")
    media = await repo.create_media_file(library.id, path=str(source), metadata_id=meta.id, organized=True)
    assert media.id is not None
    handler = OrganizeHandler(repo, HotSettings(), resource_store)
    payload = OrganizePayload(library_id=library.id, path=str(root), force=True)
    if failure == "conflict":
        target = root / "target" / "ABC-123.mp4"
        target.parent.mkdir()
        target.write_bytes(b"occupied")
        result = await handler.handle(payload)
        assert result.result is not None and result.result.conflicted == 1
        assert target.read_bytes() == b"occupied"
    else:
        error = asyncio.CancelledError if failure == "cancel" else OSError
        with (
            patch("amane.handlers.file.apply_file_operations", AsyncMock(side_effect=error("interrupted"))),
            pytest.raises(error, match="interrupted"),
        ):
            await handler.handle(payload)
    indexed = await repo.get_media_file(media.id)
    assert indexed is not None and not indexed.organized
    assert source.read_bytes() == b"source"
