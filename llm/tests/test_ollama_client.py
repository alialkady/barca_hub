"""OllamaClient against a tiny stub that imitates Ollama's HTTP API (no real model involved)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from llm_client import LLMError, OllamaClient


def serve(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def make_handler(status=200, chat_body=None, tags=None, seen=None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, payload):
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if seen is not None:
                seen.append((self.path, body))
            self._send(status, chat_body if chat_body is not None else {"message": {"role": "assistant", "content": "hi"}})

        def do_GET(self):
            self._send(200, {"models": [{"name": n} for n in (tags or [])]})
    return H


def test_chat_sends_the_settings_we_rely_on_and_parses_the_reply():
    seen = []
    srv, url = serve(make_handler(seen=seen))
    reply = OllamaClient(url, "m:1", num_ctx=8192).chat([{"role": "user", "content": "q"}])
    srv.shutdown()
    assert reply == "hi"
    path, body = seen[0]
    assert path == "/api/chat" and body["model"] == "m:1" and body["stream"] is False
    assert body["options"] == {"temperature": 0, "num_ctx": 8192}
    assert body["messages"] == [{"role": "user", "content": "q"}]


def test_missing_model_gives_the_pull_command():
    srv, url = serve(make_handler(status=404, chat_body={"error": "model not found"}))
    with pytest.raises(LLMError) as e:
        OllamaClient(url, "qwen:7b").chat([])
    srv.shutdown()
    assert "ollama pull qwen:7b" in str(e.value)


def test_server_error_is_reported():
    srv, url = serve(make_handler(status=500, chat_body={"error": "oom"}))
    with pytest.raises(LLMError) as e:
        OllamaClient(url, "m").chat([])
    srv.shutdown()
    assert "500" in str(e.value)


def test_unreachable_server_message_mentions_the_service():
    with pytest.raises(LLMError) as e:
        OllamaClient("http://127.0.0.1:1", "m").chat([])
    assert "ollama" in str(e.value).lower()


def test_malformed_reply():
    srv, url = serve(make_handler(chat_body={"unexpected": True}))
    with pytest.raises(LLMError):
        OllamaClient(url, "m").chat([])
    srv.shutdown()


def test_health_states():
    srv, url = serve(make_handler(tags=["qwen2.5-coder:7b", "llama3:latest"]))
    assert OllamaClient(url, "qwen2.5-coder:7b").health() == "ok"
    assert OllamaClient(url, "llama3").health() == "ok"          # no tag means :latest
    assert OllamaClient(url, "other:1b").health() == "model_missing"
    srv.shutdown()
    assert OllamaClient("http://127.0.0.1:1", "m").health() == "unreachable"


def test_thread_cap_is_sent_only_when_set():
    seen = []
    srv, url = serve(make_handler(seen=seen))
    OllamaClient(url, "m").chat([{"role": "user", "content": "q"}])
    OllamaClient(url, "m", num_thread=4).chat([{"role": "user", "content": "q"}])
    srv.shutdown()
    assert "num_thread" not in seen[0][1]["options"]
    assert seen[1][1]["options"]["num_thread"] == 4
