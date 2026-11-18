"""The dataset catalog in Postgres, and its web API."""

import io

import polars as pl
import pytest
from commons.catalog import parse_query
from fastapi.testclient import TestClient
from test_web import EXAMPLES, upload, zip_dir
from web.app import create_app
from web.runs import ThreadExecutor, wait_for
from web.workspace import Workspace


@pytest.fixture
def web(tmp_path, monkeypatch, postgres_url):
    import psycopg

    with psycopg.connect(postgres_url, autocommit=True) as db:
        for table in ("tables", "columns", "history", "lineage", "notes", "queries"):
            db.execute(f"DROP TABLE IF EXISTS runner_catalog_{table}")
    monkeypatch.setenv("RUNNER_DATABASE_URL", postgres_url)
    workspace = Workspace(
        str(tmp_path / "collections"), str(tmp_path / "tables"), tmp_path / "data"
    )
    executor = ThreadExecutor(workspace, max_runs=2)
    app = create_app(workspace, executor, schedule=False, catalog_refresh=None)
    with TestClient(app) as client:
        assert upload(client, "qs", [("qs.zip", zip_dir(EXAMPLES / "quickstart"))]).is_success
        yield client, executor, workspace


def run(client, executor, collection, pipeline):
    run_id = client.post(f"/api/collections/{collection}/pipelines/{pipeline}/runs").json()[
        "run_id"
    ]
    wait_for(executor, collection, run_id)


def test_import_lists_datasets_before_they_have_data(web):
    client, _, _ = web
    found = client.get("/api/catalog").json()
    assert found["total"] == 3
    assert {i["name"]: i["has_data"] for i in found["items"]} == {
        "customer_totals": False,
        "orders_daily": False,
        "orders_raw": False,
    }
    lineage = client.get("/api/collections/qs/datasets/orders_raw").json()["lineage"]
    assert [(w["pipeline"], w["step"], w["mode"]) for w in lineage["writers"]] == [
        ("orders", "generate", "append")
    ]
    assert {d["dataset"] for d in lineage["downstream"]} == {"orders_daily", "customer_totals"}


def test_runs_update_the_catalog(web):
    client, executor, _ = web
    run(client, executor, "qs", "orders")
    run(client, executor, "qs", "orders")
    d = client.get("/api/collections/qs/datasets/orders_raw").json()
    assert d["catalog"] is True
    assert d["has_data"] and d["rows"] == 40 and d["version"] == 1 and d["files"] == 2
    assert d["size_bytes"] > 0 and d["properties"]["delta.enableChangeDataFeed"] == "true"
    assert [h["version"] for h in d["history"]] == [1, 0]
    assert d["history"][0]["operation"] == "WRITE"
    order_id = next(c for c in d["columns"] if c["name"] == "order_id")
    assert order_id["null_count"] == 0 and order_id["min"] is not None
    upstream = client.get("/api/collections/qs/datasets/orders_daily").json()["lineage"]["upstream"]
    assert upstream == [{"dataset": "orders_raw", "pipeline": "orders", "step": "daily"}]

    # Time travel: the first version had the first run's rows.
    first = client.get("/api/collections/qs/datasets/orders_raw/rows?version=0&limit=5").json()
    assert first["total"] == 20 and len(first["rows"]) == 5
    assert client.get("/api/collections/qs/datasets/orders_raw/rows").json()["total"] == 40
    assert client.get("/api/collections/qs/datasets/orders_raw/rows?version=9").status_code == 422

    csv = client.get("/api/collections/qs/datasets/orders_raw/export?version=0")
    assert csv.headers["content-disposition"].endswith('orders_raw_v0.csv"')
    assert pl.read_csv(io.BytesIO(csv.content)).height == 20
    parquet = client.get("/api/collections/qs/datasets/orders_raw/export?format=parquet").content
    assert pl.read_parquet(io.BytesIO(parquet)).height == 40


def test_search(web):
    client, executor, _ = web
    run(client, executor, "qs", "orders")
    names = lambda **params: [  # noqa: E731
        i["name"] for i in client.get("/api/catalog", params=params).json()["items"]
    ]
    # The name ranks above a match in the description.
    assert names(q="orders") == ["orders_daily", "orders_raw"]
    assert names(q="order")[-1] == "customer_totals"  # "order" only in its description
    assert names(q="paid customer") == ["customer_totals", "orders_raw"]  # column descriptions
    assert names(q="결제 합계") == ["customer_totals"]  # alias and column description
    assert names(q="tag:집계") == ["customer_totals", "orders_daily"]  # tags of the definitions
    assert names(q="col:customer_id") == ["customer_totals", "orders_raw"]
    found = client.get("/api/catalog", params={"q": "customer_id"}).json()
    assert found["items"][0]["matched_columns"] == ["customer_id"]
    assert names(q="nothing-like-this") == []
    assert names(sort="rows")[0] == "orders_raw"
    assert names(sort="name", order="desc")[0] == "orders_raw"
    assert len(client.get("/api/catalog", params={"limit": 1, "offset": 1}).json()["items"]) == 1
    assert client.get("/api/catalog?sort=bogus").status_code == 422


def test_notes_survive_syncs_and_are_searched(web):
    client, executor, _ = web
    run(client, executor, "qs", "orders")
    response = client.put(
        "/api/collections/qs/datasets/orders_raw/notes",
        json={
            "owner": "data team",
            "tags": ["Sales", "raw", "sales", " "],
            "readme": "주문 원천 데이터",
            "columns": {"amount": "부가세 포함"},
        },
    )
    assert response.status_code == 200, response.text
    d = response.json()
    # The definition's tags (주문, 원천) and the noted ones, without repeats.
    assert d["tags"] == ["raw", "Sales", "원천", "주문"] and d["owner"] == "data team"
    assert d["notes"]["tags"] == ["raw", "Sales"]
    assert next(c for c in d["columns"] if c["name"] == "amount")["note"] == "부가세 포함"

    run(client, executor, "qs", "orders")  # a sync after the run
    client.post("/api/catalog/sync", json={"force": True})
    d = client.get("/api/collections/qs/datasets/orders_raw").json()
    assert d["notes"]["readme"] == "주문 원천 데이터" and d["has_data"]

    def names(**params):
        return [i["name"] for i in client.get("/api/catalog", params=params).json()["items"]]

    assert names(q="원천") == ["orders_raw"]
    assert names(q="부가세") == ["orders_raw"]
    assert names(q="tag:sales") == ["orders_raw"]
    assert names(tag="raw") == ["orders_raw"]
    assert names(owner="Data Team") == ["orders_raw"]
    facets = client.get("/api/catalog").json()["facets"]
    assert {(t["value"], t["count"]) for t in facets["tags"]} == {
        ("raw", 1),
        ("Sales", 1),
        ("원천", 1),
        ("주문", 3),
        ("집계", 2),
        ("일간", 1),
        ("고객", 1),
    }
    assert facets["collections"] == [{"value": "qs", "label": None, "count": 3}]

    # Removing a column note; refusing what is not a note.
    client.put("/api/collections/qs/datasets/orders_raw/notes", json={"columns": {"amount": ""}})
    d = client.get("/api/collections/qs/datasets/orders_raw").json()
    assert d["notes"]["columns"] == {} and d["notes"]["owner"] == "data team"
    assert (
        client.put("/api/collections/qs/datasets/orders_raw/notes", json={"tags": "x"}).status_code
        == 422
    )
    assert client.put("/api/collections/qs/datasets/nope/notes", json={}).status_code == 404


def test_query_and_saved_queries(web):
    client, executor, _ = web
    run(client, executor, "qs", "orders")
    result = client.post(
        "/api/collections/qs/query",
        json={"sql": 'SELECT count(*) AS n FROM "orders_raw"'},
    ).json()
    assert result["rows"] == [{"n": 20}]
    bad = client.post("/api/collections/qs/query", json={"sql": "DROP TABLE orders_raw"})
    assert bad.status_code == 422

    saved = client.post("/api/collections/qs/queries", json={"title": "count", "sql": "SELECT 1"})
    query_id = saved.json()["id"]
    client.post(
        "/api/collections/qs/queries", json={"id": query_id, "title": "n", "sql": "SELECT 2"}
    )
    assert [(q["title"], q["sql"]) for q in client.get("/api/collections/qs/queries").json()] == [
        ("n", "SELECT 2")
    ]
    assert client.post("/api/collections/qs/queries", json={"title": ""}).status_code == 422
    assert client.delete(f"/api/collections/qs/queries/{query_id}").json() == {"deleted": query_id}
    assert client.get("/api/collections/qs/queries").json() == []


def test_collection_changes_reach_the_catalog(web, tmp_path):
    client, _, workspace = web
    client.put("/api/collections/qs/datasets/orders_raw/notes", json={"owner": "me"})
    client.post("/api/collections/qs/queries", json={"title": "t", "sql": "SELECT 1"})
    upload(client, "two", [("qs.zip", zip_dir(EXAMPLES / "quickstart"))])
    assert client.get("/api/catalog").json()["total"] == 6
    assert client.get("/api/catalog?collection=two").json()["total"] == 3

    client.delete("/api/collections/qs")
    assert {i["collection"] for i in client.get("/api/catalog").json()["items"]} == {"two"}
    assert workspace.catalog.queries("qs") == []

    # A collection removed behind the app's back is forgotten on the next full sync.
    import shutil

    shutil.rmtree(tmp_path / "collections" / "two")
    workspace.invalidate("two")
    client.post("/api/catalog/sync", json={})
    assert client.get("/api/catalog").json()["total"] == 0


def test_without_postgres(tmp_path, monkeypatch):
    """Datasets are described from the Delta log; search and notes need Postgres."""
    monkeypatch.delenv("RUNNER_DATABASE_URL", raising=False)
    workspace = Workspace(
        str(tmp_path / "collections"), str(tmp_path / "tables"), tmp_path / "data"
    )
    executor = ThreadExecutor(workspace, max_runs=1)
    with TestClient(create_app(workspace, executor, schedule=False)) as client:
        upload(client, "qs", [("qs.zip", zip_dir(EXAMPLES / "quickstart"))])
        run(client, executor, "qs", "orders")
        d = client.get("/api/collections/qs/datasets/orders_raw").json()
        assert d["catalog"] is False and d["rows"] == 20 and d["history"][0]["version"] == 0
        assert [w["step"] for w in d["lineage"]["writers"]] == ["generate"]
        assert {n["dataset"] for n in d["lineage"]["downstream"]} == {
            "orders_daily",
            "customer_totals",
        }
        up = client.get("/api/collections/qs/datasets/orders_daily").json()["lineage"]["upstream"]
        assert up == [{"dataset": "orders_raw", "pipeline": "orders", "step": "daily"}]
        assert client.get("/api/collections/qs/datasets/nope").status_code == 404
        response = client.get("/api/catalog")
        assert response.status_code == 503 and "RUNNER_DATABASE_URL" in response.json()["detail"]
        notes = client.put("/api/collections/qs/datasets/orders_raw/notes", json={"owner": "x"})
        assert notes.status_code == 503
        query = client.post(
            "/api/collections/qs/query", json={"sql": 'SELECT 1 AS one FROM "orders_raw" LIMIT 1'}
        )
        assert query.json()["rows"] == [{"one": 1}]


def test_parse_query():
    assert parse_query('tag:sales col:"order id" Owner:kim 주문  "two words" foo:bar') == (
        ["주문", "two words", "foo:bar"],
        {"tag": ["sales"], "column": ["order id"], "owner": ["kim"]},
    )
