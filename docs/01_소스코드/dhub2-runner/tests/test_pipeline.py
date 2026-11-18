import json
from pathlib import Path

import polars as pl
import pytest
from commons.collection import Collection
from commons.state import State
from commons.storage import Storage
from conftest import MemorySecretStore, REST_VERBS
from pipeline.runner import PipelineError, PipelineRunner

QUICKSTART = Path(__file__).parent.parent / "examples" / "quickstart"


def make_runner(collection_dir: Path, data_dir: Path, secrets=None) -> PipelineRunner:
    return PipelineRunner(Collection(collection_dir, secrets), Storage(data_dir), State(data_dir))


def write(root: Path, path: str, content) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content if isinstance(content, str) else json.dumps(content))


def test_quickstart_pipeline(tmp_path):
    runner = make_runner(QUICKSTART, tmp_path)
    runner.run("orders")
    runner.run("orders")
    raw = runner.storage.read("orders_raw")
    assert raw.height == 40
    customers = runner.storage.read("customer_totals")
    assert customers["customer_id"].is_unique().all()
    paid = raw.filter(pl.col("status") == "paid")
    assert customers["total_amount"].sum() == paid["amount"].sum()
    assert runner.storage.read("orders_daily")["orders"].sum() == 40


def test_cdc_input_reads_only_new_rows(tmp_path):
    collection = tmp_path / "collection"
    write(collection, "codes/emit.json", {"name": "emit", "type": "python"})
    write(
        collection,
        "codes/emit.py",
        "import polars as pl\n"
        "def run(options=None, contexts=None):\n"
        "    return {'out': pl.DataFrame({'n': [options['n']] * 2})}\n",
    )
    write(collection, "codes/count.json", {"name": "count", "type": "python"})
    write(
        collection,
        "codes/count.py",
        "import polars as pl\n"
        "def run(rows, options=None, contexts=None):\n"
        "    return {'out': pl.DataFrame({'seen': [rows.height]})}\n",
    )
    pipeline = {
        "name": "p",
        "type": "batch",
        "steps": [
            {
                "name": "emit",
                "script": "emit",
                "options": {"n": 1},
                "outputs": {"out": {"dataset": "events"}},
            },
            {
                "name": "count",
                "script": "count",
                "inputs": {"rows": {"dataset": "events"}},
                "outputs": {"out": {"dataset": "counts"}},
            },
        ],
    }
    write(collection, "pipelines/p.json", pipeline)
    runner = make_runner(collection, tmp_path / "data")
    runner.run("p")
    runner.run("p")
    assert runner.storage.read("counts")["seen"].to_list() == [2, 2]
    assert runner.storage.read("events").height == 4


def test_transforms_variables_and_connector(tmp_path):
    collection = tmp_path / "collection"
    write(collection, "secrets/demo.json", {"name": "demo", "keys": ["token"]})
    write(
        collection,
        "connectors/fake_api.json",
        {
            "name": "fake_api",
            "type": "rest",
            "options": {"prefix": "${secret:demo/token}"},
            "script": "def connect(options, collection=None):\n    return options\n"
            "def get(client, path):\n    return client['prefix'] + ':' + path\n" + REST_VERBS,
        },
    )
    write(collection, "codes/left.json", {"name": "left", "type": "python"})
    write(
        collection,
        "codes/left.py",
        "import polars as pl\n"
        "@use_connector('api', ref='fake_api')\n"
        "def run(options=None, contexts=None):\n"
        "    return {'users': pl.DataFrame({'id': [1, 2], 'name': [api.get('a'), api.get('b')]}),\n"
        "            'token': contexts['secrets']['token']}\n",
    )
    write(collection, "codes/check.json", {"name": "check", "type": "python"})
    write(
        collection,
        "codes/check.py",
        "def run(joined, token, options=None, contexts=None):\n"
        "    assert token == 's3cret'\n"
        "    return {}\n",
    )
    pipeline = {
        "name": "p",
        "steps": [
            {
                "name": "left",
                "script": "left",
                "secrets": [{"name": "token", "ref": "secret://demo/token"}],
                "outputs": {"users": {"dataset": "users"}, "token": {}},
            },
            {
                "name": "right",
                "transform": {
                    "op": "stages",
                    "config": {
                        "input": "users",
                        "pipeline": [
                            {
                                "op": "select",
                                "exprs": [
                                    {"name": "user_id", "expr": "id"},
                                    {"name": "score", "expr": "id * 10"},
                                ],
                            }
                        ],
                    },
                },
                "inputs": {"users": {"dataset": "users", "options": {"read_mode": "snapshot"}}},
                "outputs": {
                    "scores": {"dataset": "scores", "options": {"write_mode": "overwrite"}}
                },
            },
            {
                "name": "join",
                "transform": {
                    "op": "join",
                    "config": {
                        "left_input": "users",
                        "right_input": "scores",
                        "join_type": "inner",
                        "keys": [{"left": "id", "right": "user_id"}],
                        "columns": [
                            {"input": "users", "name": "name"},
                            {"input": "scores", "name": "score"},
                        ],
                    },
                },
                "inputs": {
                    "users": {"dataset": "users", "options": {"read_mode": "snapshot"}},
                    "scores": {"dataset": "scores", "options": {"read_mode": "snapshot"}},
                },
                "outputs": {
                    "joined": {"dataset": "joined", "options": {"write_mode": "overwrite"}}
                },
            },
            {
                "name": "check",
                "script": "check",
                "inputs": {
                    "joined": {"dataset": "joined", "options": {"read_mode": "snapshot"}},
                    "token": {"variable": "token"},
                },
            },
        ],
    }
    write(collection, "pipelines/p.json", pipeline)
    runner = make_runner(
        collection, tmp_path / "data", MemorySecretStore({"demo": {"token": "s3cret"}})
    )
    runner.run("p")
    users = runner.storage.read("users")
    assert users["name"].to_list() == ["s3cret:a", "s3cret:b"]
    assert sorted(runner.storage.read("scores")["score"].to_list()) == [10, 20]


def test_unsupported_features_are_refused(tmp_path):
    collection = tmp_path / "collection"
    write(collection, "codes/c.json", {"name": "c", "type": "python"})
    write(
        collection,
        "codes/c.py",
        "@use_datahub('d')\ndef run(options=None, contexts=None):\n    return {}\n",
    )
    write(
        collection,
        "pipelines/p.json",
        {
            "name": "p",
            "type": "event",
            "steps": [
                {"name": "s", "script": "c", "inputs": {"x": {"source": "source.1"}}},
            ],
        },
    )
    with pytest.raises(PipelineError) as error:
        make_runner(collection, tmp_path / "data").load("p")
    message = str(error.value)
    assert "event pipelines" in message
    assert "ClickHouse" in message
    assert "@use_datahub" in message


def test_sql_and_transform_steps_refuse_variables(tmp_path):
    collection = tmp_path / "collection"
    write(collection, "codes/q.json", {"name": "q", "type": "sql"})
    write(collection, "codes/q.sql", "SELECT 1 AS one")
    write(
        collection,
        "pipelines/p.json",
        {
            "name": "p",
            "steps": [
                {"name": "sql", "script": "q", "outputs": {"x": {"variable": "x"}}},
                {
                    "name": "t",
                    "transform": {"op": "union", "config": {"inputs": ["a", "b"]}},
                    "inputs": {"a": {"variable": "x"}, "b": {"dataset": "d"}},
                    "outputs": {"out": {"dataset": "e"}},
                },
            ],
        },
    )
    with pytest.raises(PipelineError) as error:
        make_runner(collection, tmp_path / "data").load("p")
    assert "variable outputs are not supported for SQL steps" in str(error.value)
    assert "variable inputs are not supported for transform steps" in str(error.value)
