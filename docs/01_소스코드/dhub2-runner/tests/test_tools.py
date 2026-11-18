"""The builtin tools: dataset search and text-to-SQL over the collection's Delta tables."""

import json
from pathlib import Path

import polars as pl
import pytest
from agent import runtime
from commons.collection import Collection
from commons.scripts import Resources
from commons.storage import Storage
from langchain_core.messages import AIMessage
from test_agent import ScriptedModel, tool_call
from test_resources import write
from test_s3 import s3_server  # noqa: F401  (fixture)

TOOLS = ["resources_search", "analytics_source", "analytics_query"]


def make_collection(root: Path, tools: list) -> Collection:
    write(
        root,
        "datasets/orders.json",
        {
            "name": "orders",
            "id": "dataset.orders",
            "type": "delta",
            "description": "One row per order",
            "schema": {
                "fields": [{"name": "amount", "type": {"name": "int64"}, "description": "KRW"}]
            },
        },
    )
    write(root, "datasets/cities.json", {"name": "cities", "type": "delta"})
    write(root, "datasets/empty.json", {"name": "empty", "type": "delta"})
    write(root, "llm/m.json", {"name": "m", "provider": "openai", "model": "x", "base_url": "u"})
    write(
        root,
        "agents/analyst.json",
        {"name": "analyst", "agent": {"model": "m", "tools": tools, "current_date": False}},
    )
    return Collection(root)


def fill(storage: Storage) -> None:
    storage.write(
        "orders",
        pl.DataFrame({"city": ["seoul", "busan", "seoul"], "amount": [100, 200, 300]}),
        "append",
    )
    storage.write("cities", pl.DataFrame({"city": ["seoul", "busan"], "pop": [9, 3]}), "append")


@pytest.fixture
def agent(tmp_path, monkeypatch):
    model = ScriptedModel(replies=[])
    monkeypatch.setattr(runtime, "create_chat_model", lambda spec, **kw: model)
    collection = make_collection(tmp_path / "collection", TOOLS)
    storage = Storage(tmp_path / "tables")
    fill(storage)
    return runtime.Agent(collection, Resources(collection), name="analyst", storage=storage)


def call(agent, name, **args):
    return agent.tools[name].call(args, {})


def test_search_and_source(agent):
    found = call(agent, "resources_search", kind="dataset")
    assert [(i["name"], i["has_data"]) for i in found["items"]] == [
        ("cities", True),
        ("empty", False),
        ("orders", True),
    ]
    assert call(agent, "resources_search", name_filter="ORD")["counts"] == {"dataset": 1}
    found = call(agent, "resources_search", name_filter="per order")  # descriptions match too
    assert [i["name"] for i in found["items"]] == ["orders"]
    with pytest.raises(ValueError, match="datasets only"):
        call(agent, "resources_search", kind="pipeline")

    source = call(agent, "analytics_source", kind="dataset", source_id="dataset.orders")
    assert source["qualified_table"] == '"orders"'
    assert source["columns"] == [
        {"name": "city", "type": "String"},
        {"name": "amount", "type": "Int64", "description": "KRW"},
    ]
    with pytest.raises(ValueError, match="no data yet"):
        call(agent, "analytics_source", source_id="empty")
    with pytest.raises(ValueError, match="not found"):
        call(agent, "analytics_source", source_id="nope")


def test_query(agent):
    result = call(
        agent,
        "analytics_query",
        kind="dataset",
        source_id="orders",
        query='SELECT o.city, SUM(o.amount) AS total, MAX(c.pop) AS pop FROM "orders" o '
        "JOIN cities c ON o.city = c.city GROUP BY o.city ORDER BY total DESC",
    )
    assert result["rows"] == [
        {"city": "seoul", "total": 400, "pop": 9},
        {"city": "busan", "total": 200, "pop": 3},
    ]
    assert (result["row_count"], result["truncated"]) == (2, False)
    assert sorted(result["tables"]) == ["cities", "orders"]

    limited = call(agent, "analytics_query", query="SELECT * FROM orders", max_rows=2)
    assert (limited["row_count"], limited["truncated"]) == (2, True)


@pytest.mark.parametrize(
    "query",
    ["DROP TABLE orders", "INSERT INTO orders VALUES ('x', 1)", "SELECT 1; SELECT 2", ""],
)
def test_query_refuses_anything_but_one_select(agent, query):
    with pytest.raises(ValueError):
        call(agent, "analytics_query", query=query)
    assert call(agent, "analytics_query", query="SELECT COUNT(*) AS n FROM orders")["rows"] == [
        {"n": 3}
    ]


def test_unknown_table_lists_the_readable_ones(agent):
    with pytest.raises(ValueError, match=r"tables with data: \['cities', 'orders'\]"):
        call(agent, "analytics_query", query="SELECT * FROM empty")


def test_text2sql_turn(agent):
    """The model resolves the dataset, writes SQL and answers from the rows."""
    model = agent.model
    model.replies += [
        tool_call("analytics_source", {"kind": "dataset", "source_id": "orders"}, "1"),
        tool_call("analytics_query", {"query": "SELECT SUM(amount) AS total FROM orders"}, "2"),
        AIMessage("The total is 600."),
    ]
    result = agent.chat(agent.new_session(), [{"role": "user", "content": "Total sales?"}])
    assert result["content"] == "The total is 600."
    assert json.loads(model.seen[-1][-1].content)["rows"] == [{"total": 600}]


def test_dhub2_builtin_refs_and_settings(tmp_path, monkeypatch):
    """An exported agent refers to the builtins by id (agent.tool.<name>), maybe
    with dhub2's own manifest of the builtin in the collection; its settings apply."""
    monkeypatch.setattr(runtime, "create_chat_model", lambda spec, **kw: ScriptedModel(replies=[]))
    root = tmp_path / "collection"
    collection = make_collection(
        root,
        [
            {"id": "agent.tool.analytics_source", "name": "analytics_source"},
            {
                "id": "agent.tool.analytics_query",
                "name": "analytics_query",
                "settings": {"guidance": "Sales are in KRW.", "fixed_inputs": {"max_rows": 1}},
            },
        ],
    )
    write(
        root,
        "tools/analytics_source.json",
        {
            "id": "agent.tool.analytics_source",
            "name": "analytics_source",
            "is_builtin": True,
            "function": {"source": '@use_manager_read_api("manager")\ndef execute(**kw): ...'},
        },
    )
    collection = Collection(root)
    storage = Storage(tmp_path / "tables")
    fill(storage)
    agent = runtime.Agent(collection, Resources(collection), name="analyst", storage=storage)
    query = agent.tools["analytics_query"]
    assert query.description.endswith("Sales are in KRW.")
    assert "max_rows" not in query.parameters["properties"]
    assert call(agent, "analytics_query", query="SELECT * FROM orders")["truncated"] is True
    assert call(agent, "analytics_source", source_id="orders")["name"] == "orders"

    with pytest.raises(runtime.AgentError, match="needs the collection's tables"):
        runtime.Agent(collection, Resources(collection), name="analyst")


def test_collection_tool_of_the_same_name_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "create_chat_model", lambda spec, **kw: ScriptedModel(replies=[]))
    root = tmp_path / "collection"
    make_collection(root, ["analytics_query"])
    write(
        root,
        "tools/analytics_query.json",
        {
            "id": "agent.tool.42",
            "name": "analytics_query",
            "function": {"source": "def execute(**kw):\n    return 'mine'\n"},
        },
    )
    collection = Collection(root)
    agent = runtime.Agent(collection, Resources(collection), name="analyst")
    assert call(agent, "analytics_query") == "mine"


def test_query_tables_on_s3(s3_server, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(runtime, "create_chat_model", lambda spec, **kw: ScriptedModel(replies=[]))
    collection = make_collection(tmp_path / "collection", TOOLS)
    storage = Storage("s3://runner/tables/demo")
    fill(storage)
    agent = runtime.Agent(collection, Resources(collection), name="analyst", storage=storage)
    result = call(agent, "analytics_query", query="SELECT city FROM cities ORDER BY pop")
    assert result["rows"] == [{"city": "busan"}, {"city": "seoul"}]
