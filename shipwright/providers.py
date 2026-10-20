"""Provider abstractions for Shipwright's model backends.

The module deliberately uses only Python's standard library so that a provider
can be used by the TUI or by a headless worker without adding an HTTP runtime
dependency.
"""

from __future__ import annotations

import json
import math
import socket
from dataclasses import dataclass
from os import environ
from typing import Any, Mapping, Protocol, Sequence
from urllib import error as url_error
from urllib import request as url_request
from urllib.parse import quote, urlencode, urlparse


Message = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """Normalized response returned by every provider."""

    content: str
    model: str = ""
    finish_reason: str | None = None
    usage: Mapping[str, Any] | None = None
    raw: Mapping[str, Any] | None = None

    @property
    def text(self) -> str:
        """Compatibility alias for callers that refer to generated text."""

        return self.content


class Provider(Protocol):
    """Interface implemented by model providers."""

    model: str

    def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        """Generate one response for a chat message sequence."""

        ...


class ProviderError(RuntimeError):
    """Raised when a provider request or response cannot be used."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.cause = cause


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Configuration accepted by :func:`create_provider`."""

    provider: str = "ollama"
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout: float | None = None


DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:7b"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
DEFAULT_ANTHROPIC_MODEL = "claude-3-5-sonnet-latest"
DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_TIMEOUT = 60.0

OPENAI_COMPATIBLE_PRESETS: dict[str, str] = {
    "openai": DEFAULT_OPENAI_BASE_URL,
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.xyz/v1",
    "mistral": "https://api.mistral.ai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "xai": "https://api.x.ai/v1",
    "perplexity": "https://api.perplexity.ai",
}


def _validate_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url must be a non-empty URL")
    normalized = base_url.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must use http:// or https:// and include a host")
    return normalized


def _validate_timeout(timeout: float) -> float:
    try:
        value = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be a positive number")
    return value


def _validate_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    if isinstance(messages, (str, bytes)):
        raise ProviderError("messages must be a sequence of message mappings")
    try:
        items = list(messages)
    except TypeError as exc:
        raise ProviderError("messages must be a sequence of message mappings", cause=exc) from exc
    if not items:
        raise ProviderError("messages must not be empty")

    normalized: list[dict[str, Any]] = []
    for index, message in enumerate(items):
        if not isinstance(message, Mapping):
            raise ProviderError(f"message {index} must be a mapping")
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str) or not role:
            raise ProviderError(f"message {index} must include a non-empty string role")
        if not isinstance(content, str):
            raise ProviderError(f"message {index} must include string content")
        normalized.append(dict(message))
    return normalized


def _validate_generation_options(temperature: float | None, max_tokens: int | None) -> None:
    if temperature is not None:
        try:
            value = float(temperature)
        except (TypeError, ValueError) as exc:
            raise ProviderError("temperature must be a finite non-negative number", cause=exc) from exc
        if not math.isfinite(value) or value < 0:
            raise ProviderError("temperature must be a finite non-negative number")
    if max_tokens is not None and (not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0):
        raise ProviderError("max_tokens must be a positive integer")


def _response_metadata(payload: dict[str, Any], requested_model: str) -> tuple[str, str | None, Mapping[str, Any] | None]:
    model = payload.get("model", requested_model)
    if not isinstance(model, str):
        raise ProviderError("provider response field 'model' must be a string")
    finish_reason = payload.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ProviderError("provider response field 'finish_reason' must be a string or null")
    usage = payload.get("usage")
    if usage is not None and not isinstance(usage, Mapping):
        raise ProviderError("provider response field 'usage' must be an object or null")
    return model, finish_reason, usage


def _post_json(url: str, payload: Mapping[str, Any], headers: Mapping[str, str], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = url_request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        response = url_request.urlopen(request, timeout=timeout)
        try:
            response_body = response.read()
        finally:
            response.close()
    except url_error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except OSError:
            detail = ""
        suffix = f": {detail[:300]}" if detail else ""
        raise ProviderError(
            f"provider returned HTTP {exc.code}{suffix}",
            status_code=exc.code,
            cause=exc,
        ) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ProviderError("provider request timed out", cause=exc) from exc
    except url_error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).lower():
            message = "provider request timed out"
        else:
            message = f"provider request failed: {reason}"
        raise ProviderError(message, cause=exc) from exc
    except OSError as exc:
        raise ProviderError(f"provider request failed: {exc}", cause=exc) from exc

    try:
        decoded = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("provider returned invalid JSON", cause=exc) from exc
    if not isinstance(decoded, dict):
        raise ProviderError("provider response must be a JSON object")
    return decoded


class OllamaProvider:
    """Provider for Ollama's non-streaming ``/api/chat`` endpoint."""

    def __init__(
        self,
        model: str = DEFAULT_OLLAMA_MODEL,
        *,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = _validate_url(base_url)
        self.model = self._validate_model(model)
        self.timeout = _validate_timeout(timeout)

    @staticmethod
    def _validate_model(model: str) -> str:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        return model.strip()

    def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        _validate_generation_options(temperature, max_tokens)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _validate_messages(messages),
            "stream": False,
        }
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        if options:
            payload["options"] = options

        response = _post_json(
            f"{self.base_url}/api/chat",
            payload,
            {"Content-Type": "application/json", "Accept": "application/json"},
            self.timeout,
        )
        message = response.get("message")
        if not isinstance(message, Mapping):
            raise ProviderError("Ollama response must include a message object")
        content = message.get("content")
        if not isinstance(content, str):
            raise ProviderError("Ollama response message must include string content")
        response_for_metadata = dict(response)
        if "finish_reason" not in response_for_metadata and "done_reason" in response_for_metadata:
            response_for_metadata["finish_reason"] = response_for_metadata["done_reason"]
        model, finish_reason, usage = _response_metadata(response_for_metadata, self.model)
        return ProviderResponse(content, model, finish_reason, usage, response)

    chat = complete


class OpenAICompatibleProvider:
    """Provider for OpenAI-compatible ``/chat/completions`` endpoints."""

    def __init__(
        self,
        model: str = DEFAULT_OPENAI_MODEL,
        *,
        base_url: str = DEFAULT_OPENAI_BASE_URL,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = _validate_url(base_url)
        self.model = OllamaProvider._validate_model(model)
        self.api_key = api_key or ""
        self.timeout = _validate_timeout(timeout)

    def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        _validate_generation_options(temperature, max_tokens)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _validate_messages(messages),
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = _post_json(f"{self.base_url}/chat/completions", payload, headers, self.timeout)

        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("OpenAI-compatible response must include a non-empty choices array")
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise ProviderError("OpenAI-compatible response choice must be an object")
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise ProviderError("OpenAI-compatible response choice must include a message object")
        content = message.get("content")
        if not isinstance(content, str):
            raise ProviderError("OpenAI-compatible response message must include string content")

        response_for_metadata = dict(response)
        response_for_metadata["finish_reason"] = choice.get("finish_reason")
        model, finish_reason, usage = _response_metadata(response_for_metadata, self.model)
        return ProviderResponse(content, model, finish_reason, usage, response)

    chat = complete


class AnthropicProvider:
    """Provider for Anthropic's Messages API."""

    def __init__(
        self,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        *,
        base_url: str = DEFAULT_ANTHROPIC_BASE_URL,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT,
        anthropic_version: str = "2023-06-01",
    ) -> None:
        self.base_url = _validate_url(base_url)
        self.model = OllamaProvider._validate_model(model)
        self.api_key = self._validate_api_key(api_key)
        self.timeout = _validate_timeout(timeout)
        self.anthropic_version = self._validate_api_key(anthropic_version)

    @staticmethod
    def _validate_api_key(api_key: str) -> str:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        return api_key.strip()

    def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        _validate_generation_options(temperature, max_tokens)
        normalized = _validate_messages(messages)
        system_messages = [message["content"] for message in normalized if message["role"] == "system"]
        chat_messages = [message for message in normalized if message["role"] != "system"]
        if not chat_messages:
            raise ProviderError("Anthropic requests must include at least one non-system message")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": chat_messages,
            "max_tokens": max_tokens or 1024,
        }
        if system_messages:
            payload["system"] = "\n\n".join(system_messages)
        if temperature is not None:
            payload["temperature"] = temperature
        response = _post_json(
            f"{self.base_url}/v1/messages",
            payload,
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": self.anthropic_version,
            },
            self.timeout,
        )
        content_blocks = response.get("content")
        if not isinstance(content_blocks, list):
            raise ProviderError("Anthropic response must include a content array")
        text = "".join(
            block.get("text", "")
            for block in content_blocks
            if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str)
        )
        if not text:
            raise ProviderError("Anthropic response did not include text content")
        response_for_metadata = dict(response)
        response_for_metadata["finish_reason"] = response.get("stop_reason")
        model, finish_reason, usage = _response_metadata(response_for_metadata, self.model)
        return ProviderResponse(text, model, finish_reason, usage, response)

    chat = complete


class GeminiProvider:
    """Provider for Google's Gemini ``generateContent`` API."""

    def __init__(
        self,
        model: str = DEFAULT_GEMINI_MODEL,
        *,
        base_url: str = DEFAULT_GEMINI_BASE_URL,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = _validate_url(base_url)
        self.model = OllamaProvider._validate_model(model)
        self.api_key = AnthropicProvider._validate_api_key(api_key)
        self.timeout = _validate_timeout(timeout)

    def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        _validate_generation_options(temperature, max_tokens)
        normalized = _validate_messages(messages)
        system_messages = [message["content"] for message in normalized if message["role"] == "system"]
        contents: list[dict[str, Any]] = []
        for message in normalized:
            if message["role"] == "system":
                continue
            role = "model" if message["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": message["content"]}]})
        if not contents:
            raise ProviderError("Gemini requests must include at least one non-system message")
        payload: dict[str, Any] = {"contents": contents}
        if system_messages:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_messages)}]}
        generation_config: dict[str, Any] = {}
        if temperature is not None:
            generation_config["temperature"] = temperature
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        if generation_config:
            payload["generationConfig"] = generation_config
        endpoint = f"{self.base_url}/v1beta/models/{quote(self.model, safe='')}:generateContent?{urlencode({'key': self.api_key})}"
        response = _post_json(
            endpoint,
            payload,
            {"Content-Type": "application/json", "Accept": "application/json"},
            self.timeout,
        )
        candidates = response.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ProviderError("Gemini response must include a non-empty candidates array")
        candidate = candidates[0]
        content = candidate.get("content") if isinstance(candidate, Mapping) else None
        parts = content.get("parts") if isinstance(content, Mapping) else None
        text = "".join(
            part.get("text", "")
            for part in parts or []
            if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        )
        if not text:
            raise ProviderError("Gemini response did not include text content")
        response_for_metadata = dict(response)
        response_for_metadata["model"] = self.model
        response_for_metadata["finish_reason"] = candidate.get("finishReason") if isinstance(candidate, Mapping) else None
        model, finish_reason, usage = _response_metadata(response_for_metadata, self.model)
        return ProviderResponse(text, model, finish_reason, response.get("usageMetadata"), response)

    chat = complete


def _coerce_config(config: ProviderConfig | Mapping[str, Any]) -> ProviderConfig:
    if isinstance(config, ProviderConfig):
        return config
    if not isinstance(config, Mapping):
        raise TypeError("config must be a ProviderConfig or mapping")
    provider = config.get("provider", config.get("kind", config.get("type", "ollama")))
    return ProviderConfig(
        provider=str(provider),
        base_url=config.get("base_url"),
        api_key=config.get("api_key"),
        model=config.get("model"),
        timeout=config.get("timeout"),
    )


def provider_from_config(config: ProviderConfig | Mapping[str, Any]) -> Provider:
    """Construct a provider from a dataclass or mapping configuration."""

    selected = _coerce_config(config)
    kind = selected.provider.strip().lower().replace("_", "-")
    if kind in {"ollama", "local", "local-ollama"}:
        return OllamaProvider(
            model=selected.model or DEFAULT_OLLAMA_MODEL,
            base_url=selected.base_url or DEFAULT_OLLAMA_BASE_URL,
            timeout=selected.timeout if selected.timeout is not None else DEFAULT_TIMEOUT,
        )
    if kind in {"anthropic", "claude"}:
        if not selected.api_key:
            raise ValueError("api_key is required for Anthropic")
        return AnthropicProvider(
            model=selected.model or DEFAULT_ANTHROPIC_MODEL,
            base_url=selected.base_url or DEFAULT_ANTHROPIC_BASE_URL,
            api_key=selected.api_key,
            timeout=selected.timeout if selected.timeout is not None else DEFAULT_TIMEOUT,
        )
    if kind in {"gemini", "google", "google-gemini"}:
        if not selected.api_key:
            raise ValueError("api_key is required for Gemini")
        return GeminiProvider(
            model=selected.model or DEFAULT_GEMINI_MODEL,
            base_url=selected.base_url or DEFAULT_GEMINI_BASE_URL,
            api_key=selected.api_key,
            timeout=selected.timeout if selected.timeout is not None else DEFAULT_TIMEOUT,
        )
    if kind in {"openai-compatible", "openai-compatible-chat"} or kind in OPENAI_COMPATIBLE_PRESETS:
        return OpenAICompatibleProvider(
            model=selected.model or DEFAULT_OPENAI_MODEL,
            base_url=selected.base_url or OPENAI_COMPATIBLE_PRESETS.get(kind, DEFAULT_OPENAI_BASE_URL),
            api_key=selected.api_key,
            timeout=selected.timeout if selected.timeout is not None else DEFAULT_TIMEOUT,
        )
    raise ValueError(f"unsupported provider: {selected.provider!r}")


def provider_from_env(env: Mapping[str, str] | None = None) -> Provider:
    """Construct a provider from ``SHIPWRIGHT_*`` environment variables.

    ``SHIPWRIGHT_PROVIDER`` defaults to ``ollama``. OpenAI-compatible setups
    may also use the conventional ``OPENAI_API_KEY`` and ``OPENAI_BASE_URL``
    variables when their Shipwright-specific equivalents are absent.
    """

    values = environ if env is None else env
    kind = values.get("SHIPWRIGHT_PROVIDER", "ollama").strip().lower()
    base_url = values.get("SHIPWRIGHT_BASE_URL")
    if not base_url:
        base_url = values.get("OLLAMA_BASE_URL" if kind in {"ollama", "local", "local-ollama"} else "OPENAI_BASE_URL")
    api_key = values.get("SHIPWRIGHT_API_KEY")
    if not api_key:
        provider_key_names = {
            "anthropic": "ANTHROPIC_API_KEY",
            "claude": "ANTHROPIC_API_KEY",
            "gemini": "GOOGLE_API_KEY",
            "google": "GOOGLE_API_KEY",
            "google-gemini": "GOOGLE_API_KEY",
        }
        api_key = values.get(provider_key_names.get(kind, "OPENAI_API_KEY"))
    timeout_text = values.get("SHIPWRIGHT_TIMEOUT")
    timeout = float(timeout_text) if timeout_text else None
    return provider_from_config(
        ProviderConfig(
            provider=kind,
            base_url=base_url,
            api_key=api_key,
            model=values.get("SHIPWRIGHT_MODEL"),
            timeout=timeout,
        )
    )


def create_provider(
    config: ProviderConfig | Mapping[str, Any] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Provider:
    """Create a provider from explicit config, or from the environment."""

    return provider_from_env(env) if config is None else provider_from_config(config)


__all__ = [
    "DEFAULT_OLLAMA_BASE_URL",
    "DEFAULT_OLLAMA_MODEL",
    "DEFAULT_OPENAI_BASE_URL",
    "DEFAULT_OPENAI_MODEL",
    "DEFAULT_ANTHROPIC_BASE_URL",
    "DEFAULT_ANTHROPIC_MODEL",
    "DEFAULT_GEMINI_BASE_URL",
    "DEFAULT_GEMINI_MODEL",
    "DEFAULT_TIMEOUT",
    "OPENAI_COMPATIBLE_PRESETS",
    "AnthropicProvider",
    "GeminiProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "Provider",
    "ProviderConfig",
    "ProviderError",
    "ProviderResponse",
    "create_provider",
    "provider_from_config",
    "provider_from_env",
]
