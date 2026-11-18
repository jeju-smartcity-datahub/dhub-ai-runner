"""OpenAI-compatible HTTP API over the collection's agents.

POST   /v1/chat/completions        model = agent name; X-Session-Id keeps history
GET    /v1/models                  the agents
GET    /v1/sessions/{id}?agent=    a session's messages
DELETE /v1/sessions/{id}?agent=
POST   /v1/agents/{name}/invoke    {"inputs": {...}} -> {"output": ...} (pipeline agent steps)
POST   /v1/agents/{name}/chat      {"session_id", "messages"} -> {"content", "pending", "events"}
GET    /health
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

from commons.collection import Collection, CollectionError
from commons.scripts import Resources
from commons.state import State
from commons.storage import Storage
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from langchain_core.messages import messages_from_dict

from .runtime import Agent, AgentError

log = logging.getLogger(__name__)


class AgentPool:
    """Agents built on first use, so one agent's missing key does not stop the rest.
    ``storage`` holds the collection's tables the builtin tools read."""

    def __init__(
        self,
        collection: Collection,
        state: State,
        names: list[str] | None = None,
        storage: Storage | None = None,
    ):
        self.collection = collection
        self.state = state
        self.storage = storage
        self.resources = Resources(collection)
        available = [d["name"] for d in collection.list("agent")]
        self.names = names or available
        missing = sorted(set(self.names) - set(available))
        if missing:
            raise CollectionError(f"agents {missing} not found in collection")
        self._agents: dict[str, Agent] = {}
        self._lock = threading.Lock()
        self._session_locks: dict[tuple[str, str], threading.Lock] = {}

    def get(self, name: str | None) -> Agent:
        if not name:
            if len(self.names) != 1:
                raise HTTPException(400, f"set 'model' to one of the agents: {self.names}")
            name = self.names[0]
        if name not in self.names:
            raise HTTPException(404, f"agent {name!r} not found; agents: {self.names}")
        with self._lock:
            if name not in self._agents:
                self._agents[name] = Agent(
                    self.collection, self.resources, name=name, storage=self.storage
                )
            return self._agents[name]

    def drop(self, name: str) -> None:
        """Forget a built agent; it is built again on next use."""
        with self._lock:
            self._agents.pop(name, None)

    def loaded(self) -> set[str]:
        with self._lock:
            return set(self._agents)

    def chat(
        self,
        agent: Agent,
        messages: list[dict[str, Any]],
        session_id: str | None,
        ephemeral: bool = False,
        on_event: Any = None,
    ) -> dict[str, Any]:
        """One turn; with a session id the history is kept in the state file."""
        if not session_id:
            return agent.chat(agent.new_session(), messages, on_event)
        with self._lock:
            lock = self._session_locks.setdefault((agent.name, session_id), threading.Lock())
        with lock:
            stored = None if ephemeral else self.state.get_session(agent.name, session_id)
            session = stored or agent.new_session()
            if stored and stored.get("messages"):
                # The server holds the history: only the messages after the
                # client's last assistant turn are new.
                last = max(
                    (i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1
                )
                messages = messages[last + 1 :]
            result = agent.chat(session, messages, on_event)
            if not ephemeral:
                self.state.save_session(agent.name, session_id, session)
            return result

    def session(self, agent: str, session_id: str) -> dict[str, Any] | None:
        session = self.state.get_session(agent, session_id)
        if session is None:
            return None
        messages = [
            {
                "role": m.type,
                "content": m.content,
                **({"tool_calls": m.tool_calls} if getattr(m, "tool_calls", None) else {}),
                **({"tool_call_id": m.tool_call_id} if getattr(m, "tool_call_id", None) else {}),
            }
            for m in messages_from_dict(session.get("messages") or [])
        ]
        return {
            "session_id": session_id,
            "agent": agent,
            "messages": messages,
            "variables": session.get("variables"),
            "pending": session.get("pending"),
        }


def create_app(
    collection: Collection,
    state: State,
    names: list[str] | None = None,
    storage: Storage | None = None,
) -> FastAPI:
    app = FastAPI(title="dhub2-runner agents")
    pool = AgentPool(collection, state, names, storage)
    run_turn = pool.chat

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [{"id": n, "object": "model", "owned_by": "dhub2-runner"} for n in pool.names],
        }

    @app.post("/v1/chat/completions", response_model=None)
    def chat_completions(
        body: dict[str, Any],
        x_session_id: str | None = Header(default=None),
        x_session_mode: str | None = Header(default=None),
    ) -> Any:
        agent = pool.get(body.get("model"))
        messages = body.get("messages") or []
        ephemeral = x_session_mode == "ephemeral"
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        if body.get("stream"):
            return StreamingResponse(
                _stream(
                    completion_id,
                    agent,
                    lambda emit: run_turn(agent, messages, x_session_id, ephemeral, emit),
                ),
                media_type="text/event-stream",
            )
        try:
            result = run_turn(agent, messages, x_session_id, ephemeral)
        except AgentError as exc:
            raise HTTPException(409, str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:  # the model or a tool's backend failed
            log.exception("agent %s failed", agent.name)
            raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc
        message: dict[str, Any] = {"role": "assistant", "content": result["content"]}
        if result["pending"]:
            message["tool_calls"] = _tool_calls(result["pending"])
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": agent.name,
            "session_id": x_session_id,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if result["pending"] else "stop",
                }
            ],
        }

    @app.get("/v1/sessions/{session_id}")
    def get_session(session_id: str, agent: str | None = None) -> dict[str, Any]:
        session = pool.session(pool.get(agent).name, session_id)
        if session is None:
            raise HTTPException(404, "session not found")
        return session

    @app.delete("/v1/sessions/{session_id}")
    def delete_session(session_id: str, agent: str | None = None) -> dict[str, Any]:
        return {"deleted": state.delete_session(pool.get(agent).name, session_id)}

    @app.post("/v1/agents/{name}/invoke")
    def invoke(name: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"output": pool.get(name).invoke(body.get("inputs") or {})}

    @app.post("/v1/agents/{name}/chat")
    def chat(name: str, body: dict[str, Any]) -> dict[str, Any]:
        """One turn as the runner's web app shows it: ``{"session_id",
        "messages"}`` -> ``{"session_id", "content", "pending", "events"}``
        (how the web app talks to an agent running in its own pod)."""
        agent = pool.get(name)
        session_id = body.get("session_id") or uuid.uuid4().hex[:12]
        events: list[dict[str, Any]] = []
        try:
            result = pool.chat(
                agent,
                body.get("messages") or [],
                session_id,
                on_event=lambda kind, data: events.append({"type": kind, **data}),
            )
        except AgentError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:  # the model or a tool's backend failed
            log.exception("agent %s failed", name)
            raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc
        return {"session_id": session_id, "events": events, **result}

    return app


def _tool_calls(pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": c["id"],
            "type": "function",
            "function": {"name": c["name"], "arguments": json.dumps(c["args"], ensure_ascii=False)},
        }
        for c in pending
    ]


def _stream(completion_id: str, agent: Agent, run: Any) -> Iterator[str]:
    """SSE in OpenAI chunk format. Tool activity goes out as ``delta.tool_progress``;
    the answer is sent whole once the agent is done."""
    events: queue.Queue = queue.Queue()

    def worker() -> None:
        try:
            events.put(
                ("done", run(lambda kind, data: events.put(("event", {"type": kind, **data}))))
            )
        except Exception as exc:  # reported to the client in the stream
            log.exception("agent %s failed", agent.name)
            events.put(("error", str(exc)))

    threading.Thread(target=worker, daemon=True).start()

    def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": agent.name,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"

    yield chunk({"role": "assistant"})
    while True:
        kind, data = events.get()
        if kind == "event":
            yield chunk({"tool_progress": data})
        elif kind == "error":
            yield f"data: {json.dumps({'error': {'message': data}}, ensure_ascii=False)}\n\n"
            break
        else:
            delta: dict[str, Any] = {"content": data["content"]}
            if data["pending"]:
                delta["tool_calls"] = _tool_calls(data["pending"])
            yield chunk(delta)
            yield chunk({}, "tool_calls" if data["pending"] else "stop")
            break
    yield "data: [DONE]\n\n"
