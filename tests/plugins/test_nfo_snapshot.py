from pathlib import Path

import pytest
from PIL import Image

from amane.plugin import ImportContext, ImportProgress, SnapshotImportProvider


def test_plugin_snapshot_restore_and_invalid_cache(nfo_provider: SnapshotImportProvider, tmp_path: Path) -> None:
    cloud = tmp_path / "cloud"
    cloud.mkdir()
    source = cloud / "ABC-123-UC.nfo"
    source.write_text("<movie><num>ABC-123</num><title>预览标题</title></movie>", encoding="utf-8")
    video = cloud / "ABC-123-UC.mp4"
    video.write_bytes(b"video")
    Image.new("RGB", (20, 30), "red").save(cloud / "poster.jpg")
    cache = tmp_path / "cache"
    cache.mkdir()
    context = ImportContext(root=cloud, source=source, cache_dir=cache)
    updates: list[ImportProgress] = []
    assert nfo_provider.discover_with_progress(cloud, recursive=False, report=updates.append) == (source,)
    assert updates[-1].phase == "discover" and updates[-1].processed == updates[-1].total == 1
    prepared = nfo_provider.prepare(context, updates.append)
    assert prepared.candidate.metadata.title == "预览标题"
    assert prepared.media[0].path == str(video) and prepared.media[0].size == 5
    assert updates[-1].phase == "cache" and updates[-1].processed == updates[-1].total == 1
    cloud.rename(tmp_path / "offline")
    restored = nfo_provider.restore(context)
    assert restored.candidate.source_path == str(source) and Path(restored.images[0].path).is_file()
    manifest = cache / "nfo-snapshot.json"
    original = manifest.read_text(encoding="utf-8")
    for changed, message in (
        (original.replace('"version":1', '"version":999'), "版本或格式不兼容"),
        (original.replace("image-0.jpg", "../outside.jpg"), "文件名无效"),
        (original.replace("image-0.jpg", "missing.jpg"), "缺失或超出"),
    ):
        manifest.write_text(changed, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            nfo_provider.restore(context)
