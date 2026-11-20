"""Importing a D.Hub for AI collection export."""

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import polars as pl
import pytest
from agent.runtime import Agent, AgentError
from commons.collection import Collection
from commons.importer import import_export
from commons.scripts import Resources
from commons.state import State
from commons.storage import Storage
from conftest import MemorySecretStore, REST_VERBS
from pipeline.runner import PipelineRunner


def export_zip(path: Path) -> Path:
    sales = io.BytesIO()
    pl.DataFrame({"region": ["SEL", "BSN", "SEL"], "amount": [10, 20, 30]}).write_parquet(sales)
    files = {
        "manifest.json": {
            "name": "demo",
            "version": "2.2.0",
            "entities": {
                "collections": ["collections/demo.json"],
                "folders": ["folders/root.json"],
                "datasets": [
                    "datasets/sales.json",
                    "datasets/region_sums.json",
                    "datasets/region_totals.json",
                ],
                "codes": ["codes/totals.json", "codes/stamp.json"],
                "pipelines": ["pipelines/daily.json"],
                "ontologies": {"entities": ["ontologies/entities/region.json"]},
            },
            "data_files": {"parquet": ["data/parquet/sales.parquet"]},
            "manifests": [
                "manifests/llm.json",
                "manifests/tool.json",
                "manifests/agent.json",
                "manifests/analyst.json",
                "manifests/semantic.json",
                "manifests/connector.json",
                "manifests/dashboard.json",
            ],
            "requirements": {"secrets": [{"id": "secret.42", "name": "crm", "keys": ["token"]}]},
        },
        "collections/demo.json": {"name": "demo", "category": "collection"},
        "folders/root.json": {"name": "root", "path": "/root"},
        "datasets/sales.json": {"name": "sales", "collection_id": "demo"},
        "datasets/region_sums.json": {"name": "region_sums", "collection_id": "demo"},
        "datasets/region_totals.json": {"name": "region_totals", "collection_id": "demo"},
        "ontologies/entities/region.json": {
            "name": "region",
            "schema_": {"fields": [], "metadata": {"keys": "region"}},
        },
        "codes/totals.json": {"name": "totals", "type": "sql"},
        # The portal export writes SQL code to <name>.sql.
        "codes/totals.sql": "SELECT region, SUM(amount) AS amount FROM sales GROUP BY region",
        "codes/stamp.json": {"name": "stamp", "type": "python"},
        "codes/stamp.py": (
            "import polars as pl\n"
            "def run(totals, options=None, contexts=None):\n"
            "    token = contexts['secrets']['token']\n"
            "    return {'out': totals.with_columns(pl.lit(token).alias('token'))}\n"
        ),
        "pipelines/daily.json": {
            "name": "daily",
            "type": "batch",
            "collection_id": "demo",
            "steps": [
                {
                    "name": "sum",
                    "script": "totals",
                    "inputs": {"sales": {"dataset": "sales", "options": {"read_mode": "snapshot"}}},
                    "outputs": {
                        "totals": {"dataset": "region_sums", "options": {"write_mode": "overwrite"}}
                    },
                },
                {
                    "name": "stamp",
                    "script": "stamp",
                    "secrets": [{"name": "token", "ref": "secret://secret.42/token"}],
                    "inputs": {
                        "totals": {"dataset": "region_sums", "options": {"read_mode": "snapshot"}}
                    },
                    "outputs": {
                        "out": {"dataset": "region_totals", "options": {"write_mode": "overwrite"}}
                    },
                },
            ],
        },
        "data/parquet/sales.parquet": sales.getvalue(),
        "manifests/llm.json": {
            "category": "llm.model",
            "name": "assistant-llm",
            "provider": "claude",
            "model": "claude-opus-5-5",
            "has_api_key": True,
        },
        "manifests/local_llm.json": {
            "category": "llm.model",
            "name": "local-llm",
            "provider": "openai",
            "model": "gemma-4-31b-it",
            "base_url": "http://llm.internal/v1",
            "has_api_key": False,
        },
        "manifests/tool.json": {
            "category": "agent.tool",
            "id": "agent.tool.7",
            "name": "region_total",
            "type": "python",
            "function": {
                "source": "def execute(region):\n    return {'region': region.upper()}\n",
                "inputs": {"type": "object", "properties": {"region": {"type": "string"}}},
            },
        },
        "manifests/agent.json": {
            "category": "agent",
            "name": "helper",
            "mode": "react",
            "agent": {
                "instructions": "Answer with region totals.",
                "model_provider": "claude",
                "model": "assistant-llm",
                "tools": [{"id": "agent.tool.7", "name": "region_total"}],
            },
        },
        "manifests/analyst.json": {
            "category": "agent",
            "name": "analyst",
            "mode": "react",
            "tables": ["semantic.table.orders"],
            "agent": {"instructions": "x", "model_provider": "claude", "model": "assistant-llm"},
        },
        "manifests/semantic.json": {"category": "semantic.table", "name": "orders"},
        "manifests/connector.json": {
            "category": "connector",
            "name": "crm_api",
            "type": "rest",
            "options": {"token_ref": "secret://secret.42/token"},
            "script": "def connect(options, collection=None):\n    return options\n"
            "def get(client, path, params=None):\n    return None\n" + REST_VERBS,
        },
        "manifests/dashboard.json": {"category": "dashboard", "name": "overview"},
    }
    target = path / "demo-export.zip"
    with zipfile.ZipFile(target, "w") as archive:
        for name, content in files.items():
            if isinstance(content, dict):
                content = json.dumps(content)
            archive.writestr(f"demo/{name}", content)
    return target


def test_import_collection_export(tmp_path):
    report = import_export(export_zip(tmp_path), tmp_path / "collection")

    assert report.imported["pipeline"] == ["daily"]
    assert sorted(report.imported["code"]) == ["stamp", "totals"]
    assert sorted(report.imported["agent"]) == ["analyst", "helper"]
    assert report.skipped == {
        "folder": ["root"],
        "ontology.entity": ["region"],
        "semantic.table": ["orders"],
        "dashboard": ["overview"],
    }
    assert report.data == ["sales"]
    # The model's key, left out of the export, becomes a secret named after the model.
    assert report.secrets == ["assistant-llm/api_key", "crm/token"]

    secrets = MemorySecretStore()
    collection = Collection(tmp_path / "collection", secrets)
    assert collection.get("secret", "secret.42") == {
        "name": "crm",
        "id": "secret.42",
        "keys": ["token"],
    }
    assert collection.get("llm", "assistant-llm")["api_key_ref"] == "secret://assistant-llm/api_key"
    # D.Hub for AI calls this one without a key, so the runner asks for none.
    assert "api_key_ref" not in collection.get("llm", "local-llm")
    assert (tmp_path / "collection" / "codes" / "totals.sql").exists()
    assert not (tmp_path / "collection" / "secrets.env.example").exists()

    secrets.set("crm", "token", "t0k")
    secrets.set("assistant-llm", "api_key", "test")
    runner = PipelineRunner(collection, Storage(tmp_path / "tables"), State(tmp_path))
    runner.run("daily")
    regions = runner.storage.read("region_totals").sort("region")
    assert regions.to_dicts() == [
        {"region": "BSN", "amount": 20, "token": "t0k"},
        {"region": "SEL", "amount": 40, "token": "t0k"},
    ]

    agent = Agent(collection, Resources(collection), name="helper")
    assert agent.tools["region_total"].call({"region": "sel"}, {}) == {"region": "SEL"}
    with pytest.raises(AgentError, match="semantic layer"):
        Agent(collection, Resources(collection), name="analyst")
    assert Resources(collection).connector("crm_api")._client["token"] == "t0k"


def test_import_refuses_a_non_empty_destination(tmp_path):
    (tmp_path / "collection").mkdir()
    (tmp_path / "collection" / "x").write_text("")
    with pytest.raises(ValueError, match="not empty"):
        import_export(export_zip(tmp_path), tmp_path / "collection")


def test_import_command(tmp_path):
    command = ["import", str(export_zip(tmp_path)), str(tmp_path / "b")]
    result = subprocess.run(
        [sys.executable, "-c", f"from cli import main; raise SystemExit(main({command!r}))"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["imported"]["pipeline"] == ["daily"]
