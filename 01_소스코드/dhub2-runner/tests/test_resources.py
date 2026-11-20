"""Each dhub2 resource on its own, in the JSON dhub2 returns for it:
connectors (script and builtin drivers), LLM models, datasets, prompts, tools
and secrets."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import polars as pl
import pytest
from agent import runtime
from commons.collection import Collection, CollectionError
from commons.llm import resolve_llm
from commons.scripts import Resources, ScriptError
from commons.state import State
from commons.storage import Storage
from conftest import MemorySecretStore, REST_VERBS
from pipeline.runner import PipelineError, PipelineRunner
from test_agent import ScriptedModel


def write(root: Path, path: str, doc) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(doc if isinstance(doc, str) else json.dumps(doc))


class Stub:
    """A local HTTP server answering every request with ``reply(request)``."""

    def __init__(self, reply):
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def handle_one(self):
                length = int(self.headers.get("content-length") or 0)
                request = {
                    "method": self.command,
                    "path": urlparse(self.path).path,
                    "query": {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()},
                    "headers": dict(self.headers),
                    "body": self.rfile.read(length) if length else b"",
                }
                stub.requests.append(request)
                status, kind, body = reply(request)
                self.send_response(status)
                self.send_header("content-type", kind)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_one

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


def json_reply(payload, status=200):
    return lambda request: (status, "application/json", json.dumps(payload).encode())


# -- connectors ----------------------------------------------------------------


def test_http_driver_with_secret_credentials(tmp_path):
    stub = Stub(json_reply({"ok": True}))
    try:
        write(
            tmp_path,
            "connectors/api.json",
            {
                "category": "connector",
                "name": "api",
                "type": "rest",
                "driver": "http",
                "options": {
                    "base_url": stub.url + "/v1",
                    "auth_type": "api_key",
                    "auth_in": "query",
                    "auth_name": "serviceKey",
                    # As dhub2 stores a connector credential: a ref to its owner secret.
                    "api_key_ref": "secret://connector.api/api_key",
                    "headers": {"X-Team": "data"},
                },
            },
        )
        secrets = MemorySecretStore({"connector.api": {"api_key": "k3y"}})
        api = Resources(Collection(tmp_path, secrets)).connector("api")
        assert api.get("items", {"page": 2}) == {"ok": True}
        assert api.post("items", {"a": 1}) == {"ok": True}
        api.close()
        first, second = stub.requests
        assert first["path"] == "/v1/items"
        assert first["query"] == {"serviceKey": "k3y", "page": "2"}
        assert first["headers"]["X-Team"] == "data"
        assert json.loads(second["body"]) == {"a": 1}
    finally:
        stub.close()


def test_http_driver_bearer_and_errors(tmp_path):
    stub = Stub(
        lambda r: (
            (404, "text/plain", b"nope") if r["path"] == "/missing" else (200, "text/plain", b"hi")
        )
    )
    try:
        write(
            tmp_path,
            "connectors/api.json",
            {
                "name": "api",
                "type": "rest",
                "driver": "http",
                "options": {
                    "base_url": stub.url,
                    "auth_type": "bearer",
                    "token_ref": "secret://api/token",
                },
            },
        )
        api = Resources(Collection(tmp_path, MemorySecretStore({"api": {"token": "t"}}))).connector(
            "api"
        )
        assert api.get("hello") == "hi"
        assert stub.requests[0]["headers"]["Authorization"] == "Bearer t"
        with pytest.raises(RuntimeError, match="HTTP 404"):
            api.get("missing")
    finally:
        stub.close()


def test_postgres_driver(tmp_path, postgres_url):
    port = urlparse(postgres_url).port
    write(
        tmp_path,
        "connectors/pg.json",
        {
            "name": "pg",
            "type": "db",
            "driver": "postgres",
            "options": {
                "host": "127.0.0.1",
                "port": port,
                "database": "postgres",
                "username": "runner",
                "password_ref": "secret://pg/password",
                "sslmode": "disable",
            },
        },
    )
    pg = Resources(
        Collection(tmp_path, MemorySecretStore({"pg": {"password": "unused"}}))
    ).connector("pg")
    pg.execute("CREATE TABLE IF NOT EXISTS t (id int, name text)")
    assert pg.execute("INSERT INTO t VALUES (%s, %s), (%s, %s)", [1, "a", 2, "b"]) == 2
    assert pg.query("SELECT * FROM t WHERE id > %s ORDER BY id", [0]).to_dicts() == [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
    ]
    pg.execute("DROP TABLE t")
    pg.close()


def test_drivers_that_cannot_run_here(tmp_path):
    write(
        tmp_path,
        "connectors/m.json",
        {"name": "m", "type": "rest", "driver": "dhub2_manager_api", "options": {}},
    )
    write(
        tmp_path, "connectors/x.json", {"name": "x", "type": "rest", "driver": "ftp", "options": {}}
    )
    resources = Resources(Collection(tmp_path))
    with pytest.raises(ScriptError, match="calls the dhub2 API"):
        resources.connector("m")
    for driver in ("clickhouse", "qdrant"):
        write(
            tmp_path,
            f"connectors/{driver}.json",
            {"name": driver, "type": "db", "driver": driver, "options": {}},
        )
        with pytest.raises(ScriptError, match="is not supported"):
            Resources(Collection(tmp_path)).connector(driver)
    with pytest.raises(ScriptError, match="available: http, postgres"):
        resources.connector("x")


def test_script_connector_defines_its_type_functions(tmp_path):
    connect = "def connect(options, collection=None):\n    return options\n"
    get = "def get(client, path, params=None):\n    return path\n"
    write(tmp_path, "connectors/r.json", {"name": "r", "type": "rest", "script": connect + get})
    write(tmp_path, "connectors/u.json", {"name": "u", "type": "ftp", "script": connect})
    write(
        tmp_path,
        "connectors/ok.json",
        {"name": "ok", "type": "rest", "script": connect + get + REST_VERBS},
    )
    resources = Resources(Collection(tmp_path))
    with pytest.raises(ScriptError, match=r"does not define \['post', 'put', 'delete', 'close'\]"):
        resources.connector("r")
    with pytest.raises(ScriptError, match="unknown connector type 'ftp'"):
        resources.connector("u")
    assert resources.connector("ok").get("/a") == "/a"


def test_mcp_is_not_supported(tmp_path, monkeypatch):
    write(
        tmp_path,
        "connectors/docs.json",
        {
            "name": "docs",
            "type": "mcp",
            "driver": "mcp",
            "options": {"url": "http://127.0.0.1:9/mcp"},
        },
    )
    write(
        tmp_path,
        "llm/m.json",
        {"name": "m", "provider": "openai", "model": "x", "base_url": "http://127.0.0.1:9/v1"},
    )
    tool = {
        "name": "search_docs",
        "type": "mcp",
        "ref": "docs",
        "options": {"mcp": {"tool": "search"}},
    }
    write(tmp_path, "tools/search_docs.json", tool)
    write(
        tmp_path,
        "agents/a.json",
        {
            "name": "a",
            "mode": "react",
            "agent": {
                "instructions": "x",
                "model_provider": "openai",
                "model": "m",
                "tools": ["search_docs"],
            },
        },
    )
    collection = Collection(tmp_path)
    with pytest.raises(ScriptError, match="MCP is not supported"):
        Resources(collection).connector("docs")
    monkeypatch.setattr(runtime, "create_chat_model", lambda spec, **kw: ScriptedModel(replies=[]))
    with pytest.raises(runtime.AgentError, match="mcp tool 'search_docs' is not supported"):
        runtime.Agent(collection, Resources(collection), name="a")


# -- LLM models ----------------------------------------------------------------


def test_llm_model_as_dhub2_returns_it(tmp_path):
    # dhub2 never returns the key; the runner reads it from a secret named after the model.
    write(
        tmp_path,
        "llm/e2e_llm.json",
        {
            "category": "llm.model",
            "name": "e2e_llm",
            "type": "external",
            "provider": "claude",
            "model": "claude-x",
            "base_url": "https://api.anthropic.com",
            "has_api_key": True,
            "api_key_ref": "secret://e2e_llm/api_key",
        },
    )
    write(
        tmp_path,
        "llm/hosted.json",
        {
            "category": "llm.model",
            "name": "hosted",
            "type": "internal",
            "provider": "openai",
            "model": "gemma",
            "deployment": "gemma-4",
        },
    )
    secrets = MemorySecretStore({"e2e_llm": {"api_key": "k"}})
    collection = Collection(tmp_path, secrets)
    assert resolve_llm(collection, "e2e_llm")["api_key"] == "k"
    assert ("hosted", "base_url") in collection.secret_keys()
    with pytest.raises(CollectionError, match="set the secret hosted/base_url"):
        resolve_llm(collection, "hosted")
    # Where a model is served can be set in the runner without changing the collection.
    secrets.set("hosted", "base_url", "http://gemma.local/v1")
    secrets.set("e2e_llm", "base_url", "http://proxy.local")
    assert resolve_llm(collection, "hosted")["base_url"] == "http://gemma.local/v1"
    assert resolve_llm(collection, "e2e_llm")["base_url"] == "http://proxy.local"


# -- datasets ------------------------------------------------------------------


def test_dataset_schema_types_the_seed_data(tmp_path):
    write(
        tmp_path,
        "datasets/orders.json",
        {
            "category": "dataset",
            "name": "orders",
            "type": "delta",
            "schema": {
                "fields": [
                    {"name": "id", "type": {"name": "int32"}, "nullable": False},
                    {"name": "amount", "type": {"name": "float64"}},
                    {"name": "day", "type": {"name": "date32"}},
                    {"name": "at", "type": {"name": "timestamp", "unit": "us"}},
                    {"name": "code", "type": {"name": "utf8"}},
                ]
            },
        },
    )
    write(
        tmp_path,
        "data/orders.csv",
        "id,amount,day,at,code\n1,10,2026-10-01,2026-10-01 10:00:00,007\n",
    )
    write(
        tmp_path,
        "pipelines/p.json",
        {
            "name": "p",
            "steps": [
                {
                    "name": "s",
                    "transform": {
                        "op": "stages",
                        "config": {
                            "input": "o",
                            "pipeline": [{"op": "select", "columns": ["id", "code"]}],
                        },
                    },
                    "inputs": {"o": {"dataset": "orders", "options": {"read_mode": "snapshot"}}},
                    "outputs": {"x": {"dataset": "x"}},
                }
            ],
        },
    )
    runner = PipelineRunner(Collection(tmp_path), Storage(tmp_path / "t"), State(tmp_path))
    runner.run("p")
    schema = runner.storage.read("orders").schema
    assert schema["id"] == pl.Int32 and schema["amount"] == pl.Float64
    assert schema["day"] == pl.Date and schema["at"] == pl.Datetime("us")
    assert runner.storage.read("x")["code"].to_list() == ["007"]


def test_non_delta_datasets_are_refused(tmp_path):
    write(tmp_path, "datasets/files.json", {"name": "files", "type": "object"})
    write(tmp_path, "codes/c.json", {"name": "c", "type": "python"})
    write(tmp_path, "codes/c.py", "def run(files, options=None, contexts=None):\n    return {}\n")
    write(
        tmp_path,
        "pipelines/p.json",
        {
            "name": "p",
            "steps": [{"name": "s", "script": "c", "inputs": {"files": {"dataset": "files"}}}],
        },
    )
    with pytest.raises(PipelineError, match="'object' dataset; only delta"):
        PipelineRunner(Collection(tmp_path), Storage(tmp_path / "t"), State(tmp_path)).load("p")


# -- prompts and secrets -------------------------------------------------------


def test_prompt_and_secret_resources(tmp_path, monkeypatch):
    write(
        tmp_path,
        "prompts/tone.json",
        {
            "category": "prompt",
            "name": "tone",
            "type": "system",
            "content": "Answer in {{language}}.",
            "variables": [{"name": "language", "default": "Korean"}],
        },
    )
    # dhub2 keeps only a secret's key names; the values are in the runner's store.
    write(
        tmp_path,
        "secrets/crm.json",
        {"category": "secret", "id": "secret.9", "name": "crm", "keys": ["token", "user"]},
    )
    write(
        tmp_path,
        "llm/m.json",
        {"name": "m", "provider": "openai", "model": "x", "base_url": "http://127.0.0.1:9/v1"},
    )
    write(
        tmp_path,
        "agents/a.json",
        {
            "name": "a",
            "mode": "react",
            "agent": {
                "instructions": "Be brief.",
                "model_provider": "openai",
                "model": "m",
                "prompts": [{"prompt_id": "tone", "variables": {}}],
                "current_date": False,
            },
        },
    )
    collection = Collection(tmp_path)
    assert collection.secret_keys() == [("crm", "token"), ("crm", "user")]
    monkeypatch.setattr(runtime, "create_chat_model", lambda spec, **kw: ScriptedModel(replies=[]))
    agent = runtime.Agent(collection, Resources(collection), name="a")
    assert agent.system_prompt(agent.new_session()) == "Answer in Korean.\n\nBe brief."


def test_use_connectors_resolver(tmp_path):
    write(
        tmp_path,
        "connectors/echo.json",
        {
            "name": "echo",
            "type": "rest",
            "script": "def connect(options, collection=None):\n    return options\n"
            "def get(client, path):\n    return client['prefix'] + path\n" + REST_VERBS,
            "options": {"prefix": ">"},
        },
    )
    write(tmp_path, "codes/c.json", {"name": "c", "type": "python"})
    write(
        tmp_path,
        "codes/c.py",
        "import polars as pl\n"
        "@use_connectors('connectors')\n"
        "def run(options=None, contexts=None):\n"
        "    return {'out': pl.DataFrame({'v': [connectors.get('echo').get('x')]})}\n",
    )
    write(
        tmp_path,
        "pipelines/p.json",
        {
            "name": "p",
            "steps": [{"name": "s", "script": "c", "outputs": {"out": {"dataset": "o"}}}],
        },
    )
    runner = PipelineRunner(Collection(tmp_path), Storage(tmp_path / "t"), State(tmp_path))
    runner.run("p")
    assert runner.storage.read("o")["v"].to_list() == [">x"]
