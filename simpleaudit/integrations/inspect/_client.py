"""An AnyLLM-style client that sends SimpleAudit's model calls to an Inspect model."""

from __future__ import annotations

import warnings
from types import SimpleNamespace
from typing import Any

from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageUser,
    ContentImage,
    ContentText,
    GenerateConfig,
    Model,
    ResponseSchema,
)
from inspect_ai.util import JSONSchema

#: SimpleAudit generation params (OpenAI spelling) -> GenerateConfig fields.
_PARAMS = {
    "temperature": "temperature",
    "max_tokens": "max_tokens",
    "top_p": "top_p",
    "top_k": "top_k",
    "seed": "seed",
    "stop": "stop_seqs",
    "frequency_penalty": "frequency_penalty",
    "presence_penalty": "presence_penalty",
    "reasoning_effort": "reasoning_effort",
    "extra_headers": "extra_headers",
    "extra_body": "extra_body",
}

_warned_params: set = set()
_warned_schema = False


def _content(block: dict[str, Any]):
    if block.get("type") == "image_url":
        return ContentImage(image=block["image_url"]["url"])
    return ContentText(text=block["text"])


def to_chat_message(message: dict[str, Any]) -> ChatMessage:
    """Convert one OpenAI-style message, as ModelAuditor builds them, to Inspect."""
    content = message["content"]
    if isinstance(content, list):
        content = [_content(block) for block in content]
    roles = {"system": ChatMessageSystem, "user": ChatMessageUser, "assistant": ChatMessageAssistant}
    return roles[message["role"]](content=content)


def generate_config(response_format: dict[str, Any] | None, params: dict[str, Any]) -> GenerateConfig:
    fields: dict[str, Any] = {}
    for key, value in params.items():
        if key not in _PARAMS:
            if key not in _warned_params:
                _warned_params.add(key)
                warnings.warn(
                    f"Generation param {key!r} has no Inspect equivalent and is ignored.",
                    stacklevel=3,
                )
            continue
        if key == "stop" and isinstance(value, str):
            value = [value]
        fields[_PARAMS[key]] = value
    if response_format and response_format.get("type") == "json_schema":
        response_schema = _response_schema(response_format["json_schema"])
        if response_schema is not None:
            fields["response_schema"] = response_schema
    return GenerateConfig(**fields)


def _response_schema(spec: dict[str, Any]) -> ResponseSchema | None:
    """The judge's JSON schema for Inspect, or None when Inspect cannot carry it whole.

    Inspect's schema model drops keywords it doesn't define ($defs, $ref,
    const, ...). Sending such a schema half-converted would constrain the
    reply wrongly, so it is not sent; SimpleAudit parses the reply as JSON
    either way.
    """
    global _warned_schema
    converted = JSONSchema.model_validate(spec["schema"])
    if converted.model_dump(exclude_none=True, by_alias=True) != spec["schema"]:
        if not _warned_schema:
            _warned_schema = True
            warnings.warn(
                "The judge's response schema uses keywords Inspect cannot express; "
                "it is not sent, and the judge's reply is parsed as JSON without it.",
                stacklevel=4,
            )
        return None
    return ResponseSchema(name=spec.get("name", "response"), json_schema=converted)


class InspectModelClient:
    """Stands in for an AnyLLM client and calls an Inspect ``Model`` instead.

    ``ModelAuditor`` talks to its target, auditor and judge through
    ``acompletion(model=..., messages=[...], response_format=..., **params)``.
    Routing that through Inspect puts every call in the Inspect transcript,
    under the eval's limits, retries and usage accounting.

    SimpleAudit turns a failing call into an ERROR result. Inside Inspect that
    would hide token limits and sample terminations, so the client keeps the
    first exception in ``error`` and the solver re-raises it.
    """

    def __init__(self, model: Model) -> None:
        self.model = model
        self.error: BaseException | None = None

    async def acompletion(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str | None = None,
        stream: bool = False,
        response_format: dict[str, Any] | None = None,
        **params: Any,
    ) -> Any:
        try:
            output = await self.model.generate(
                [to_chat_message(m) for m in messages],
                config=generate_config(response_format, params),
            )
        except Exception as exc:
            if self.error is None:
                self.error = exc
            raise
        usage = output.usage
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=output.completion))],
            usage=SimpleNamespace(
                prompt_tokens=usage.input_tokens if usage else 0,
                completion_tokens=usage.output_tokens if usage else 0,
            ),
        )
