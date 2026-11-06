"""为性别未知或缺少资料的演员一次性批量提交联网刮削任务."""

import json
import sqlite3
import urllib.request
from pathlib import Path

from pydantic import BaseModel

from amane.api.models.tasks import ActorScrapeSubmission, TaskBatchSubmitRequest, TaskBatchSubmitResponse


class SelectedActor(BaseModel):
    actor_id: int
    name: str
    unknown_gender: bool
    missing_profile: bool


class Selection(BaseModel):
    actors: list[SelectedActor]


def select_actors(database: Path) -> Selection:
    result = Selection(actors=[])
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT * FROM actors ORDER BY id"):
            unknown = row["gender"] == "UNKNOWN"
            missing = not json.loads(row["raw"] or "{}") or (
                not json.loads(row["image_urls"] or "[]")
                and not any(row[field] for field in ("birthday", "birthplace", "height", "bust", "waist", "hip", "cup", "overview", "tagline"))
            )
            if unknown or missing:
                result.actors.append(SelectedActor(actor_id=row["id"], name=row["name"], unknown_gender=unknown, missing_profile=missing))
    return result


def main() -> None:
    directory = Path(__file__).resolve().parent
    receipt = directory / "actor-scrape-batch.json"
    if receipt.exists():
        raise ValueError("提交回执已存在, 禁止重复提交")
    selection = select_actors(directory.parent / "amane.db")
    if not selection.actors:
        print("No matching actors")
        return
    (directory / "actor-scrape-selection.json").write_text(selection.model_dump_json(indent=2), encoding="utf-8")
    batch = TaskBatchSubmitRequest(submissions=[ActorScrapeSubmission(type="actor_scrape", actor_id=actor.actor_id, use_cache=set()) for actor in selection.actors])
    request = urllib.request.Request(
        "http://127.0.0.1:8000/api/tasks/batch/submit",
        data=batch.model_dump_json().encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        result = TaskBatchSubmitResponse.model_validate_json(response.read())
    receipt.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    print(result.model_dump_json())
    assert result.submitted == len(selection.actors) and result.missing == 0 and result.skipped == 0


if __name__ == "__main__":
    main()
