"""校验一次性修复的数据库副本与执行结果."""

import argparse
import json
import sqlite3
from pathlib import Path

from repair_actors import Plan


def verify(before: Path, after: Path, plan_path: Path) -> dict[str, int]:
    plan = Plan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    old = sqlite3.connect(before.resolve().as_uri() + "?mode=ro", uri=True)
    new = sqlite3.connect(after.resolve().as_uri() + "?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    new.row_factory = sqlite3.Row
    remap = {source: c.target_id for c in plan.changes for source in c.source_ids}
    rename = {source: c.target_name for c in plan.changes for source in c.source_names}
    merged_targets = {c.target_id for c in plan.changes if c.source_ids}
    assert tuple(new.execute("PRAGMA integrity_check").fetchone()) == ("ok",)
    assert [tuple(r) for r in new.execute("PRAGMA foreign_key_check")] == [tuple(r) for r in old.execute("PRAGMA foreign_key_check")]
    changed_tables = {"actors", "actor_aliases", "metadata", "metadata_actors", "actor_user_tags"}
    tables = [r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        if table not in changed_tables:
            assert sorted((tuple(r) for r in old.execute(f'SELECT * FROM "{table}"')), key=repr) == sorted((tuple(r) for r in new.execute(f'SELECT * FROM "{table}"')), key=repr), table
    old_actors = {r["id"]: dict(r) for r in old.execute("SELECT * FROM actors")}
    new_actors = {r["id"]: dict(r) for r in new.execute("SELECT * FROM actors")}
    assert set(new_actors) == set(old_actors) - set(remap)
    for actor_id, actor in new_actors.items():
        original = old_actors[actor_id]
        if actor_id not in merged_targets:
            assert actor == original, actor_id
            continue
        for field, value in original.items():
            if field in ("updated_at", "field_sources", "raw", "provider_ids", "source_urls", "image_urls", "locked_fields"):
                continue
            if value not in (None, "", "UNKNOWN"):
                assert actor[field] == value, (actor_id, field)
        for field in ("raw", "provider_ids", "source_urls", "field_sources"):
            previous = json.loads(original[field])
            current = json.loads(actor[field])
            assert all(current.get(k) == v for k, v in previous.items()), (actor_id, field)
        for field in ("image_urls", "locked_fields"):
            assert set(json.loads(original[field])) <= set(json.loads(actor[field])), (actor_id, field)
    old_aliases = {(r["actor_id"], r["name"]) for r in old.execute("SELECT * FROM actor_aliases")}
    new_aliases = {(r["actor_id"], r["name"]) for r in new.execute("SELECT * FROM actor_aliases")}
    expected = {(remap.get(actor_id, actor_id), name) for actor_id, name in old_aliases if name != new_actors[remap.get(actor_id, actor_id)]["name"]}
    for change in plan.changes:
        expected.update((change.target_id, name) for name in [*change.source_names, *change.aliases] if name != change.target_name)
    assert new_aliases == expected
    old_metadata = {r["id"]: dict(r) for r in old.execute("SELECT * FROM metadata")}
    new_metadata = {r["id"]: dict(r) for r in new.execute("SELECT * FROM metadata")}
    assert old_metadata.keys() == new_metadata.keys()
    affected = 0
    for mid, row in old_metadata.items():
        expected_names = list(dict.fromkeys(rename.get(name, name) for name in json.loads(row["actors"])))
        current = new_metadata[mid]
        assert json.loads(current["actors"]) == expected_names, mid
        if current["actors"] != row["actors"]:
            affected += 1
        assert {k: v for k, v in current.items() if k not in ("actors", "updated_at")} == {k: v for k, v in row.items() if k not in ("actors", "updated_at")}, mid
    expected_links: set[tuple[int, int, int]] = set()
    names = {a["name"]: actor_id for actor_id, a in new_actors.items()}
    for mid, row in new_metadata.items():
        for position, name in enumerate(json.loads(row["actors"])):
            expected_links.add((mid, names[name], position))
    assert {tuple(r) for r in new.execute("SELECT metadata_id,actor_id,position FROM metadata_actors")} == expected_links
    expected_tags = {(remap.get(r[0], r[0]), r[1]) for r in old.execute("SELECT actor_id,user_tag_id FROM actor_user_tags")}
    assert {tuple(r) for r in new.execute("SELECT actor_id,user_tag_id FROM actor_user_tags")} == expected_tags
    summary = {"actors_before": len(old_actors), "actors_after": len(new_actors), "aliases_before": len(old_aliases), "aliases_after": len(new_aliases), "metadata": len(new_metadata), "affected_metadata": affected, "unchanged_other_tables": len(tables) - len(changed_tables)}
    old.close()
    new.close()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.before, args.after, args.plan)))


if __name__ == "__main__":
    main()
