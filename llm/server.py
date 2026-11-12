"""HTTP API for the assistant:  POST /ask  and  GET /health.   Run: uvicorn server:app --host 0.0.0.0 --port 8000"""
import logging
import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from assistant import Assistant
from db import run_select
from llm_client import LLMError, OllamaClient

logging.basicConfig(level=logging.INFO)


class Turn(BaseModel):
    question: str = Field(max_length=500)
    sql: str = Field(max_length=4000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    history: list[Turn] = Field(default_factory=list, max_length=4)


def create_app(assistant):
    app = FastAPI(title="Barca assistant")

    @app.get("/health")
    def health():
        try:
            run_select(assistant.dsn, "SELECT 1")
            database = "ok"
        except Exception as e:                         # report, never crash the health check
            database = f"error: {str(e).splitlines()[0]}"
        try:
            _, rows, _ = run_select(assistant.dsn, "SELECT to_regclass('dw.v_player_match') IS NOT NULL "
                                                   "AND to_regclass('dw.v_shot') IS NOT NULL AND to_regclass('dw.v_match') IS NOT NULL")
            views = "ok" if rows[0][0] else "missing"
        except Exception:
            views = "unknown"
        llm = getattr(assistant.llm, "health", lambda: "unknown")()
        return {"database": database, "views": views, "llm": llm, "model": getattr(assistant.llm, "model", "unknown")}

    @app.post("/ask")
    def ask(req: AskRequest):
        try:
            return assistant.ask(req.question, [t.model_dump() for t in req.history])
        except LLMError as e:
            raise HTTPException(status_code=503, detail=str(e))

    return app


def build_default():
    llm = OllamaClient(
        os.environ.get("OLLAMA_URL", "http://localhost:11434"),
        os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b"),
        num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", "8192")),
        num_thread=int(os.environ["OLLAMA_NUM_THREAD"]) if os.environ.get("OLLAMA_NUM_THREAD") else None,
    )
    phrase = os.environ.get("PHRASE_ANSWERS", "false").lower() in ("1", "true", "yes")
    return Assistant(llm, os.environ["DATABASE_URL"], phrase_answers=phrase)


app = create_app(build_default()) if os.environ.get("DATABASE_URL") else None
