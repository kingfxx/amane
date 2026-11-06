import json

import httpx
import pytest
from pydantic import ValidationError

from amane.api.models.metadata import MetadataResponse
from amane.enums import MetadataField
from amane.parsing import ContentType
from tests.trailer_backfill.backfill import Options, write_trailers


@pytest.mark.asyncio
@pytest.mark.parametrize("already_locked,fail_status", [(False, None), (True, None), (False, 422), (False, 500)])
async def test_write_scope_and_errors(already_locked: bool, fail_status: int | None) -> None:
    original = [MetadataField.TITLE]
    if already_locked:
        original.append(MetadataField.TRAILER_URLS)
    metadata = MetadataResponse(id=1, number="TEST-001", content_type=ContentType.CENSORED, locked_fields=original)
    stored = metadata.model_copy(deep=True)
    urls = ["https://example.com/trailer.mp4"]
    writes: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            writes.append("patch")
            assert json.loads(request.content) == {"trailer_urls": urls}
            if fail_status is not None:
                return httpx.Response(fail_status, json={"detail": "写入失败"})
            stored.trailer_urls = urls
            if MetadataField.TRAILER_URLS not in stored.locked_fields:
                stored.locked_fields.append(MetadataField.TRAILER_URLS)
            return httpx.Response(200, json=stored.model_dump(mode="json"))
        if request.method == "PUT":
            writes.append("locks")
            assert json.loads(request.content) == {"fields": original}
            stored.locked_fields = list(original)
            return httpx.Response(200, json=stored.model_dump(mode="json"))
        assert request.method == "GET"
        return httpx.Response(200, json={"metadata": stored.model_dump(mode="json"), "files": []})

    async with httpx.AsyncClient(base_url="http://amane.test", transport=httpx.MockTransport(respond)) as api:
        if fail_status is not None:
            with pytest.raises(httpx.HTTPStatusError, match=str(fail_status)):
                await write_trailers(api, metadata, urls)
            assert writes == ["patch"]
            assert stored.trailer_urls == []
        else:
            await write_trailers(api, metadata, urls)
            assert writes == (["patch"] if already_locked else ["patch", "locks"])
            assert stored.locked_fields == original
            assert stored.trailer_urls == urls


@pytest.mark.parametrize("field,value", [("limit", 0), ("limit", -1), ("timeout", 0), ("timeout", -1)])
def test_invalid_options(field: str, value: int) -> None:
    with pytest.raises(ValidationError, match=field):
        Options.model_validate(
            {
                "base_url": "http://127.0.0.1:8000",
                "data_dir": "data",
                "apply": False,
                "report": "report.jsonl",
                field: value,
            }
        )
