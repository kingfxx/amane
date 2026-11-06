"""电影 NFO 与同目录附属文件导入, 不访问网络或修改源目录."""

import codecs
import hashlib
import math
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Literal, override
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from xml.parsers import expat

from pydantic import BaseModel, ValidationError

from amane.plugin import (
    FilmActor,
    ImportCandidate,
    ImportContext,
    ImportImage,
    ImportMedia,
    ImportPlugin,
    ImportProgress,
    ImportProvider,
    MediaMetadata,
    PreparedImport,
    SnapshotImportProvider,
    SourceCapability,
    SourceDescriptor,
    extract_number,
)


def _forbid_doctype(*args: object) -> None:
    raise ValueError("NFO 不允许包含 DTD 或外部实体")


def _parse_xml(data: bytes) -> tuple[ET.Element, list[str]]:
    def parse(content: bytes) -> ET.Element:
        parser = expat.ParserCreate()
        parser.StartDoctypeDeclHandler = _forbid_doctype
        parser.Parse(content, True)
        return ET.fromstring(content)

    try:
        return parse(data), []
    except ET.ParseError, expat.ExpatError:
        # 仅修复裸 & 与数字开头的自定义标签名; CDATA、注释和源文件保持不变.
        encoding = "utf-8-sig"
        declaration = re.search(rb"encoding=[\"\']([^\"\']+)", data[:256], re.IGNORECASE)
        if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            encoding = "utf-16"
        elif declaration:
            encoding = declaration[1].decode("ascii")
        try:
            text = data.decode(encoding)
            parts: list[str] = []
            repaired_ampersands = False
            repaired_tags = False
            for part in re.split(r"(<!\[CDATA\[.*?\]\]>|<!--.*?-->)", text, flags=re.DOTALL):
                if part.startswith(("<![CDATA[", "<!--")):
                    parts.append(part)
                    continue
                escaped = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#[0-9]+;|#x[0-9A-Fa-f]+;)", "&amp;", part)
                normalized = re.sub(r"(</?)([0-9][A-Za-z0-9_.-]*)(?=[\s/>])", r"\1_\2", escaped)
                repaired_ampersands |= escaped != part
                repaired_tags |= normalized != escaped
                parts.append(normalized)
            repaired = "".join(parts)
            if repaired == text:
                raise ValueError("NFO XML 格式无效")
            root = parse(repaired.encode(encoding))
            warnings: list[str] = []
            if repaired_ampersands:
                warnings.append("NFO 存在未转义的 & 字符, 已在内存中修复, 源文件保持不变")
            if repaired_tags:
                warnings.append("NFO 自定义标签名以数字开头, 已兼容读取, 源文件保持不变")
            return root, warnings
        except (UnicodeError, LookupError, ET.ParseError, expat.ExpatError) as exc:
            raise ValueError("NFO XML 格式无效") from exc


def _http(value: str) -> str | None:
    parsed = urlsplit(value)
    return value if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username else None


def _text(root: ET.Element, *names: str) -> str | None:
    for name in names:
        value = root.findtext(name)
        if value and value.strip():
            return value.strip()
    return None


def _texts(root: ET.Element, *names: str) -> list[str]:
    return list(
        dict.fromkeys(
            node.text.strip() for name in names for node in root.findall(name) if node.text and node.text.strip()
        )
    )


def _numeric(value: str | None, field: str, maximum: float) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"NFO {field} 必须是数值") from exc
    if not math.isfinite(result) or result < 0 or result > maximum:
        raise ValueError(f"NFO {field} 超出有效范围")
    return result


def _local(path: Path, parent: Path) -> Path | None:
    resolved = path.resolve()
    if not resolved.is_relative_to(parent):
        raise ValueError("NFO 附属文件路径超出所属目录")
    return resolved if resolved.is_file() else None


class NfoSnapshot(BaseModel):
    version: Literal[1] = 1
    prepared: PreparedImport
    source_file: str


def _cache_file(context: ImportContext, name: str) -> Path:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError("NFO 缓存文件名无效")
    path = (context.cache_dir / name).resolve()
    if not path.is_relative_to(context.cache_dir.resolve()) or not path.is_file():
        raise ValueError("NFO 缓存文件缺失或超出缓存目录, 请重新预览")
    return path


class NfoProvider(SnapshotImportProvider):
    @override
    def prepare(self, context: ImportContext, report: Callable[[ImportProgress], None]) -> PreparedImport:
        report(ImportProgress(phase="read", processed=0, total=1))
        candidate = self.read(context.source)
        data = candidate.source_bytes
        if data is None or hashlib.sha256(data).hexdigest() != candidate.source_hash:
            raise ValueError("NFO 来源内容与解析结果不匹配")
        candidate.source_bytes = None
        (context.cache_dir / "source.nfo").write_bytes(data)
        report(ImportProgress(phase="read", processed=1, total=1))
        copied: dict[str, str] = {}
        images: list[ImportImage] = []
        report(ImportProgress(phase="cache", processed=0, total=len(candidate.images)))
        for index, image in enumerate(candidate.images):
            name = copied.get(image.path)
            if name is None:
                local = _local(Path(image.path), context.root)
                if local is None:
                    raise ValueError("NFO 图片不存在")
                name = f"image-{len(copied)}{local.suffix}"
                with local.open("rb") as stream:
                    content = stream.read(32 * 1024 * 1024 + 1)
                if len(content) > 32 * 1024 * 1024:
                    raise ValueError("导入图片超过 32 MiB")
                (context.cache_dir / name).write_bytes(content)
                copied[image.path] = name
            images.append(ImportImage(kind=image.kind, path=name))
            report(ImportProgress(phase="cache", processed=index + 1, total=len(candidate.images)))
        extensions: dict[Path, list[str]] = {}
        media: list[ImportMedia] = []
        for value in candidate.media_paths:
            path = _local(Path(value), context.root)
            if path is None:
                raise ValueError("NFO 关联正片不存在")
            if path.parent not in extensions:
                extensions[path.parent] = sorted(
                    {item.suffix.lower() for item in path.parent.iterdir() if item.is_file()}
                )
            media.append(
                ImportMedia(
                    path=unicodedata.normalize("NFC", str(path)),
                    size=path.stat().st_size,
                    subtitle_extensions=extensions[path.parent],
                )
            )
        prepared = PreparedImport(candidate=candidate, images=images, media=media)
        snapshot = NfoSnapshot(prepared=prepared, source_file="source.nfo")
        (context.cache_dir / "nfo-snapshot.json").write_text(snapshot.model_dump_json(), encoding="utf-8")
        return self.restore(context)

    @override
    def restore(self, context: ImportContext) -> PreparedImport:
        try:
            snapshot = NfoSnapshot.model_validate_json(
                _cache_file(context, "nfo-snapshot.json").read_text(encoding="utf-8")
            )
        except ValidationError as exc:
            raise ValueError("NFO 快照版本或格式不兼容, 请重新预览") from exc
        source = _cache_file(context, snapshot.source_file)
        if hashlib.sha256(source.read_bytes()).hexdigest() != snapshot.prepared.candidate.source_hash:
            raise ValueError("NFO 缓存来源内容无效, 请重新预览")
        for image in snapshot.prepared.images:
            image.path = str(_cache_file(context, image.path))
        return snapshot.prepared

    @override
    def discover(self, root: Path, *, recursive: bool) -> tuple[Path, ...]:
        result: list[Path] = []
        for path in root.glob("**/*" if recursive else "*"):
            if ".amane_trash" in path.parts or path.suffix.casefold() != ".nfo" or not path.is_file():
                continue
            result.append(path)
        return tuple(sorted(result))

    @override
    def read(self, path: Path) -> ImportCandidate:
        with path.open("rb") as stream:
            data = stream.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("NFO 超过 2 MiB")
        root, warnings = _parse_xml(data)
        if root.tag != "movie":
            raise ValueError("只支持 movie 类型的 NFO")
        explicit = _text(root, "num", "number")
        number = extract_number(explicit) if explicit else None
        if explicit and (number is None or number.casefold() != explicit.casefold()):
            raise ValueError("NFO 番号标识无效")
        title = _text(root, "title", "originaltitle")
        number = number or extract_number(path.stem) or extract_number(title or "")
        if not number:
            raise ValueError("NFO 缺少可识别的番号")
        from_name = extract_number(path.stem)
        if from_name and from_name.casefold() != number.casefold():
            raise ValueError("NFO 番号与文件名冲突")
        if title:
            title = re.sub(rf"^{re.escape(number)}(?:\s+|\s*[-_:：]\s*)", "", title, flags=re.IGNORECASE).strip()
        runtime = _numeric(_text(root, "runtime"), "runtime", 100000)
        if runtime is not None and not runtime.is_integer():
            raise ValueError("NFO runtime 必须是整数分钟")
        metadata = MediaMetadata(
            number=number,
            title=title,
            actors=[FilmActor(name=name) for name in _texts(root, "actor/name")],
            directors=_texts(root, "director"),
            tags=_texts(root, "tag", "genre"),
            studio=_text(root, "studio", "maker"),
            publisher=_text(root, "publisher", "label"),
            series=_text(root, "series", "set/name"),
            release=_text(root, "premiered", "releasedate", "release"),
            plot=_text(root, "plot", "outline"),
            runtime=int(runtime) if runtime is not None else None,
            score=_numeric(_text(root, "rating"), "rating", 10),
            poster_urls=[url for value in _texts(root, "poster") if (url := _http(value))],
            thumb_urls=[url for value in _texts(root, "cover") if (url := _http(value))],
            trailer_urls=[url for value in _texts(root, "trailer") if (url := _http(value))],
        )
        parent = path.parent.resolve()
        siblings = list(parent.iterdir())
        single = sum(item.suffix.casefold() == ".nfo" for item in siblings) == 1
        images: list[ImportImage] = []
        media: list[str] = []
        trailers: list[str] = []
        for item in siblings:
            suffix = item.suffix.casefold()
            stem = item.stem.casefold()
            item_number = extract_number(item.stem)
            related = (
                item_number == number
                if item_number
                else (
                    stem == path.stem.casefold()
                    or stem.startswith((f"{path.stem.casefold()}-", f"{path.stem.casefold()}_"))
                )
            )
            generic = single and stem in {"poster", "thumb", "fanart", "trailer"}
            if suffix in {".jpg", ".jpeg", ".png", ".webp"} and (related or generic):
                local = _local(item, parent)
                if local:
                    kind = "poster" if stem.endswith("poster") else "thumb"
                    images.append(ImportImage(kind=kind, path=str(local)))
            elif suffix in {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".ts", ".webm"} and (related or generic):
                local = _local(item, parent)
                if local:
                    (trailers if re.search(r"trailer|preview|sample|预告|预览", stem) else media).append(str(local))
        # 通用子目录只在目录所属影片唯一时关联.
        if single:
            for directory, kind in (("extrafanart", "still"), ("trailers", "trailer")):
                folder = parent / directory
                if not folder.exists():
                    continue
                if not folder.resolve().is_relative_to(parent):
                    raise ValueError("NFO 附属目录路径超出所属目录")
                for item in sorted(folder.iterdir()):
                    if kind == "still" and item.suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"}:
                        local = _local(item, parent)
                        if local:
                            images.append(ImportImage(kind="still", path=str(local)))
                    elif kind == "trailer" and item.suffix.casefold() in {".mp4", ".mkv", ".webm", ".mov"}:
                        local = _local(item, parent)
                        if local:
                            trailers.append(str(local))
        image_refs: tuple[tuple[str, Literal["poster", "thumb", "still"]], ...] = (
            ("poster", "poster"),
            ("cover", "thumb"),
            ("fanart/thumb", "still"),
        )
        for tag, kind in image_refs:
            for value in _texts(root, tag):
                remote = _http(value)
                if remote:
                    urls = {
                        "poster": metadata.poster_urls,
                        "thumb": metadata.thumb_urls,
                        "still": metadata.extrafanart,
                    }[kind]
                    if remote not in urls:
                        urls.append(remote)
                    continue
                local = _local(parent / value, parent)
                if local:
                    images.append(ImportImage(kind=kind, path=str(local)))
        for node in root.findall("thumb"):
            value = (node.text or "").strip()
            if not value:
                continue
            kind = "poster" if node.attrib.get("aspect") == "poster" else "thumb"
            remote = _http(value)
            if remote:
                urls = metadata.poster_urls if kind == "poster" else metadata.thumb_urls
                if remote not in urls:
                    urls.append(remote)
            else:
                local = _local(parent / value, parent)
                if local:
                    images.append(ImportImage(kind=kind, path=str(local)))
        for value in _texts(root, "trailer"):
            if not _http(value):
                local = _local(parent / value, parent)
                if local:
                    trailers.append(str(local))
        # thumb 优先于 fanart, 避免备用横版封面成为默认图.
        images.sort(key=lambda image: (Path(image.path).stem.casefold().endswith("fanart"), image.path))
        return ImportCandidate(
            source_path=str(path.resolve()),
            source_hash=hashlib.sha256(data).hexdigest(),
            source_bytes=data,
            metadata=metadata,
            images=list({(image.kind, image.path): image for image in images}.values()),
            media_paths=list(dict.fromkeys(media)),
            local_trailer_paths=list(dict.fromkeys(trailers)),
            warnings=warnings,
        )


class Plugin(ImportPlugin):
    import_api_version: ClassVar[int] = 2

    @classmethod
    @override
    def descriptor(cls) -> SourceDescriptor:
        return SourceDescriptor(
            id="local.nfo",
            name="本地 NFO 导入",
            version="0.2.0",
            capabilities=frozenset({SourceCapability.METADATA_IMPORT}),
        )

    @override
    def build_importer(self, config: BaseModel) -> ImportProvider:
        return NfoProvider()
