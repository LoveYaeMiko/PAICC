"""LLM client for OpenAI-compatible and Anthropic (Claude) providers.

The single entry point is :meth:`LLMClient.chat`, which reads its configuration
fresh on every call (so Settings changes take effect without a restart) and returns
a provider-agnostic result::

    {"content": str, "tool_calls": [{"id": str|None, "name": str, "arguments": dict}]}

Supported providers:

* ``deepseek`` / ``openai`` / ``ollama`` / ``custom`` — OpenAI-compatible
  ``POST {base_url}/chat/completions``.
* ``claude`` — Anthropic Messages API, with OpenAI tool schemas translated to
  Anthropic's ``{name, description, input_schema}`` shape.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

#: Providers that speak the OpenAI chat/completions protocol.
OPENAI_COMPATIBLE_PROVIDERS = {"deepseek", "openai", "ollama", "custom"}

#: Default base URLs (before the ``/v1`` suffix is applied).
_DEFAULT_BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
    "openai": "https://api.openai.com",
    "ollama": "http://localhost:11434",
}

CLAUDE_DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
CLAUDE_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


def _extract_text(content: Any) -> str:
    """Coerce an LLM ``content`` field (str | list | None) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(content)


def _coerce_dict(value: Any) -> dict[str, Any]:
    """Coerce a possibly JSON-encoded tool-arguments value into a dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _truncate(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


class LLMClient:
    """Stateless async client for the configured LLM provider."""

    def _normalize_base_url(self, provider: str, base_url: str) -> str:
        base = (base_url or "").strip().rstrip("/")
        if provider in ("deepseek", "openai", "ollama"):
            if not base:
                base = _DEFAULT_BASE_URLS.get(provider, "")
            if base and not base.endswith("/v1"):
                base += "/v1"
        return base

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        api_key: str | None = None,
        temperature: Any = None,
    ) -> dict[str, Any]:
        """Send a chat request and return ``{content, tool_calls}``.

        ``messages`` uses the OpenAI shape (``role`` in
        ``system/user/assistant/tool``); it is translated for the Anthropic
        provider automatically.

        ``model``/``api_key``/``temperature`` override the configured values on
        a per-call basis (e.g. to run a dedicated, higher-tier model for a single
        feature without disturbing the main LLM settings).
        """
        cfg = settings.get_llm_config()
        provider = str(cfg.get("provider") or "deepseek").strip().lower()
        eff_model = model or str(cfg.get("model") or "")
        base_url = str(cfg.get("base_url") or "")
        eff_api_key = api_key or str(cfg.get("api_key") or "")
        eff_temperature = cfg.get("temperature") if temperature is None else temperature

        if provider != "ollama" and not eff_api_key:
            raise RuntimeError("LLM API key not configured. Set it in Settings.")

        if provider == "claude":
            return await self._chat_claude(eff_model, base_url, eff_api_key, eff_temperature, messages, tools or [])
        if provider in OPENAI_COMPATIBLE_PROVIDERS:
            return await self._chat_openai(provider, eff_model, base_url, eff_api_key, eff_temperature, messages, tools or [])
        raise RuntimeError(f"Unsupported LLM provider: {provider}")

    # -- OpenAI-compatible -------------------------------------------------

    async def _chat_openai(
        self,
        provider: str,
        model: str,
        base_url: str,
        api_key: str,
        temperature: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        base = self._normalize_base_url(provider, base_url)
        url = f"{base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        body: dict[str, Any] = {"model": model, "messages": messages}
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = tools

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"LLM provider returned HTTP {exc.response.status_code}: {_truncate(exc.response.text)}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Failed to reach LLM provider at {url}: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"LLM provider returned invalid JSON: {exc}") from exc

        choices = data.get("choices") or []
        message = (choices[0].get("message") if choices else None) or {}

        tool_calls: list[dict[str, Any]] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            tool_calls.append(
                {
                    "id": tc.get("id"),
                    "name": fn.get("name") or "",
                    "arguments": _coerce_dict(fn.get("arguments")),
                }
            )

        return {"content": _extract_text(message.get("content")), "tool_calls": tool_calls}

    # -- Anthropic / Claude ------------------------------------------------

    async def _chat_claude(
        self,
        model: str,
        base_url: str,
        api_key: str,
        temperature: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        base = (base_url or "").strip().rstrip("/") or CLAUDE_DEFAULT_BASE_URL
        url = f"{base}/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": CLAUDE_VERSION,
            "content-type": "application/json",
        }

        system, translated = self._to_anthropic_messages(messages)
        claude_tools = [self._to_anthropic_tool(t) for t in tools]

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "messages": translated,
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        if claude_tools:
            body["tools"] = claude_tools

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Anthropic API returned HTTP {exc.response.status_code}: {_truncate(exc.response.text)}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Failed to reach Anthropic API at {url}: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"Anthropic API returned invalid JSON: {exc}") from exc

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in data.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(str(block.get("text") or ""))
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id"),
                        "name": block.get("name") or "",
                        "arguments": _coerce_dict(block.get("input")),
                    }
                )

        return {"content": "\n".join(text_parts), "tool_calls": tool_calls}

    @staticmethod
    def _to_anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        return {
            "name": fn.get("name") or "",
            "description": fn.get("description") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        }

    def _to_anthropic_messages(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        """Translate OpenAI-style messages into Anthropic's format.

        Returns ``(system_text, messages)``.
        """
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            if role == "system":
                if content:
                    system_parts.append(str(content))
                continue

            if role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": str(msg.get("tool_call_id") or ""),
                    "content": _extract_text(content),
                }
                self._append_anthropic_message(out, "user", [block])
                continue

            if role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    blocks: list[dict[str, Any]] = []
                    text = _extract_text(content)
                    if text:
                        blocks.append({"type": "text", "text": text})
                    for i, tc in enumerate(tool_calls):
                        fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": tc.get("id") or f"call_{i}",
                                "name": fn.get("name") or tc.get("name") or "",
                                "input": _coerce_dict(fn.get("arguments") or tc.get("arguments")),
                            }
                        )
                    self._append_anthropic_message(out, "assistant", blocks)
                else:
                    self._append_anthropic_message(out, "assistant", _extract_text(content))
                continue

            # user, and any unknown role is treated as a user turn.
            self._append_anthropic_message(out, "user", _extract_text(content))

        return "\n".join(system_parts), out

    @staticmethod
    def _append_anthropic_message(
        out: list[dict[str, Any]], role: str, content: Any
    ) -> None:
        """Append a message, merging consecutive turns of the same role."""
        if out and out[-1]["role"] == role:
            prev_content = out[-1]["content"]
            if isinstance(prev_content, list):
                if isinstance(content, list):
                    prev_content.extend(content)
                else:
                    prev_content.append({"type": "text", "text": content})
            else:
                if isinstance(content, list):
                    out[-1]["content"] = [{"type": "text", "text": prev_content}, *content]
                else:
                    out[-1]["content"] = f"{prev_content}\n{content}"
        else:
            out.append({"role": role, "content": content})
