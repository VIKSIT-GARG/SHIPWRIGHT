import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from shipwright.providers import (
    AnthropicProvider,
    GeminiProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    ProviderError,
    ProviderResponse,
    create_provider,
)


class _CaptureHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    response_payload: dict[str, Any] = {}
    response_status = 200

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.__class__.requests.append(
            {
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "payload": json.loads(body),
            }
        )
        encoded = json.dumps(self.__class__.response_payload).encode()
        self.send_response(self.__class__.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture()
def server() -> Any:
    _CaptureHandler.requests = []
    _CaptureHandler.response_status = 200
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, _CaptureHandler
    finally:
        httpd.shutdown()
        thread.join(timeout=2)
        httpd.server_close()


def test_ollama_posts_non_streaming_chat_and_parses_response(server: Any) -> None:
    httpd, handler = server
    handler.response_payload = {
        "model": "qwen2.5-coder",
        "message": {"role": "assistant", "content": "done"},
        "done": True,
        "done_reason": "stop",
    }

    response = OllamaProvider(
        model="qwen2.5-coder",
        base_url=f"http://127.0.0.1:{httpd.server_port}/",
    ).complete(
        [{"role": "user", "content": "ship it"}],
        temperature=0.2,
        max_tokens=32,
    )

    assert response == ProviderResponse("done", "qwen2.5-coder", "stop", None, handler.response_payload)
    request = handler.requests[0]
    assert request["path"] == "/api/chat"
    assert request["payload"] == {
        "model": "qwen2.5-coder",
        "messages": [{"role": "user", "content": "ship it"}],
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 32},
    }
    assert "authorization" not in request["headers"]
    assert request["headers"]["content-type"] == "application/json"


def test_openai_compatible_posts_auth_and_parses_choice(server: Any) -> None:
    httpd, handler = server
    handler.response_payload = {
        "id": "chatcmpl-test",
        "model": "local-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1},
    }

    response = OpenAICompatibleProvider(
        model="local-model",
        base_url=f"http://127.0.0.1:{httpd.server_port}/v1/",
        api_key="secret",
    ).chat([{"role": "user", "content": "hi"}])

    assert response.content == "hello"
    assert response.text == "hello"
    assert response.model == "local-model"
    assert response.finish_reason == "stop"
    assert response.usage == {"prompt_tokens": 2, "completion_tokens": 1}
    request = handler.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["payload"] == {
        "model": "local-model",
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert request["headers"]["authorization"] == "Bearer secret"


def test_anthropic_posts_messages_and_parses_text(server: Any) -> None:
    httpd, handler = server
    handler.response_payload = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": "anthropic reply"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }

    response = AnthropicProvider(
        model="claude-test",
        base_url=f"http://127.0.0.1:{httpd.server_port}",
        api_key="secret",
    ).complete(
        [
            {"role": "system", "content": "Be concise"},
            {"role": "user", "content": "hello"},
        ],
        max_tokens=24,
    )

    assert response.content == "anthropic reply"
    assert response.finish_reason == "end_turn"
    request = handler.requests[0]
    assert request["path"] == "/v1/messages"
    assert request["headers"]["x-api-key"] == "secret"
    assert request["payload"]["system"] == "Be concise"
    assert request["payload"]["max_tokens"] == 24


def test_gemini_posts_generate_content_and_parses_text(server: Any) -> None:
    httpd, handler = server
    handler.response_payload = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "gemini reply"}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 2},
    }

    response = GeminiProvider(
        model="gemini-test",
        base_url=f"http://127.0.0.1:{httpd.server_port}",
        api_key="secret",
    ).complete([{"role": "user", "content": "hello"}])

    assert response.content == "gemini reply"
    assert response.finish_reason == "STOP"
    assert response.usage == {"promptTokenCount": 2, "candidatesTokenCount": 2}
    request = handler.requests[0]
    assert request["path"] == "/v1beta/models/gemini-test:generateContent?key=secret"
    assert request["payload"]["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]


def test_factory_supports_explicit_and_environment_configuration() -> None:
    explicit = create_provider(
        {
            "provider": "openai-compatible",
            "base_url": "http://localhost:9000/v1",
            "api_key": "key",
            "model": "model-a",
            "timeout": 3,
        }
    )
    from_environment = create_provider(
        env={
            "SHIPWRIGHT_PROVIDER": "ollama",
            "SHIPWRIGHT_BASE_URL": "http://localhost:11434",
            "SHIPWRIGHT_MODEL": "model-b",
            "SHIPWRIGHT_TIMEOUT": "4.5",
        }
    )

    assert isinstance(explicit, OpenAICompatibleProvider)
    assert explicit.base_url == "http://localhost:9000/v1"
    assert explicit.api_key == "key"
    assert explicit.model == "model-a"
    assert explicit.timeout == 3
    assert isinstance(from_environment, OllamaProvider)
    assert from_environment.model == "model-b"
    assert from_environment.timeout == 4.5

    assert isinstance(
        create_provider(
            env={
                "SHIPWRIGHT_PROVIDER": "anthropic",
                "ANTHROPIC_API_KEY": "anthropic-secret",
            }
        ),
        AnthropicProvider,
    )
    assert isinstance(
        create_provider(
            env={
                "SHIPWRIGHT_PROVIDER": "gemini",
                "GOOGLE_API_KEY": "google-secret",
            }
        ),
        GeminiProvider,
    )
    groq = create_provider({"provider": "groq", "api_key": "groq-secret", "model": "llama"})
    assert isinstance(groq, OpenAICompatibleProvider)
    assert groq.base_url == "https://api.groq.com/openai/v1"


@pytest.mark.parametrize(
    ("provider", "payload"),
    [
        ("ollama", {"message": {"role": "assistant"}}),
        ("openai", {"choices": []}),
        ("openai", {"choices": [{"message": {"content": 42}}]}),
    ],
)
def test_malformed_provider_responses_raise_provider_error(server: Any, provider: str, payload: dict[str, Any]) -> None:
    httpd, handler = server
    handler.response_payload = payload
    instance = (
        OllamaProvider(base_url=f"http://127.0.0.1:{httpd.server_port}")
        if provider == "ollama"
        else OpenAICompatibleProvider(base_url=f"http://127.0.0.1:{httpd.server_port}")
    )

    with pytest.raises(ProviderError):
        instance.complete([{"role": "user", "content": "test"}])


def test_http_errors_and_invalid_inputs_are_provider_errors(server: Any) -> None:
    httpd, handler = server
    handler.response_status = 500
    handler.response_payload = {"error": "broken"}

    with pytest.raises(ProviderError, match="HTTP 500"):
        OllamaProvider(base_url=f"http://127.0.0.1:{httpd.server_port}").complete(
            [{"role": "user", "content": "test"}]
        )

    with pytest.raises(ProviderError, match="messages must not be empty"):
        OllamaProvider().complete([])

    with pytest.raises(ValueError, match="positive number"):
        OpenAICompatibleProvider(timeout=0)


def test_request_timeout_is_wrapped_as_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []

    def timeout(_request: Any, *, timeout: float) -> Any:
        calls.append(timeout)
        raise socket.timeout("simulated timeout")

    monkeypatch.setattr("shipwright.providers.url_request.urlopen", timeout)

    with pytest.raises(ProviderError, match="timed out"):
        OpenAICompatibleProvider(timeout=1.25).complete([{"role": "user", "content": "test"}])

    assert calls == [1.25]
