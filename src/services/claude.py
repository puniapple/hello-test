"""Claude API service wrapper."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic

from src.config import settings

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4096


@dataclass
class ClaudeResponse:
    """Structured response from Claude."""

    text: str
    tool_uses: list[dict[str, Any]]
    stop_reason: str
    raw_content: list[Any]


class ClaudeService:
    """Thin wrapper around Anthropic SDK with tool use and PDF support."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL):
        self.client = AsyncAnthropic(api_key=api_key or settings.anthropic_api_key)
        self.model = model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model: str | None = None,
    ) -> ClaudeResponse:
        """Send a chat request to Claude.

        messages — list of {"role": "user"|"assistant", "content": ...}
        Content can be a string or a list of content blocks (text, document, image).
        """
        kwargs = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            # остальные поля (max_tokens, temperature и т.д.) оставь как были
        }

        if system:
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        if tools:
            kwargs["tools"] = [
                {**tool, "cache_control": {"type": "ephemeral"}} if i == len(tools) - 1 else tool
                for i, tool in enumerate(tools)
            ]

        response = await self.client.messages.create(**kwargs)

        text_parts = []
        tool_uses = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(
                    {
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )

        return ClaudeResponse(
            text="".join(text_parts).strip(),
            tool_uses=tool_uses,
            stop_reason=response.stop_reason or "",
            raw_content=list(response.content),
        )


def build_pdf_content_block(pdf_bytes: bytes, filename: str = "document.pdf") -> dict[str, Any]:
    """Construct a Claude document block from raw PDF bytes."""
    encoded = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": encoded,
        },
        "title": filename,
    }


def build_text_block(text: str) -> dict[str, Any]:
    """Construct a Claude text content block."""
    return {"type": "text", "text": text}


def build_tool_result_block(tool_use_id: str, content: str) -> dict[str, Any]:
    """Construct a tool_result content block to send back to Claude."""
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }