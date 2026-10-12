"""通用模型配置、实际 HTTP 请求体及完整工具回合；仅使用回环服务。"""
from __future__ import annotations

import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.utils.model_loader import build_model, model_config


@pytest.fixture
def endpoint():
    requests = []
    responses = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            status, body = responses.pop(0) if responses else (200, {
                "id": "synthetic", "object": "chat.completion", "created": 0, "model": "synthetic",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            })
            if status == 299:
                time.sleep(0.15)
                return
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}/v1", requests=requests, responses=responses)
    server.shutdown()
    server.server_close()
    thread.join()


def config(endpoint, **changes):
    return {"api_key": "synthetic-only-key", "base_url": endpoint.url, "model": "synthetic-model",
            "timeout": 2, "max_retries": 0, "max_tokens": 128, **changes}


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_actual_chat_request_sends_only_selected_length_parameter(endpoint, field):
    model = build_model(config(endpoint, token_limit_param=field))
    assert not endpoint.requests
    assert model.invoke("hello").content == "ok"
    path, body = endpoint.requests[0]
    assert path == "/v1/chat/completions"
    assert body[field] == 128
    assert ({"max_tokens", "max_completion_tokens"} - {field}).isdisjoint(body)
    assert "temperature" not in body and "reasoning" not in body and "response_format" not in body


def test_complete_agent_tool_round_trip(endpoint):
    from langchain.agents import create_agent
    from langchain_core.tools import tool
    @tool
    def read_cpu() -> str:
        """读取合成 CPU 指标。"""
        return "CPU 72%"
    endpoint.responses.extend([
        (200, {"id": "call", "object": "chat.completion", "created": 0, "model": "synthetic",
               "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                   "role": "assistant", "content": None, "tool_calls": [
                       {"id": "cpu-call", "type": "function", "function": {"name": "read_cpu", "arguments": "{}"}}]}}]}),
        (200, {"id": "answer", "object": "chat.completion", "created": 0, "model": "synthetic",
               "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "CPU 72%"}}]}),
    ])
    agent = create_agent(build_model(config(endpoint)), tools=[read_cpu])
    result = agent.invoke({"messages": [{"role": "user", "content": "CPU?"}]})
    assert result["messages"][-1].content == "CPU 72%"
    assert len(endpoint.requests) == 2
    assert endpoint.requests[1][1]["messages"][-1]["role"] == "tool"
    assert endpoint.requests[1][1]["messages"][-1]["content"] == "CPU 72%"


@pytest.mark.parametrize("changes", [
    {"timeout": "nan"}, {"timeout": 0}, {"timeout": "inf"}, {"max_retries": -1},
    {"max_tokens": 0}, {"max_tokens": True}, {"token_limit_param": "unknown"},
    {"base_url": "https://user:secret@example.invalid"}, {"model": ""}, {"api_key": ""},
])
def test_invalid_parameters_rejected_before_network(endpoint, changes):
    with pytest.raises(ValueError):
        build_model(config(endpoint, **changes))
    assert not endpoint.requests


def test_page_configuration_is_whole_and_clear_never_uses_environment(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "environment-only")
    monkeypatch.setenv("MODEL_BASE_URL", "https://environment.invalid/v1")
    monkeypatch.setenv("MODEL_NAME", "environment-model")
    assert model_config()["api_key"] == "environment-only"
    page = model_config({"api_key": "", "model": "page-model"})
    assert page["api_key"] == "" and page["base_url"] == ""
    with pytest.raises(ValueError):
        build_model(page)


def test_configuration_cache_retains_old_instances(monkeypatch):
    # 独立加载模块，保留全套测试的禁止真实模型保护。
    path = Path(__file__).parents[1] / "src/utils/model_loader.py"
    spec = importlib.util.spec_from_file_location("isolated_loader", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(module, "build_model", lambda config: calls.append(config) or object())
    first_config = {"api_key": "key-one", "base_url": "https://example.invalid/v1", "model": "one"}
    first = module.get_model(first_config)
    assert module.get_model(dict(reversed(list(first_config.items())))) is first
    second = module.get_model({**first_config, "model": "two"})
    assert second is not first and len(calls) == 2
    assert module.get_model(first_config) is first


def test_client_failure_does_not_expose_vendor_response(endpoint, monkeypatch):
    from src.plugins.llm_clients import openai_compatible_client
    from src.interfaces.llm_client import LLMError
    endpoint.responses.append((401, {"error": {"message": "synthetic-sensitive-vendor-context"}}))
    monkeypatch.setattr(openai_compatible_client, "get_model", lambda *_: build_model(config(endpoint)))
    with pytest.raises(LLMError) as error:
        openai_compatible_client.OpenAICompatibleLLMClient().complete("hello")
    assert "synthetic-sensitive" not in str(error.value)


def test_actual_http_timeout_is_bounded_and_sanitized(endpoint, monkeypatch):
    from src.plugins.llm_clients import openai_compatible_client
    from src.interfaces.llm_client import LLMError
    endpoint.responses.append((299, {}))
    monkeypatch.setattr(openai_compatible_client, "get_model", lambda *_: build_model(config(endpoint, timeout=0.03)))
    with pytest.raises(LLMError) as error:
        openai_compatible_client.OpenAICompatibleLLMClient().complete("hello")
    assert len(endpoint.requests) == 1
    assert "synthetic-only-key" not in str(error.value)
