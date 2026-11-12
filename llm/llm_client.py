"""Thin client for a local Ollama server. Swap this class to use another model provider."""
import requests


class LLMError(RuntimeError):
    """The language model could not be reached or refused. The message tells the user what to do."""


class OllamaClient:
    def __init__(self, url, model, num_ctx=8192, timeout=600, num_thread=None):
        self.url, self.model, self.num_ctx, self.timeout = url.rstrip("/"), model, num_ctx, timeout
        self.num_thread = num_thread               # optional CPU thread cap, to keep the laptop responsive

    def chat(self, messages):
        # num_ctx is set explicitly: Ollama's default context window is small and silently cuts
        # the START of a long prompt (which is where our instructions and schema live).
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": "30m",                       # keep the model loaded between questions
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
        }
        if self.num_thread:
            body["options"]["num_thread"] = self.num_thread
        try:
            r = requests.post(f"{self.url}/api/chat", json=body, timeout=self.timeout)
        except requests.ConnectionError:
            raise LLMError(f"Cannot reach the language model server at {self.url}. Is the 'ollama' service running?")
        except requests.Timeout:
            raise LLMError("The language model took too long to answer (a bigger model or a CPU-only machine can be slow).")
        if r.status_code == 404:
            raise LLMError(f"Model '{self.model}' is not downloaded yet. Run: docker compose exec ollama ollama pull {self.model}")
        if r.status_code != 200:
            raise LLMError(f"The language model server returned HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()["message"]["content"]
        except (KeyError, ValueError):
            raise LLMError("Unexpected response from the language model server.")

    def health(self):
        """'ok' | 'model_missing' | 'unreachable'"""
        try:
            r = requests.get(f"{self.url}/api/tags", timeout=5)
            names = {m.get("name", "") for m in r.json().get("models", [])}
        except (requests.RequestException, ValueError):
            return "unreachable"
        wanted = self.model if ":" in self.model else self.model + ":latest"
        return "ok" if wanted in names else "model_missing"
