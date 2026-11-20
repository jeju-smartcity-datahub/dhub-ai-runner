"""The web app's API over a workspace of collections."""

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent import runtime
from conftest import EXAMPLE_SECRETS
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from test_agent import ScriptedModel, tool_call
from test_importer import export_zip
from web.app import create_app
from web.runs import Scheduler, ThreadExecutor, wait_for
from web.workspace import Workspace

EXAMPLES = Path(__file__).parent.parent / "examples"


def zip_dir(path: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for file in path.rglob("*"):
            if file.is_file():
                archive.write(file, file.relative_to(path))
    return buffer.getvalue()


@pytest.fixture
def web(tmp_path, monkeypatch):
    workspace = Workspace(
        str(tmp_path / "collections"), str(tmp_path / "tables"), tmp_path / "data"
    )
    executor = ThreadExecutor(workspace, max_runs=2)
    with TestClient(create_app(workspace, executor, schedule=False)) as client:
        # The examples' model endpoint, as a shared secret.
        values = {f"{s}/{k}": v for s, keys in EXAMPLE_SECRETS.items() for k, v in keys.items()}
        client.put("/api/secrets", json={"values": values})
        yield client, executor


def upload(client, name, files, replace=False):
    return client.post(
        "/api/collections",
        data={"name": name, "replace": str(replace).lower()},
        files=[("files", (n, content)) for n, content in files],
    )


def test_import_run_and_browse(web):
    client, executor = web
    response = upload(client, "qs", [("quickstart.zip", zip_dir(EXAMPLES / "quickstart"))])
    assert response.status_code == 200, response.text
    assert response.json()["imported"]["pipeline"] == ["orders"]
    assert [b["name"] for b in client.get("/api/collections").json()] == ["qs"]

    # A second ZIP under the same name needs replace.
    assert upload(client, "qs", [("q.zip", zip_dir(EXAMPLES / "quickstart"))]).status_code == 409

    summary = client.get("/api/collections/qs").json()
    assert summary["pipelines"][0]["problems"] == []
    assert summary["agents"][0]["problems"] == []

    run_id = client.post("/api/collections/qs/pipelines/orders/runs").json()["run_id"]
    wait_for(executor, "qs", run_id)
    detail = client.get("/api/collections/qs/pipelines/orders").json()
    assert detail["runs"][0] == {**detail["runs"][0], "id": run_id, "state": "ready"}
    assert {s["checkpoint"]["state"] for s in detail["steps"]} == {"ready"}

    rows = client.get("/api/collections/qs/datasets/orders_raw/rows?limit=5").json()
    assert rows["total"] == 20 and len(rows["rows"]) == 5
    assert [c["name"] for c in rows["columns"]][:2] == ["order_id", "customer_id"]

    assert client.delete("/api/collections/qs").json() == {"deleted": "qs"}
    assert client.get("/api/collections").json() == []


def test_chat_with_approval(web, monkeypatch):
    client, _ = web
    model = ScriptedModel(
        replies=[
            tool_call("send_coupon", {"customer_id": "c1", "percent": 10}, "a1"),
            AIMessage("Sent."),
        ]
    )
    monkeypatch.setattr(runtime, "create_chat_model", lambda spec, **kw: model)
    upload(client, "qs", [("quickstart.zip", zip_dir(EXAMPLES / "quickstart"))])

    first = client.post(
        "/api/collections/qs/agents/sales_assistant/chat",
        json={"messages": [{"role": "user", "content": "send c1 a coupon"}]},
    ).json()
    assert first["pending"][0]["name"] == "send_coupon"
    second = client.post(
        "/api/collections/qs/agents/sales_assistant/chat",
        json={
            "session_id": first["session_id"],
            "messages": [{"role": "tool", "tool_call_id": "a1", "content": '{"approved": true}'}],
        },
    ).json()
    assert second["content"] == "Sent."
    assert second["events"][0] == {
        "type": "tool",
        "name": "send_coupon",
        "args": {"customer_id": "c1", "percent": 10},
    }
    session = client.get(
        f"/api/collections/qs/agents/sales_assistant/sessions/{first['session_id']}"
    ).json()
    assert [m["role"] for m in session["messages"]] == ["human", "ai", "tool", "ai"]
    assert session["messages"][2]["tool_call_id"] == "a1"


def sse(response) -> list[dict]:
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


def test_chat_stream(web, monkeypatch):
    """The chatbot's stream: tool events as they happen, then the answer."""
    client, _ = web
    model = ScriptedModel(
        replies=[
            tool_call("suggest_coupon", {"total_amount": 150000}, "t1"),
            tool_call("suggest_coupon", {}, "t2"),  # a missing argument fails
            AIMessage("10%."),
        ]
    )
    monkeypatch.setattr(runtime, "create_chat_model", lambda spec, **kw: model)
    upload(client, "qs", [("quickstart.zip", zip_dir(EXAMPLES / "quickstart"))])

    response = client.post(
        "/api/collections/qs/agents/sales_assistant/chat/stream",
        json={"session_id": "s1", "messages": [{"role": "user", "content": "Coupon?"}]},
    )
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse(response)
    assert [e["type"] for e in events] == ["tool", "tool_result", "tool", "tool_result", "done"]
    assert "error" not in events[1] and "total_amount" in events[3]["error"]
    assert events[-1] == {"type": "done", "content": "10%.", "pending": [], "session_id": "s1"}

    missing = client.post("/api/collections/qs/agents/nobody/chat/stream", json={"messages": []})
    assert missing.status_code == 404
    monkeypatch.setattr(model, "replies", [])  # the model fails mid-turn
    failed = sse(client.post("/api/collections/qs/agents/sales_assistant/chat/stream", json={}))
    assert failed[-1]["type"] == "error" and failed[-1]["status"] == 502


def test_chatbot_page(web):
    client, _ = web
    assert "D.Hub for AI 챗봇" in client.get("/chat").text
    assert client.get("/static/chat.js").status_code == 200


def test_manage_pipelines_and_agents(web, monkeypatch):
    client, executor = web
    monkeypatch.setattr(
        runtime, "create_chat_model", lambda spec, **kw: ScriptedModel(replies=[AIMessage("hi")])
    )
    upload(client, "qs", [("quickstart.zip", zip_dir(EXAMPLES / "quickstart"))])

    # Pausing a pipeline skips its schedule; it can still be run by hand.
    assert client.post("/api/collections/qs/pipelines/orders/pause").json() == {"paused": True}
    assert client.get("/api/collections/qs/pipelines/orders").json()["paused"] is True
    scheduler = Scheduler(executor.workspace, executor)
    due = ("qs", "orders", "*/10 * * * *")
    scheduler._next[due] = datetime(2000, 1, 1, tzinfo=UTC)
    assert scheduler.fire_due(datetime.now(UTC)) == []
    client.post("/api/collections/qs/pipelines/orders/resume")
    scheduler._next[due] = datetime(2000, 1, 1, tzinfo=UTC)
    assert scheduler.fire_due(datetime.now(UTC)) == [("qs", "orders")]
    wait_for(executor, "qs", executor.active("qs").get("orders", ""))
    ops = client.get("/api/operations").json()
    assert ops[0]["pipelines"][0] == {**ops[0]["pipelines"][0], "name": "orders", "paused": False}

    # A stopped agent refuses chats until it is started again.
    assert ops[0]["agents"][0] == {**ops[0]["agents"][0], "status": "running", "sessions": 0}
    assert client.post("/api/collections/qs/agents/sales_assistant/stop").json() == {
        "status": "stopped"
    }
    chat = {"session_id": "s1", "messages": [{"role": "user", "content": "hello"}]}
    refused = client.post("/api/collections/qs/agents/sales_assistant/chat", json=chat)
    assert refused.status_code == 409 and "stopped" in refused.json()["detail"]
    assert client.get("/api/collections/qs").json()["agents"][0]["status"] == "stopped"
    assert client.post("/api/collections/qs/agents/sales_assistant/start").json() == {
        "status": "running"
    }
    assert (
        client.post("/api/collections/qs/agents/sales_assistant/chat", json=chat).json()["content"]
        == "hi"
    )
    detail = client.get("/api/collections/qs/agents/sales_assistant").json()
    assert detail["sessions"][0] == {
        **detail["sessions"][0],
        "session_id": "s1",
        "messages": 2,
        "title": "hello",
    }


def test_collection_export_needs_its_secret(web, tmp_path):
    client, executor = web
    export = export_zip(tmp_path)
    assert upload(client, "demo", [("demo-export.zip", export.read_bytes())]).status_code == 200

    secrets = client.get("/api/collections/demo/secrets").json()
    assert secrets == [
        {"secret": "assistant-llm", "key": "api_key", "source": None},
        {"secret": "crm", "key": "token", "source": None},
    ]

    run_id = client.post("/api/collections/demo/pipelines/daily/runs").json()["run_id"]
    wait_for(executor, "demo", run_id)
    run = client.get("/api/collections/demo/pipelines/daily").json()["runs"][0]
    assert run["state"] == "error" and "crm/token is not set" in run["error"]

    client.put("/api/collections/demo/secrets", json={"values": {"crm/token": "t0k"}})
    assert client.get("/api/collections/demo/secrets").json()[1]["source"] == "settings"
    run_id = client.post("/api/collections/demo/pipelines/daily/runs").json()["run_id"]
    wait_for(executor, "demo", run_id)
    rows = client.get("/api/collections/demo/datasets/region_totals/rows").json()
    assert sorted(rows["rows"]) == [["BSN", 20, "t0k"], ["SEL", 40, "t0k"]]
    # Values are never sent back, and are not stored in plain text.
    assert "t0k" not in client.get("/api/collections/demo/secrets").text
    stored = (tmp_path / "data" / "state" / "demo" / "secrets.enc").read_bytes()
    assert b"t0k" not in stored

    # Deleting a secret key brings the error back.
    client.put("/api/collections/demo/secrets", json={"values": {"crm/token": None}})
    assert client.get("/api/collections/demo/secrets").json()[1]["source"] is None


def test_add_single_resources(web):
    client, executor = web
    files = [
        (
            "hello.json",
            json.dumps({"category": "code", "name": "hello", "type": "python"}).encode(),
        ),
        (
            "hello.py",
            b"import polars as pl\ndef run(options=None, contexts=None):\n"
            b"    return {'out': pl.DataFrame({'greeting': ['hi']})}\n",
        ),
        (
            "greet.json",
            json.dumps(
                {
                    "category": "pipeline",
                    "name": "greet",
                    "type": "batch",
                    "steps": [
                        {
                            "name": "s",
                            "script": "hello",
                            "outputs": {"out": {"dataset": "greetings"}},
                        }
                    ],
                }
            ).encode(),
        ),
    ]
    response = upload(client, "single", files)
    assert response.status_code == 200, response.text
    run_id = client.post("/api/collections/single/pipelines/greet/runs").json()["run_id"]
    wait_for(executor, "single", run_id)
    assert client.get("/api/collections/single/datasets/greetings/rows").json()["rows"] == [["hi"]]

    # More resources are added to the same collection.
    extra = [("notes.csv", b"id,text\n1,a\n")]
    assert upload(client, "single", extra).status_code == 200
    summary = client.get("/api/collections/single").json()
    assert summary["pipelines"][0]["name"] == "greet"

    bad = upload(client, "single", [("x.json", b'{"name": "x"}')])
    assert bad.status_code == 422 and "category" in bad.json()["detail"]


def test_ui_is_served(web):
    client, _ = web
    assert "dhub-runner" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_secrets_in_postgres(tmp_path, monkeypatch, postgres_url):
    monkeypatch.setenv("RUNNER_DATABASE_URL", postgres_url)
    workspace = Workspace(
        str(tmp_path / "collections"), str(tmp_path / "tables"), tmp_path / "data"
    )
    with TestClient(create_app(workspace, schedule=False)) as client:
        upload(client, "pg", [("demo-export.zip", export_zip(tmp_path).read_bytes())])
        client.put(
            "/api/collections/pg/secrets",
            json={"values": {"crm/token": "t0k", "assistant-llm/api_key": "k"}},
        )
        assert {x["source"] for x in client.get("/api/collections/pg/secrets").json()} == {
            "settings"
        }
        assert not (tmp_path / "data" / "state" / "pg" / "secrets.enc").exists()
        client.delete("/api/collections/pg")
    import psycopg

    with psycopg.connect(postgres_url) as db:
        assert (
            db.execute("SELECT count(*) FROM runner_secrets WHERE collection = 'pg'").fetchone()[0]
            == 0
        )


def test_shared_secrets(web, tmp_path):
    client, executor = web
    upload(client, "demo", [("demo-export.zip", export_zip(tmp_path).read_bytes())])
    response = client.put(
        "/api/secrets",
        json={"values": {"crm/token": "shared-t0k", "assistant-llm/api_key": "k"}},
    ).json()
    assert {(x["secret"], x["key"]) for x in response["shared"]} >= {
        ("crm", "token"),
        ("assistant-llm", "api_key"),
    }
    demo = response["collections"][0]
    assert {x["source"] for x in demo["secrets"]} == {"shared"} and demo["missing"] == []
    assert "shared-t0k" not in json.dumps(response)

    run_id = client.post("/api/collections/demo/pipelines/daily/runs").json()["run_id"]
    wait_for(executor, "demo", run_id)
    rows = client.get("/api/collections/demo/datasets/region_totals/rows").json()["rows"]
    assert {r[2] for r in rows} == {"shared-t0k"}

    # The collection's own value wins over the shared one.
    client.put("/api/collections/demo/secrets", json={"values": {"crm/token": "own"}})
    assert client.get("/api/collections/demo/secrets").json()[1]["source"] == "settings"
    run_id = client.post("/api/collections/demo/pipelines/daily/runs").json()["run_id"]
    wait_for(executor, "demo", run_id)
    rows = client.get("/api/collections/demo/datasets/region_totals/rows").json()["rows"]
    assert {r[2] for r in rows} == {"own"}


def test_api_docs(web):
    client, _ = web
    assert "swagger" in client.get("/api/docs").text.lower()
    spec = client.get("/api/openapi.json").json()
    operations = [op for path in spec["paths"].values() for op in path.values()]
    # Every endpoint is grouped under a documented tag and has a summary.
    tags = {t["name"] for t in spec["tags"]}
    assert all(op["tags"][0] in tags and op["summary"] for op in operations)
    notes = spec["paths"]["/api/collections/{name}/datasets/{dataset}/notes"]["put"]
    assert notes["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "NotesRequest"
    )
    detail = spec["paths"]["/api/collections/{name}/datasets/{dataset}"]["get"]
    assert detail["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "DatasetDetail"
    )
