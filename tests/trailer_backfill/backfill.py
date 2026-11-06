"""一次性补充缺失的预告片链接; 通过 API 写入, 不启动应用或任务队列."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections import defaultdict
from pathlib import Path
from typing import Literal

import httpx
import structlog
from pydantic import BaseModel, Field

from amane.aggregate import aggregate, compile_priority
from amane.api.models.metadata import MetadataDetailResponse, MetadataListResponse, MetadataResponse
from amane.app.runtime import NetworkStack, build_network_stack, build_r18_db
from amane.config import ColdSettings, HotSettings
from amane.crawlers.models import SearchQuery
from amane.enums import MetadataField
from amane.plugins.manager import PluginManager
from amane.plugins.models import SourceTrait


class Options(BaseModel):
    base_url: str
    data_dir: Path
    apply: bool
    limit: int | None = Field(default=None, gt=0)
    timeout: float = Field(default=120, gt=0)
    concurrency: int = Field(default=4, gt=0, le=8)
    report: Path


class Result(BaseModel):
    id: int
    number: str
    status: Literal["updated", "not_found", "skipped", "error", "prepared"]
    trailer_urls: list[str] = Field(default_factory=list)
    original_locks: list[MetadataField] = Field(default_factory=list)
    detail: str = ""


def record(path: Path, result: Result) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(result.model_dump_json() + "\n")
    logging.info("%s %s %s", result.number, result.status, result.detail)


async def read_metadata(api: httpx.AsyncClient, metadata_id: int) -> MetadataResponse:
    response = await api.get(f"/api/metadata/{metadata_id}")
    response.raise_for_status()
    return MetadataDetailResponse.model_validate(response.json()).metadata


async def write_trailers(api: httpx.AsyncClient, metadata: MetadataResponse, urls: list[str]) -> None:
    # PATCH 属于手动修改, 会自动增加预告片锁; 写入后仅移除本次新增的锁.
    response = await api.patch(f"/api/metadata/{metadata.id}", json={"trailer_urls": urls})
    response.raise_for_status()
    if MetadataField.TRAILER_URLS not in metadata.locked_fields:
        current = await read_metadata(api, metadata.id)
        locks = [field for field in current.locked_fields if field != MetadataField.TRAILER_URLS]
        response = await api.put(f"/api/metadata/{metadata.id}/locks", json={"fields": locks})
        response.raise_for_status()
    current = await read_metadata(api, metadata.id)
    if current.trailer_urls != urls:
        raise RuntimeError("写入后的预告片链接与提交内容不一致")


async def process(
    api: httpx.AsyncClient,
    stack: NetworkStack,
    hot: HotSettings,
    plugins: PluginManager,
    metadata: MetadataResponse,
    options: Options,
) -> Result:
    route = hot.scraping.content_routes.get(metadata.content_type, [])
    compiled = compile_priority(route, hot.scraping.field_priority, hot.scraping.field_blacklist)
    priority: defaultdict[MetadataField, list[str]] = defaultdict(list)
    priority[MetadataField.TRAILER_URLS] = compiled[MetadataField.TRAILER_URLS]
    crawlers = await stack.factory.get_crawlers(priority[MetadataField.TRAILER_URLS])
    descriptors = plugins.descriptors()
    result = await aggregate(
        SearchQuery(metadata.number, content_type=metadata.content_type),
        crawlers,
        priority,
        multi_lang_sites=frozenset(d.id for d in descriptors if SourceTrait.MULTI_LANGUAGE in d.traits),
        deferred_sites=frozenset(d.id for d in descriptors if SourceTrait.NEEDS_PARTIAL in d.traits),
    )
    urls = list(dict.fromkeys(url for url in result.metadata.trailer_urls if url.strip()))
    entry = Result(id=metadata.id, number=metadata.number, status="not_found", trailer_urls=urls)
    if not urls:
        entry.detail = f"已查询来源: {result.sites_queried}; 失败来源: {result.failed_sites}"
        return entry
    # 获取期间影片可能已被其它操作补充或锁定, 写入前再次检查.
    current = await read_metadata(api, metadata.id)
    if current.trailer_urls or MetadataField.TRAILER_URLS in current.locked_fields:
        entry.status = "skipped"
        entry.detail = "影片已有预告片链接或预告片字段已锁定"
        return entry
    entry.original_locks = current.locked_fields
    entry.status = "prepared"
    # 写入前记录恢复依据; 中断发生在 PATCH 与恢复锁之间时可据此核查.
    await asyncio.to_thread(record, options.report, entry)
    await write_trailers(api, current, urls)
    entry.status = "updated"
    return entry


async def run(options: Options, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(base_url=options.base_url, headers=headers, timeout=30, trust_env=False) as api:
        response = await api.get("/api/config")
        response.raise_for_status()
        hot = HotSettings.model_validate(response.json())
        candidates: list[MetadataResponse] = []
        offset = 0
        while True:
            response = await api.get(
                "/api/metadata", params={"offset": offset, "limit": 200, "sort_by": "number", "order": "asc"}
            )
            response.raise_for_status()
            page = MetadataListResponse.model_validate(response.json())
            candidates.extend(
                item
                for item in page.items
                if not item.trailer_urls and MetadataField.TRAILER_URLS not in item.locked_fields
            )
            offset += len(page.items)
            if not page.items or offset >= page.total:
                break
        candidates = candidates[: options.limit]
        logging.info("缺少链接且未锁定预告片字段的影片: %d; 写入: %s", len(candidates), options.apply)
        if not options.apply or not candidates:
            return
        options.report.parent.mkdir(parents=True, exist_ok=True)
        plugins = PluginManager.discover(options.data_dir)
        r18 = build_r18_db(hot.r18)
        stack = build_network_stack(hot, r18_db=r18, data_dir=options.data_dir, plugin_manager=plugins)
        try:
            counts: defaultdict[str, int] = defaultdict(int)
            semaphore = asyncio.Semaphore(options.concurrency)

            async def process_one(metadata: MetadataResponse) -> None:
                async with semaphore:
                    try:
                        async with asyncio.timeout(options.timeout):
                            entry = await process(api, stack, hot, plugins, metadata, options)
                    except (httpx.HTTPError, TimeoutError, RuntimeError, ValueError) as exc:
                        entry = Result(
                            id=metadata.id,
                            number=metadata.number,
                            status="error",
                            detail=f"{type(exc).__name__}: {exc}",
                        )
                    await asyncio.to_thread(record, options.report, entry)
                    counts[entry.status] += 1
                    logging.info("进度 %d/%d", sum(counts.values()), len(candidates))

            async with asyncio.TaskGroup() as group:
                for metadata in candidates:
                    group.create_task(process_one(metadata))
            logging.info("完成: %s", dict(counts))
        finally:
            await stack.browser.close()
            await stack.web_client.close()
            if r18 is not None:
                await r18.close()


def main() -> None:
    cold = ColdSettings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="运行中的 Amane 服务地址")
    parser.add_argument("--data-dir", type=Path, default=cold.data_dir, help="服务的数据目录, 用于 token 和来源插件")
    parser.add_argument("--apply", action="store_true", help="获取并写入链接; 省略时仅统计目标")
    parser.add_argument("--limit", type=int, help="本次处理的影片数量上限, 必须大于零")
    parser.add_argument("--timeout", type=float, default=120, help="每部影片的处理超时秒数")
    parser.add_argument("--concurrency", type=int, default=4, help="并发影片数量, 范围为 1 至 8; 复用站点限速")
    parser.add_argument("--report", type=Path, default=Path(".tmp/trailer-backfill.jsonl"), help="逐部处理记录")
    options = Options.model_validate(vars(parser.parse_args()))
    token_path = options.data_dir / "token"
    token = cold.token if cold.token is not None else token_path.read_text(encoding="utf-8").strip()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    asyncio.run(run(options, "" if token == "off" else token))


if __name__ == "__main__":
    main()
