"""暂停本机任务处理, 执行演员修复与校验, 完成后恢复原暂停状态."""

import asyncio
import json
import urllib.request
from pathlib import Path

from pydantic import BaseModel

from repair_actors import apply_plan, backup, build_plan, fingerprint
from verify_repair import verify


class WorkerState(BaseModel):
    paused: bool


def worker(action: str = "") -> WorkerState:
    request = urllib.request.Request(
        "http://127.0.0.1:8000/api/tasks/worker" + action,
        method="POST" if action else "GET",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return WorkerState.model_validate_json(response.read())


async def main() -> None:
    directory = Path(__file__).resolve().parent
    database = directory.parent / "amane.db"
    state_path = directory / "worker-before.json"
    original = WorkerState.model_validate_json(state_path.read_text(encoding="utf-8")) if state_path.exists() else worker()
    state_path.write_text(original.model_dump_json(), encoding="utf-8")
    worker("/pause")
    print("Worker paused", flush=True)
    backup_path = directory / "before-repair.backup.db"
    backup(database, backup_path)
    trial = directory / "latest-rehearsal.db"
    backup(backup_path, trial)
    trial_plan = await build_plan(trial, Path("D:/Program Files/mdcx/userdata/mapping_actor.xml"))
    trial_report = directory / "latest-rehearsal-plan.json"
    trial_report.write_text(trial_plan.model_dump_json(indent=2), encoding="utf-8")
    await apply_plan(trial, trial_plan)
    print(json.dumps(verify(backup_path, trial, trial_report)), flush=True)
    if fingerprint(database) != fingerprint(backup_path):
        raise ValueError("实际数据库在演练期间变化, 停止执行")
    plan = await build_plan(database, Path("D:/Program Files/mdcx/userdata/mapping_actor.xml"))
    report = directory / "applied-plan.json"
    report.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    if plan.model_dump(exclude={"database", "fingerprint"}) != trial_plan.model_dump(exclude={"database", "fingerprint"}):
        raise ValueError("实际计划与演练不一致")
    await apply_plan(database, plan)
    summary = verify(backup_path, database, report)
    remaining = await build_plan(database, Path("D:/Program Files/mdcx/userdata/mapping_actor.xml"))
    assert not remaining.changes, "修复后仍存在可执行项"
    (directory / "remaining.json").write_text(remaining.model_dump_json(indent=2), encoding="utf-8")
    (directory / "verification.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if not original.paused:
        worker("/resume")
    assert worker().paused == original.paused
    print(json.dumps(summary), flush=True)
    print("Worker original state restored", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
