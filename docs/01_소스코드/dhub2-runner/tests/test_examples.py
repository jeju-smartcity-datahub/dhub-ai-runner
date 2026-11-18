import json
import shutil
from pathlib import Path

import pytest
from agent import runtime
from commons import scripts
from commons.collection import Collection
from commons.state import State
from commons.storage import Storage
from conftest import example_secrets
from langchain_core.messages import AIMessage
from pipeline import runner as runner_module
from pipeline.runner import PipelineError, PipelineRunner
from test_agent import ScriptedModel, tool_call

EXAMPLES = Path(__file__).parent.parent / "examples"


def make_runner(name: str, data: Path) -> PipelineRunner:
    return PipelineRunner(
        Collection(EXAMPLES / name, example_secrets()), Storage(data), State(data)
    )


@pytest.mark.parametrize("name", ["quickstart", "pipelines", "agents", "city-monitor"])
def test_examples_load(name, tmp_path, monkeypatch):
    collection = Collection(EXAMPLES / name, example_secrets())
    runner = make_runner(name, tmp_path)
    for doc in collection.list("pipeline"):
        runner.load(doc["name"])
    resources = scripts.Resources(collection)
    for doc in collection.list("agent"):
        runtime.Agent(collection, resources, name=doc["name"], storage=runner.storage)


def test_sample_pipelines(tmp_path):
    runner = make_runner("pipelines", tmp_path)
    runner.run("batch")
    runner.run("batch")  # CDC input: nothing new the second time
    masked = runner.storage.read("people_masked")
    assert masked.columns == ["name"]
    assert sorted(masked["name"].to_list()) == ["Al***", "Bo***", "Ch***", "Di***", "Ed***"]

    runner.run("variable")
    assert set(runner.storage.read("people_summary")["total_rows"]) == {5}


def test_transform_sample(tmp_path):
    runner = make_runner("pipelines", tmp_path)
    runner.run("transforms")
    by_region = {
        r["region_name"]: r["amount"] for r in runner.storage.read("sales_by_region").to_dicts()
    }
    assert by_region == {"서울": 467000, "부산": 735000, "인천": 45000, "대전": 180000}
    assert runner.storage.read("sales_all").height == 9
    masked = runner.storage.read("sales_masked").sort("order_id")
    assert masked["order_id"].to_list() == [1003, 1005, 1006]
    assert masked["phone"].to_list()[0] == "010-****-7890"
    assert masked["email"].to_list()[0] == "pa**@example.com"


def test_llm_transform_sample(tmp_path, monkeypatch):
    model = ScriptedModel(
        replies=[AIMessage(a) for a in ["Positive", "negative", "neutral", "bad"]]
    )
    monkeypatch.setattr(runner_module, "create_chat_model", lambda spec, **kw: model)
    runner = make_runner("pipelines", tmp_path)
    runner.run("llm_review")
    labeled = runner.storage.read("reviews_labeled").sort("review_id")
    assert labeled["sentiment"].to_list() == ["positive", "negative", "neutral", None]
    assert "not one of" in labeled["sentiment_error"].to_list()[3]


def test_llm_step_sample(tmp_path, monkeypatch):
    answer = {
        "summary": "결제와 환불이 느리다.",
        "sentiment": "neutral",
        "top_issues": ["결제", "환불"],
    }
    model = ScriptedModel(
        replies=[AIMessage(f"요약입니다.\n{json.dumps(answer, ensure_ascii=False)}")]
    )
    monkeypatch.setattr(scripts, "create_chat_model", lambda spec, **kw: model)
    make_runner("agents", tmp_path).run("feedback_summary")
    saved = Storage(tmp_path).read("feedback_summary").to_dicts()
    assert saved[0]["sentiment"] == "neutral"
    assert saved[0]["top_issues"] == "결제, 환불"
    assert len(json.loads(model.seen[0][-1].content)) == 6


def test_agent_steps_need_a_workflow_agent(tmp_path):
    root = tmp_path / "collection"
    shutil.copytree(EXAMPLES / "agents", root)
    path = root / "pipelines" / "feedback_summary.json"
    doc = json.loads(path.read_text())
    doc["steps"][1].pop("script")
    doc["steps"][1]["agent"] = {"version": 1, "agent_id": "customer-support"}
    path.write_text(json.dumps(doc))
    runner = PipelineRunner(Collection(root, example_secrets()), Storage(tmp_path), State(tmp_path))
    with pytest.raises(PipelineError, match="is mode 'react'; only workflow agents"):
        runner.load("feedback_summary")


def test_tool_decorator_binding_overrides_source(tmp_path, monkeypatch):
    calls = []
    tool_model = ScriptedModel(replies=[AIMessage("짧은 요약")])
    agent_model = ScriptedModel(
        replies=[tool_call("summarize_text", {"text": "긴 글"}, "1"), AIMessage("요약했습니다.")]
    )
    monkeypatch.setattr(runtime, "create_chat_model", lambda spec, **kw: agent_model)

    def tool_llm(spec, **kw):
        calls.append(kw)
        return tool_model

    monkeypatch.setattr(scripts, "create_chat_model", tool_llm)
    collection = Collection(EXAMPLES / "agents", example_secrets())
    agent = runtime.Agent(collection, scripts.Resources(collection), name="knowledge-assistant")
    assert agent.ask("요약해줘") == "요약했습니다."
    # function.decorators sets temperature 0.1 over the source's 0.3
    assert calls == [{"temperature": 0.1}]
    assert json.loads(agent_model.seen[-1][-1].content) == {"summary": "짧은 요약"}
