"""一次性演员映射修复; 默认只生成报告, --apply 才写入指定数据库."""

import argparse
import asyncio
import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from amane.db.engine import create_async_engine_from_path
from amane.db.models import Actor, ActorAlias, FacetKind
from amane.db.repository import Repository
from amane.db.repos.facet_helpers import add_actor_aliases


class Mapping(BaseModel):
    line: int
    names: list[str]
    javdb: str


class Change(BaseModel):
    mapping: Mapping
    target_id: int
    target_name: str
    source_ids: list[int]
    source_names: list[str]
    aliases: list[str]


class Skipped(BaseModel):
    actor_ids: list[int]
    names: list[str]
    reason: str


class Plan(BaseModel):
    database: str
    fingerprint: str
    actors_before: int
    changes: list[Change] = Field(default_factory=list)
    skipped: list[Skipped] = Field(default_factory=list)
    unmatched: list[Skipped] = Field(default_factory=list)
    excluded_names: list[str]


def mappings(path: Path) -> tuple[list[Mapping], set[str]]:
    result: list[Mapping] = []
    ambiguous: set[str] = set()
    ET.parse(path)
    for line, text in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not text.lstrip().startswith("<a "):
            continue
        node = ET.fromstring(text.strip())
        names = list(dict.fromkeys(n.strip() for n in [node.get("jp", ""), node.get("zh_cn", ""), node.get("zh_tw", ""), *node.get("keyword", "").split(",")] if n.strip()))
        if any(node.get(k, "").startswith(("重名-", "重複-")) for k in ("jp", "zh_cn", "zh_tw")):
            ambiguous.update(n for n in node.get("keyword", "").split(",") if n)
            continue
        href = node.get("href", "")
        javdb = href.rstrip("/").rsplit("/", 1)[-1] if href.startswith("https://javdb.com/actors/") else ""
        result.append(Mapping(line=line, names=names, javdb=javdb))
    owners: dict[str, set[int]] = defaultdict(set)
    for row in result:
        for name in row.names:
            owners[name].add(row.line)
    ambiguous.update(name for name, lines in owners.items() if len(lines) > 1)
    # 短假名缺少姓氏, 即使字典唯一命中也不足以确认身份.
    ambiguous.update(name for name in owners if len(name) <= 3 and all("\u3040" <= ch <= "\u30ff" for ch in name))
    return result, ambiguous


def fingerprint(path: Path) -> str:
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        payload = [conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() for table in ("actors", "actor_aliases", "metadata", "metadata_actors", "actor_user_tags", "facet_rules")]
    return hashlib.sha256(repr(payload).encode()).hexdigest()


def backup(source: Path, destination: Path) -> None:
    if destination.exists():
        raise ValueError(f"备份路径已存在: {destination}")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)


async def build_plan(database: Path, xml: Path) -> Plan:
    rows, ambiguous = mappings(xml)
    index: dict[str, set[int]] = defaultdict(set)
    for i, row in enumerate(rows):
        for name in row.names:
            if name not in ambiguous:
                index[name].add(i)
    engine = await create_async_engine_from_path(database)
    try:
        async with AsyncSession(engine) as session:
            actors = list((await session.exec(select(Actor).order_by(Actor.id))).all())
            alias_rows = list((await session.exec(select(ActorAlias))).all())
    finally:
        await engine.dispose()
    aliases: dict[int, set[str]] = defaultdict(set)
    name_owners: dict[str, set[int]] = defaultdict(set)
    for actor in actors:
        assert actor.id is not None
        name_owners[actor.name].add(actor.id)
    for alias in alias_rows:
        aliases[alias.actor_id].add(alias.name)
        name_owners[alias.name].add(alias.actor_id)
    plan = Plan(database=str(database), fingerprint=fingerprint(database), actors_before=len(actors), excluded_names=sorted(ambiguous))
    groups: dict[int, list[Actor]] = defaultdict(list)
    for actor in actors:
        assert actor.id is not None
        candidates: set[int] = set()
        for name in [actor.name, *sorted(aliases[actor.id])]:
            candidates.update(index.get(name, set()))
        item = Skipped(actor_ids=[actor.id], names=[actor.name], reason="XML 未唯一命中")
        if len(candidates) == 1:
            groups[next(iter(candidates))].append(actor)
        elif candidates:
            item.reason = "现有姓名或别名命中多个映射"
            plan.skipped.append(item)
        else:
            plan.unmatched.append(item)
    for i, group in sorted(groups.items()):
        row = rows[i]
        ids = {a.id for a in group}
        reasons: list[str] = []
        if any(a.provider_ids.get("javdb") and row.javdb and a.provider_ids["javdb"] != row.javdb for a in group):
            reasons.append("已有 JavDB 身份与 XML 不一致")
        if len({a.gender for a in group if a.gender != "unknown"}) > 1:
            reasons.append("性别不一致")
        if len({a.birthday for a in group if a.birthday}) > 1:
            reasons.append("生日不一致")
        providers = {key for a in group for key in a.provider_ids}
        if any(len({a.provider_ids[key] for a in group if a.provider_ids.get(key)}) > 1 for key in providers):
            reasons.append("同一来源身份不一致")
        safe_names = [n for n in row.names if n not in ambiguous]
        if any(name_owners.get(n, set()) - ids for n in safe_names):
            reasons.append("映射姓名被组外演员使用")
        if any("aliases" in a.locked_fields for a in group):
            reasons.append("别名已锁定")
        if reasons:
            plan.skipped.append(Skipped(actor_ids=[a.id for a in group if a.id is not None], names=[a.name for a in group], reason="; ".join(reasons)))
            continue
        group.sort(key=lambda a: (bool(a.provider_ids), bool(a.raw), bool(a.image_urls), len(a.provider_ids), -(a.id or 0)), reverse=True)
        target = group[0]
        assert target.id is not None
        existing = {target.name, *aliases[target.id]}
        additions = [n for n in safe_names if n not in existing]
        if len(group) > 1 or additions:
            plan.changes.append(Change(mapping=row, target_id=target.id, target_name=target.name, source_ids=[a.id for a in group[1:] if a.id is not None], source_names=[a.name for a in group[1:]], aliases=additions))
    return plan


async def apply_plan(database: Path, plan: Plan) -> None:
    if fingerprint(database) != plan.fingerprint:
        raise ValueError("数据库已变化, 必须重新生成计划")
    with sqlite3.connect(database) as conn:
        active = conn.execute("SELECT count(*) FROM tasks WHERE status IN ('QUEUED','RUNNING')").fetchone()[0]
        if active:
            raise ValueError("存在尚未完成的任务")
    engine = await create_async_engine_from_path(database)
    repo = Repository(engine)
    try:
        for change in plan.changes:
            if change.source_ids:
                await repo.merge_facets(FacetKind.ACTOR, change.target_id, change.source_ids)
            async with AsyncSession(engine) as session:
                actor = await session.get(Actor, change.target_id)
                if actor is None or actor.name != change.target_name:
                    raise ValueError("目标演员与计划不一致")
                await add_actor_aliases(session, actor, change.aliases)
                await session.commit()
    finally:
        await engine.dispose()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--xml", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    database = args.db.resolve(strict=True)
    plan = await build_plan(database, args.xml)
    args.report.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    print(json.dumps({"actors": plan.actors_before, "changes": len(plan.changes), "merges": sum(bool(c.source_ids) for c in plan.changes), "merged_records": sum(len(c.source_ids) for c in plan.changes), "new_aliases": sum(len(c.aliases) for c in plan.changes), "skipped": len(plan.skipped), "unmatched": len(plan.unmatched)}))
    if args.apply:
        backup(database, args.report.with_suffix(".backup.db"))
        await apply_plan(database, plan)
        print("Applied")


if __name__ == "__main__":
    asyncio.run(main())
