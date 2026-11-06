import json
from collections.abc import Callable
from pathlib import Path
from typing import override

import pytest
from PIL import Image
from pydantic import BaseModel

from amane.config import HotSettings, PluginConfig
from amane.db.repository import Repository
from amane.handlers.plugin_import import PluginImportHandler, PluginImportPayload
from amane.media import ResourceStore
from amane.plugin import (
    ImportCandidate,
    ImportContext,
    ImportPlugin,
    ImportProgress,
    ImportProvider,
    PreparedImport,
    SnapshotImportProvider,
    SourceCapability,
    SourceDescriptor,
)
from amane.plugins.import_cache import ImportCache
from amane.plugins.manager import PluginManager
from amane.plugins.packaging import load_plugin


class _V1Provider(ImportProvider):
    def __init__(self, delegate: ImportProvider) -> None:
        self._delegate = delegate

    @override
    def discover(self, root: Path, *, recursive: bool) -> tuple[Path, ...]:
        return self._delegate.discover(root, recursive=recursive)

    @override
    def read(self, path: Path) -> ImportCandidate:
        return self._delegate.read(path)


@pytest.mark.asyncio
async def test_v1_provider_and_version_validation(
    nfo_provider: SnapshotImportProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class LegacyPlugin(ImportPlugin):
        @classmethod
        @override
        def descriptor(cls) -> SourceDescriptor:
            return SourceDescriptor(
                id="legacy.nfo", name="Legacy", version="1", capabilities=frozenset({SourceCapability.METADATA_IMPORT})
            )

        @override
        def build_importer(self, config: BaseModel) -> ImportProvider:
            return _V1Provider(nfo_provider)

    manager = PluginManager({"legacy.nfo": LegacyPlugin()}, [])
    source = tmp_path / "ABC-123.nfo"
    source.write_text("<movie><num>ABC-123</num></movie>", encoding="utf-8")
    cache = ImportCache(tmp_path / "data")
    provider = manager.build_import_provider("legacy.nfo", PluginConfig())
    result = await cache.stage(provider, "legacy.nfo", source, tmp_path, [tmp_path])
    assert result.cache_id is None and result.snapshot.candidate.metadata.number == "ABC-123"
    assert not cache.root.exists()
    monkeypatch.setattr(LegacyPlugin, "import_api_version", 2)
    with pytest.raises(TypeError, match="SnapshotImportProvider"):
        manager.build_import_provider("legacy.nfo", PluginConfig())


@pytest.mark.asyncio
async def test_legacy_snapshot_and_task_survive_plugin_update(
    nfo_provider: SnapshotImportProvider,
    repo: Repository,
    resource_store: ResourceStore,
    tmp_path: Path,
) -> None:
    cloud = tmp_path / "cloud"
    cloud.mkdir()
    source = cloud / "ABC-123-UC.nfo"
    source.write_text("<movie><num>ABC-123</num><title>旧快照标题</title></movie>", encoding="utf-8")
    video = cloud / "ABC-123-UC.mp4"
    video.write_bytes(b"video")
    Image.new("RGB", (20, 30), "red").save(cloud / "poster.jpg")
    candidate = nfo_provider.read(source)
    cache = ImportCache(resource_store.data_dir)
    cache_id = "a" * 32
    directory = cache.root / cache_id
    directory.mkdir(parents=True)
    (directory / "source.nfo").write_bytes(source.read_bytes())
    (directory / "poster.jpg").write_bytes((cloud / "poster.jpg").read_bytes())
    # 固定旧持久化结构, 不使用新模型生成兼容输入.
    (directory / "snapshot.json").write_text(
        json.dumps(
            {
                "source_id": "local.nfo",
                "root": str(cloud),
                "candidate": candidate.model_dump(mode="json"),
                "images": [{"kind": "poster", "path": "poster.jpg"}],
                "media": [{"path": str(video), "size": 5, "subtitle_extensions": [".srt"]}],
                "source_file": "source.nfo",
            }
        ),
        encoding="utf-8",
    )
    library = await repo.create_library(name="旧快照", path=str(cloud))
    cloud.rename(tmp_path / "offline")
    for provider in (_V1Provider(nfo_provider), nfo_provider):
        restored = await cache.load(
            cache_id, "local.nfo", str(cloud), str(source), candidate.source_hash, [cloud], provider
        )
        assert restored.candidate.metadata.title == "旧快照标题"
    loaded = load_plugin("local.nfo", Path(__file__).resolve().parents[2] / "extensions/nfo-import")
    manager = PluginManager({"local.nfo": loaded.plugin}, [])
    handler = PluginImportHandler(repo, resource_store, manager, HotSettings(), [cloud], cache)
    payload = PluginImportPayload(
        source_id="local.nfo",
        root=str(cloud),
        source_path=str(source),
        source_hash=candidate.source_hash,
        cache_id=cache_id,
        library_id=library.id,
    )
    for _ in range(2):
        assert (await handler.handle(payload)).success
    meta = await repo.get_metadata_by_number("ABC-123")
    assert meta is not None and meta.id is not None
    files = await repo.get_media_by_metadata_id(meta.id)
    assert len(files) == 1 and files[0].path == str(video) and files[0].has_subtitle
    assert files[0].organized
    assert len(await resource_store.list_all()) == 1


@pytest.mark.asyncio
async def test_new_snapshot_rejects_incompatible_provider_and_unready_cache(
    nfo_provider: SnapshotImportProvider, tmp_path: Path
) -> None:
    source = tmp_path / "ABC-123.nfo"
    source.write_text("<movie><num>ABC-123</num></movie>", encoding="utf-8")
    cache = ImportCache(tmp_path / "data")
    staged = await cache.stage(nfo_provider, "local.nfo", source, tmp_path, [tmp_path])
    assert staged.cache_id is not None
    args = (staged.cache_id, "local.nfo", str(tmp_path), str(source), staged.snapshot.candidate.source_hash, [tmp_path])
    with pytest.raises(ValueError, match="不支持恢复"):
        await cache.load(*args, _V1Provider(nfo_provider))
    record = cache.root / staged.cache_id / "entry.json"
    content = record.read_text(encoding="utf-8")
    record.write_text(content.replace('"version":2', '"version":999'), encoding="utf-8")
    with pytest.raises(ValueError, match="记录版本或格式不兼容"):
        await cache.load(*args, nfo_provider)
    record.unlink()
    with pytest.raises(ValueError, match="不存在或已清理"):
        await cache.load(*args, nfo_provider)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["image", "media"])
async def test_host_rejects_invalid_prepared_paths(
    nfo_provider: SnapshotImportProvider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    cloud = tmp_path / "cloud"
    cloud.mkdir()
    source = cloud / "ABC-123.nfo"
    source.write_text("<movie><num>ABC-123</num></movie>", encoding="utf-8")
    (cloud / "ABC-123.mp4").write_bytes(b"video")
    image = cloud / "poster.jpg"
    Image.new("RGB", (20, 30), "red").save(image)
    original = image.read_bytes()
    prepare = nfo_provider.prepare

    def invalid(context: ImportContext, report: Callable[[ImportProgress], None]) -> PreparedImport:
        result = prepare(context, report)
        if mode == "image":
            result.images[0].path = str(image)
        else:
            result.media = []
        return result

    monkeypatch.setattr(nfo_provider, "prepare", invalid)
    cache = ImportCache(tmp_path / "data")
    with pytest.raises(ValueError, match=r"路径无效|媒体信息与候选路径不匹配"):
        await cache.stage(nfo_provider, "local.nfo", source, cloud, [cloud])
    assert list(cache.root.iterdir()) == [] and image.read_bytes() == original
