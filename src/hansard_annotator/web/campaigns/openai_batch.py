"""Small standard-library OpenAI Batch API adapter for frozen Luna production."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from hansard_annotator.llm_eval.providers import provider_prediction_schema

MAX_OPENAI_BATCH_REQUESTS = 50_000
MAX_OPENAI_BATCH_FILE_BYTES = 200 * 1024 * 1024


class OpenAIBatchError(RuntimeError):
    """OpenAI Batch API failure with secrets and response bodies omitted."""


def luna_custom_id(record_id: str) -> str:
    """Map the internal `turn:<sha256>` identity to Batch's 64-char custom ID."""
    prefix = "turn:"
    turn_key = record_id.removeprefix(prefix)
    if not record_id.startswith(prefix) or re.fullmatch(r"[0-9a-f]{64}", turn_key) is None:
        raise ValueError("Luna Batch record IDs must contain a canonical SHA-256 turn key")
    return turn_key


def luna_batch_request(
    *,
    record_id: str,
    user_prompt: str,
    system_prompt: str,
    schema: dict[str, Any],
    generation: dict[str, Any],
) -> dict[str, Any]:
    """Return the exact Responses API request envelope for one frozen Luna record."""
    if generation != {"max_output_tokens": 1000, "reasoning_effort": "medium"}:
        raise ValueError("Luna production generation configuration is frozen")
    return {
        "custom_id": luna_custom_id(record_id),
        "method": "POST",
        "url": "/v1/responses",
        "body": {
            "model": "gpt-6-luna",
            "instructions": system_prompt,
            "input": user_prompt,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "hostility_output_luna_v1_no_reasoning",
                    "strict": True,
                    "schema": provider_prediction_schema("openai", schema),
                }
            },
            "max_output_tokens": 1000,
            "reasoning": {"effort": "medium"},
        },
    }


class OpenAIBatchGateway:
    """Authenticated JSON/multipart calls used only by the campaign background worker."""

    base_url = "https://api.openai.com/v1"
    timeout = 120.0

    @staticmethod
    def _key() -> str:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise OpenAIBatchError("OpenAI credentials are not configured")
        return key

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str = "application/json",
    ) -> bytes:
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self._key()}",
                "Content-Type": content_type,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return cast(bytes, response.read())
        except HTTPError as error:
            raise OpenAIBatchError(
                f"OpenAI Batch API returned HTTP {error.code} for {method} {path}"
            ) from None
        except (TimeoutError, URLError, OSError) as error:
            raise OpenAIBatchError(
                f"OpenAI Batch API transport failed for {method} {path}: {type(error).__name__}"
            ) from None

    def _json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        try:
            value = json.loads(self._call(method, path, body=body))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise OpenAIBatchError("OpenAI returned invalid JSON") from error
        if not isinstance(value, dict):
            raise OpenAIBatchError("OpenAI returned an unexpected response")
        return value

    def upload_requests(self, path: Path) -> str:
        boundary = f"hansard-{uuid.uuid4().hex}"
        filename = "requests.jsonl"
        payload = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nbatch\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/jsonl\r\n\r\n"
        ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        response = self._call(
            "POST",
            "/files",
            body=payload,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        try:
            decoded = json.loads(response)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise OpenAIBatchError("OpenAI returned invalid file metadata") from error
        if not isinstance(decoded, dict) or not isinstance(decoded.get("id"), str):
            raise OpenAIBatchError("OpenAI file response did not include an identifier")
        return str(decoded["id"])

    def submit(self, input_file_id: str, *, campaign_id: str, chunk_number: int) -> dict[str, Any]:
        return self._json(
            "POST",
            "/batches",
            {
                "input_file_id": input_file_id,
                "endpoint": "/v1/responses",
                "completion_window": "24h",
                "metadata": {"campaign_id": campaign_id, "chunk_number": str(chunk_number)},
            },
        )

    def find_batch(self, campaign_id: str, chunk_number: int) -> dict[str, Any] | None:
        after: str | None = None
        matches: list[dict[str, Any]] = []
        while True:
            suffix = "?limit=100" + (f"&after={quote(after, safe='')}" if after else "")
            page = self._json("GET", f"/batches{suffix}")
            data = page.get("data")
            if not isinstance(data, list):
                raise OpenAIBatchError("OpenAI batch listing is malformed")
            for batch in data:
                if not isinstance(batch, dict):
                    continue
                metadata = batch.get("metadata")
                if isinstance(metadata, dict) and metadata.get("campaign_id") == campaign_id and metadata.get("chunk_number") == str(chunk_number):
                    matches.append(batch)
            if not page.get("has_more"):
                break
            if not data or not isinstance(data[-1], dict) or not isinstance(data[-1].get("id"), str):
                raise OpenAIBatchError("OpenAI batch listing cursor is malformed")
            after = str(data[-1]["id"])
        if len(matches) > 1:
            raise OpenAIBatchError("multiple OpenAI batches share the campaign chunk identity")
        return matches[0] if matches else None

    def retrieve(self, batch_id: str) -> dict[str, Any]:
        return self._json("GET", f"/batches/{quote(batch_id, safe='')}")

    def file_content(self, file_id: str) -> bytes:
        return self._call("GET", f"/files/{quote(file_id, safe='')}/content")
