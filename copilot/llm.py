"""The only place that talks to the model.

The pipeline depends on the `LLM` protocol, so tests run with a fake and
without a key.
"""

import json
import os
import time
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from copilot.schemas import Usage

T = TypeVar("T", bound=BaseModel)
KIMI_URL = "https://api.kimi.com/coding/v1"


class LLMError(Exception):
    """The model gave no usable answer. The pipeline routes such tickets to a human."""


class LLM(Protocol):
    def parse(self, *, model: str, system: list[str], user: str, schema: type[T]) -> tuple[T, Usage]: ...


class ChatLLM:
    """Kimi K3 over the OpenAI chat-completions protocol.

    The endpoint does not enforce a response schema, so the schema goes into
    the prompt, Pydantic validates the answer, and invalid JSON gets one retry.
    LLM_BASE_URL points the same client at another compatible endpoint; only
    Kimi has been run.
    """

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport
        self._client: httpx.Client | None = None

    def _connect(self) -> httpx.Client:
        # Created on first use: `review` resumes a ticket without calling the
        # model, so it must work without a key.
        if self._client is None:
            key = os.environ.get("LLM_API_KEY")
            if not key:
                raise LLMError("LLM_API_KEY is not set")
            self._client = httpx.Client(
                base_url=os.environ.get("LLM_BASE_URL", KIMI_URL),
                headers={
                    "authorization": f"Bearer {key}",
                    # Kimi rejects requests without an agent User-Agent.
                    "user-agent": "valetax-copilot/0.1",
                },
                timeout=120.0,
                transport=self._transport,
            )
        return self._client

    def parse(self, *, model: str, system: list[str], user: str, schema: type[T]) -> tuple[T, Usage]:
        client = self._connect()
        instruction = (
            "Return only a JSON object matching this schema:\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        payload = {
            "model": model,
            "max_tokens": 4096,
            "messages": [
                {"role": "system", "content": "\n\n".join(system)},
                {"role": "user", "content": f"{user}\n\n{instruction}"},
            ],
            "response_format": {"type": "json_object"},
        }
        if str(client.base_url).startswith(KIMI_URL):
            # Kimi-specific switch; other endpoints may reject an unknown field.
            payload["thinking"] = {"type": "disabled"}
        usage = Usage(model=model)
        started = time.perf_counter()
        error: Exception | None = None
        for _ in range(2):
            try:
                response = client.post("/chat/completions", json=payload)
                response.raise_for_status()
                data = response.json()
                tokens = data.get("usage") or {}
                usage.input_tokens += tokens.get("prompt_tokens", 0)
                usage.output_tokens += tokens.get("completion_tokens", 0)
                content = data["choices"][0]["message"]["content"] or ""
                parsed = schema.model_validate_json(_strip_fence(content))
            except ValidationError as invalid:
                error = invalid  # malformed JSON is worth one more attempt
                continue
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as failed:
                raise LLMError(f"{type(failed).__name__}: {failed}") from failed
            usage.latency_s = round(time.perf_counter() - started, 2)
            return parsed, usage
        raise LLMError(f"invalid JSON from {model}: {error}") from error


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return text
