from pathlib import Path

import pytest
from PIL import Image

from amane.config import HotSettings
from amane.db.repository import Repository
from amane.enums import MetadataField
from amane.handlers.plugin_import import PluginImportHandler, PluginImportPayload, _media_path
from amane.media import ResourceStore
from amane.plugin import ImportPlugin, ImportProvider
from amane.plugins.importing import discover_imports, read_import
from amane.plugins.manager import PluginManager
from amane.plugins.packaging import load_plugin


@pytest.fixture
def nfo_provider() -> ImportProvider:
    loaded = load_plugin("local.nfo", Path(__file__).resolve().parents[2] / "extensions/nfo-import")
    assert isinstance(loaded.plugin, ImportPlugin)
    return loaded.plugin.build_importer(loaded.plugin.configuration_model()())


@pytest.mark.asyncio
async def test_discover_large_nfo_directory(nfo_provider: ImportProvider, tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    expected = []
    for index in range(501):
        path = nested / f"ABC-{index:03}.nfo"
        path.write_text("<movie/>", encoding="utf-8")
        expected.append(path)
    (nested / "notes.txt").write_text("notes", encoding="utf-8")
    trash = nested / ".amane_trash"
    trash.mkdir()
    (trash / "ABC-999.nfo").write_text("<movie/>", encoding="utf-8")
    assert await discover_imports(nfo_provider, tmp_path, False, [tmp_path]) == ()
    assert await discover_imports(nfo_provider, tmp_path, True, [tmp_path]) == tuple(sorted(expected))


def test_nfo_parsing_and_invalid_inputs(nfo_provider: ImportProvider, tmp_path: Path) -> None:
    path = tmp_path / "JUR-840.nfo"
    cases = [
        (
            "<movie><num>JUR-840</num><title>JUR-840 标题</title><plot><![CDATA[a & b]]></plot><tag>A</tag><genre>A</genre><runtime>146</runtime></movie>",
            None,
        ),
        ("<movie>", "XML 格式"),
        ("<tvshow/>", "movie"),
        ("<!DOCTYPE movie [<!ENTITY a 'abc'>]><movie/>", "DTD"),
        ("<movie><num>JUR-841</num></movie>", "冲突"),
        ("<movie><runtime>-1</runtime></movie>", "有效范围"),
        ("<movie><rating>NaN</rating></movie>", "有效范围"),
        ("<movie><runtime>1.5</runtime></movie>", "整数"),
        ("<movie><poster>../secret.jpg</poster></movie>", "超出"),
    ]
    for xml, error in cases:
        path.write_text(xml, encoding="utf-8-sig")
        if error:
            with pytest.raises(ValueError, match=error):
                nfo_provider.read(path)
        else:
            result = nfo_provider.read(path)
            assert (result.metadata.number, result.metadata.title, result.metadata.plot, result.metadata.tags) == (
                "JUR-840",
                "标题",
                "a & b",
                ["A"],
            )


def test_nfo_bare_ampersands_preserve_source_and_cdata(nfo_provider: ImportProvider, tmp_path: Path) -> None:
    path = tmp_path / "JUR-840.nfo"
    original = "<movie><num>JUR-840</num><plot><![CDATA[a & b]]></plot><trailer>https://example.test/pv?a=1&b=2</trailer></movie>"
    for encoding in ("utf-8", "utf-16"):
        path.write_text(original, encoding=encoding)
        candidate = nfo_provider.read(path)
        assert candidate.metadata.trailer_urls == ["https://example.test/pv?a=1&b=2"]
        assert candidate.metadata.plot == "a & b" and candidate.warnings
        assert path.read_text(encoding=encoding) == original
    path.write_text(
        '<movie><thumb aspect="poster">https://example.test/poster.jpg</thumb><fanart><thumb>https://example.test/still.jpg</thumb></fanart></movie>',
        encoding="utf-8",
    )
    (tmp_path / "JUR-8400.mp4").write_bytes(b"wrong movie")
    (tmp_path / "JUR-840-CD1.mp4").write_bytes(b"correct movie")
    artwork_candidate = nfo_provider.read(path)
    assert artwork_candidate.media_paths == [str((tmp_path / "JUR-840-CD1.mp4").resolve())]
    artwork = artwork_candidate.metadata
    assert artwork.poster_urls == ["https://example.test/poster.jpg"]
    assert artwork.extrafanart == ["https://example.test/still.jpg"]


def test_nfo_numeric_tag_compatibility(nfo_provider: ImportProvider, tmp_path: Path) -> None:
    path = tmp_path / "JUR-638.nfo"
    literal = "<7mmtvid>原文 & 内容</7mmtvid>"
    cases = [
        ("<7mmtvid>https://example.test/movie</7mmtvid>", 1, None),
        ('<7mmtvid type="source"/>', 1, None),
        ("<7mmtvid>https://example.test/?a=1&b=2</7mmtvid>", 2, None),
        ("<!-- <7mmtvid>原文</7mmtvid> -->", 0, None),
        ("<7mmtvid>地址</7other>", 0, "XML 格式"),
        ("<7mmtvid>地址", 0, "XML 格式"),
        ("<7mmtv!>地址</7mmtv!>", 0, "XML 格式"),
    ]
    for encoding in ("utf-8", "utf-16"):
        for extra, warnings, error in cases:
            xml = (
                f"<movie><num>JUR-638</num><title>JUR-638 标题</title><plot><![CDATA[{literal}]]></plot>{extra}</movie>"
            )
            path.write_text(xml, encoding=encoding)
            before = path.read_bytes()
            if error:
                with pytest.raises(ValueError, match=error):
                    nfo_provider.read(path)
            else:
                candidate = nfo_provider.read(path)
                assert candidate.metadata.number == "JUR-638" and candidate.metadata.title == "标题"
                assert candidate.metadata.plot == literal
                assert len(candidate.warnings) == warnings
            assert path.read_bytes() == before
        path.write_text(
            '<!DOCTYPE movie [<!ENTITY x SYSTEM "file:///secret">]><movie><7mmtvid>&x;</7mmtvid></movie>',
            encoding=encoding,
        )
        with pytest.raises(ValueError, match="DTD"):
            nfo_provider.read(path)


@pytest.mark.asyncio
async def test_virtual_volume_import_checks(
    nfo_provider: ImportProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "JUR-840.nfo"
    source.write_text("<movie><num>JUR-840</num></movie>", encoding="utf-8")
    media = tmp_path / "JUR-840.mp4"
    media.write_bytes(b"video")
    original_resolve = Path.resolve

    def reject_strict(path: Path, strict: bool = False) -> Path:
        if strict:
            raise OSError(22, "虚拟卷不支持严格规范化 (WinError 1005)", str(path))
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", reject_strict)
    assert await discover_imports(nfo_provider, tmp_path, False, [tmp_path]) == (source,)
    assert (await read_import(nfo_provider, source, tmp_path, [tmp_path])).media_paths == [str(media)]
    assert await _media_path(str(media), str(tmp_path)) == (str(media), 5)
    for path, roots, error, message in (
        (source, [tmp_path / "other"], PermissionError, "安全目录"),
        (tmp_path / ".." / "outside.nfo", [tmp_path], PermissionError, "超出"),
        (tmp_path / "missing.nfo", [tmp_path], FileNotFoundError, "missing.nfo"),
    ):
        with pytest.raises(error, match=message):
            await read_import(nfo_provider, path, tmp_path, roots)
    with pytest.raises(ValueError, match="所选媒体库"):
        await _media_path(str(media), str(tmp_path / "other"))


@pytest.mark.asyncio
async def test_import_overwrite_respects_locks_and_missing_fields(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    loaded = load_plugin("local.nfo", Path(__file__).resolve().parents[2] / "extensions/nfo-import")
    assert isinstance(loaded.plugin, ImportPlugin)
    provider = loaded.plugin.build_importer(loaded.plugin.configuration_model()())
    manager = PluginManager({"local.nfo": loaded.plugin}, [])
    handler = PluginImportHandler(repo, resource_store, manager, HotSettings(), [tmp_path])
    for overwrite, number in ((False, "JUR-638"), (True, "JUR-673")):
        source = tmp_path / f"{number}.nfo"
        source.write_text(
            f"<movie><num>{number}</num><title>新标题</title><plot>新剧情</plot><runtime>0</runtime><rating>0</rating><tag>新标签</tag><cover>https://example.test/new.jpg</cover></movie>",
            encoding="utf-8",
        )
        existing = await repo.upsert_metadata(
            number,
            title="原标题",
            plot="锁定剧情",
            runtime=120,
            tags=["原标签"],
            publisher="保留制作商",
            actors=["保留演员"],
            scores={"javdb": 8.0},
            field_sources={"title": "javdb", "plot": "javdb", "publisher": "javdb", "actors": "javdb"},
        )
        assert existing.id is not None
        await repo.set_metadata_locks(existing.id, [MetadataField.PLOT, MetadataField.THUMB_URLS])
        candidate = provider.read(source)
        result = await handler.handle(
            PluginImportPayload(
                source_id="local.nfo",
                root=str(tmp_path),
                source_path=str(source),
                source_hash=candidate.source_hash,
                overwrite_existing=overwrite,
            )
        )
        assert result.success
        meta = await repo.get_metadata(existing.id)
        assert meta is not None
        assert meta.title == ("新标题" if overwrite else "原标题")
        assert meta.runtime == (0 if overwrite else 120)
        assert meta.tags == (["新标签"] if overwrite else ["原标签"])
        assert meta.scores == ({"local.nfo": 0} if overwrite else {"javdb": 8.0})
        assert meta.plot == "锁定剧情" and meta.thumb_urls == []
        assert meta.publisher == "保留制作商" and meta.actors == ["保留演员"]
        assert meta.field_sources["title"] == ("local.nfo" if overwrite else "javdb")
        assert meta.field_sources["publisher"] == "javdb" and meta.field_sources["plot"] == "javdb"


@pytest.mark.asyncio
async def test_import_preserves_fields_paths_and_source_files(
    nfo_provider: ImportProvider, repo: Repository, resource_store: ResourceStore, tmp_path: Path
) -> None:
    directory = tmp_path / "files"
    directory.mkdir()
    source = directory / "JUR-840-C-破解.nfo"
    source.write_text(
        "<movie><num>JUR-840</num><title>JUR-840 新标题</title><plot>剧情</plot><studio>制作商</studio><trailer>https://example.test/trailer.mp4</trailer></movie>",
        encoding="utf-8",
    )
    image = directory / "JUR-840-C-破解-poster.jpg"
    Image.new("RGB", (20, 30), "red").save(image)
    trailers = directory / "trailers"
    trailers.mkdir()
    trailer = trailers / "trailer.mp4"
    trailer.write_bytes(b"local preview")
    video = directory / "JUR-840-C-破解.mp4"
    video.write_bytes(b"movie")
    lib = await repo.create_library(name="库", path=str(directory))
    existing = await repo.upsert_metadata("JUR-840", title="保留标题", plot=None)
    assert existing.id is not None
    await repo.set_metadata_locks(existing.id, [MetadataField.PLOT])
    loaded = load_plugin("local.nfo", Path(__file__).resolve().parents[2] / "extensions/nfo-import")
    manager = PluginManager({"local.nfo": loaded.plugin}, [])
    handler = PluginImportHandler(repo, resource_store, manager, HotSettings(), [directory])
    candidate = await read_import(nfo_provider, source, directory, [directory])
    payload = PluginImportPayload(
        source_id="local.nfo",
        root=str(directory),
        source_path=str(source),
        source_hash=candidate.source_hash,
        library_id=lib.id,
    )
    for _ in range(2):
        result = await handler.handle(payload)
        assert result.success
    meta = await repo.get_metadata(existing.id)
    assert meta is not None
    assert (meta.title, meta.plot, meta.studio) == ("保留标题", None, "制作商")
    assert meta.trailer_urls == ["https://example.test/trailer.mp4"]
    assert meta.poster_urls[0].startswith("/api/resources/")
    assert len(await resource_store.list_all()) == 1
    files = await repo.get_media_by_metadata_id(existing.id)
    assert len(files) == 1 and files[0].path == str(video.resolve())
    records = await repo.list_import_records(existing.id)
    assert len(records) == 1 and records[0].local_trailer_paths == [str(trailer.resolve())]
    assert trailer.read_bytes() == b"local preview" and video.read_bytes() == b"movie"
    assert source.exists() and image.exists()
    # 媒体关联冲突时, 元数据填空与来源记录更新必须一起回滚.
    original = source.read_bytes()
    other = await repo.upsert_metadata("JUR-841", title="其它影片")
    assert files[0].id is not None and other.id is not None
    await repo.update_media_file(files[0].id, metadata_id=other.id)
    source.write_text("<movie><num>JUR-840</num><publisher>不得写入</publisher></movie>", encoding="utf-8")
    changed = await read_import(nfo_provider, source, directory, [directory])
    with pytest.raises(ValueError, match="已关联"):
        await handler.handle(payload.model_copy(update={"source_hash": changed.source_hash}))
    unchanged = await repo.get_metadata(existing.id)
    assert unchanged is not None and unchanged.publisher is None
    assert (await repo.list_import_records(existing.id))[0].source_hash == candidate.source_hash
    await repo.update_media_file(files[0].id, metadata_id=existing.id)
    source.write_bytes(original)
    await repo.upsert_metadata("JUR-840", title="网络刮削", raw={})
    assert (await repo.list_import_records(existing.id))[0].local_trailer_paths == [str(trailer.resolve())]
    source.write_text("<movie><num>JUR-840</num><title>变化</title></movie>", encoding="utf-8")
    with pytest.raises(ValueError, match="预览后发生变化"):
        await handler.handle(payload)
    with pytest.raises(PermissionError, match="超出"):
        await read_import(nfo_provider, source, trailers, [directory])
    await repo.merge_metadata_entries(other.id, [existing.id])
    assert (await repo.list_import_records(other.id))[0].local_trailer_paths == [str(trailer.resolve())]
    await repo.delete_metadata(other.id)
    assert await repo.list_import_records(other.id) == []
