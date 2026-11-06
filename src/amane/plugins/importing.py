"""导入候选的路径边界与重新读取; 主机不依赖具体文件格式."""

from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from ..config import HotSettings, PluginConfig
from ..utils.path import resolved_path
from ..utils.threads import in_thread
from .api import ImportProvider
from .import_models import ImportCandidate
from .manager import PluginManager
from .models import SourceCapability


def import_provider(manager: PluginManager, hot: HotSettings, source_id: str) -> ImportProvider:
    descriptor = manager.descriptor(source_id)
    config = hot.plugins.get(source_id, PluginConfig())
    if descriptor is None or not descriptor.supports(SourceCapability.METADATA_IMPORT) or not config.enabled:
        raise ValueError("导入插件未安装、已停用或不提供导入能力")
    return manager.build_import_provider(source_id, config)


def _checked(path: Path, root: Path, safe_dirs: Sequence[Path] | None) -> Path:
    resolved = resolved_path(path)
    if not resolved.is_relative_to(resolved_path(root)):
        raise PermissionError("导入文件超出所选目录")
    if safe_dirs is not None and not any(resolved.is_relative_to(resolved_path(directory)) for directory in safe_dirs):
        raise PermissionError("导入文件不在允许的安全目录内")
    # 虚拟卷可能只拒绝严格规范化; 存在性仍须独立验证, 不能随路径回退而省略.
    resolved.stat()
    return resolved


@in_thread
def discover_imports(
    provider: ImportProvider, root: Path, recursive: bool, safe_dirs: Sequence[Path] | None
) -> tuple[Path, ...]:
    root = _checked(root, root, safe_dirs)
    if not root.is_dir():
        raise ValueError("导入路径必须是目录")
    paths = provider.discover(root, recursive=recursive)
    return tuple(dict.fromkeys(_checked(path, root, safe_dirs) for path in paths))


@in_thread
def read_import(
    provider: ImportProvider, source: Path, root: Path, safe_dirs: Sequence[Path] | None
) -> ImportCandidate:
    source = _checked(source, root, safe_dirs)
    candidate = provider.read(source)
    if _checked(Path(candidate.source_path), root, safe_dirs) != source:
        raise ValueError("导入插件返回了其它来源文件")
    if not candidate.metadata.number.strip():
        raise ValueError("导入候选缺少番号")
    for image in candidate.images:
        image.path = str(_checked(Path(image.path), root, safe_dirs))
    candidate.media_paths = [str(_checked(Path(path), root, safe_dirs)) for path in candidate.media_paths]
    candidate.local_trailer_paths = [
        str(_checked(Path(path), root, safe_dirs)) for path in candidate.local_trailer_paths
    ]
    for url in (
        *candidate.metadata.poster_urls,
        *candidate.metadata.thumb_urls,
        *candidate.metadata.trailer_urls,
        *candidate.metadata.extrafanart,
    ):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("导入远程资源只接受不含凭据的 http/https 地址")
    candidate.source_path = str(source)
    return candidate
