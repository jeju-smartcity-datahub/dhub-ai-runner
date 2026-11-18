import json
from pathlib import Path
from typing import Any

import pytest
from agent import runtime
from agent.server import create_app
from commons.collection import Collection
from commons.state import State
from conftest import example_secrets
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

QUICKSTART = Path(__file__).parent.parent / "examples" / "quickstart"


class ScriptedModel(BaseChatModel):
    """Replies from a fixed list, recording what it was sent."""

    replies: list[AIMessage]
    seen: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(messages)
        return ChatResult(generations=[ChatGeneration(message=self.replies.pop(0))])


def tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": call_id}])


@pytest.fixture
def setup(tmp_path, monkeypatch):
    model = ScriptedModel(replies=[])
    monkeypatch.setattr(runtime, "create_chat_model", lambda spec, **kw: model)
    app = create_app(Collection(QUICKSTART, example_secrets()), State(tmp_path))
    return TestClient(app), model


def last_tool_result(model: ScriptedModel) -> dict:
    return json.loads(model.seen[-1][-1].content)


def test_tool_call(setup):
    client, model = setup
    model.replies += [tool_call("suggest_coupon", {"total_amount": 150000}, "1"), AIMessage("10%.")]
    response = client.post(
        "/v1/chat/completions",
        json={"model": "sales_assistant", "messages": [{"role": "user", "content": "Coupon?"}]},
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "10%."
    assert last_tool_result(model)["percent"] == 10


def test_actor_waits_for_approval(setup):
    client, model = setup
    headers = {"X-Session-Id": "s1"}
    model.replies += [tool_call("send_coupon", {"customer_id": "c1", "percent": 10}, "c1")]
    first = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "Send Kim a 10% coupon"}]},
    ).json()
    choice = first["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "send_coupon"

    model.replies += [AIMessage("Sent.")]
    second = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "messages": [{"role": "tool", "tool_call_id": "c1", "content": '{"approved": true}'}]
        },
    ).json()
    assert second["choices"][0]["message"]["content"] == "Sent."
    assert last_tool_result(model)["coupon"] == "SAVE10"

    session = client.get("/v1/sessions/s1").json()
    assert [m["role"] for m in session["messages"]] == ["human", "ai", "tool", "ai"]


def test_invoke_endpoint(setup):
    client, model = setup
    model.replies += [AIMessage("42")]
    response = client.post("/v1/agents/sales_assistant/invoke", json={"inputs": {"q": "x"}})
    assert response.json() == {"output": "42"}
