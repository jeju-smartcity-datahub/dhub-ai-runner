"""Collections and tables on S3-compatible storage (RustFS in docker compose;
moto's S3 server here)."""

from pathlib import Path

import pytest
from commons import s3
from commons.collection import open_collection
from commons.state import State
from commons.storage import Storage
from pipeline.runner import PipelineRunner

EXAMPLES = Path(__file__).parent.parent / "examples"


@pytest.fixture
def s3_server(monkeypatch):
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    monkeypatch.setenv("AWS_ENDPOINT_URL", f"http://{host}:{port}")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    yield
    server.stop()


def test_collection_and_tables_on_s3(s3_server, tmp_path):
    assert s3.upload(EXAMPLES / "quickstart", "s3://runner/collections/quickstart") == 15
    collection = open_collection("s3://runner/collections/quickstart")
    assert collection.find("pipeline", "orders")

    storage = Storage("s3://runner/tables")
    runner = PipelineRunner(collection, storage, State(tmp_path))
    runner.run("orders")
    runner.run("orders")
    assert storage.uri("orders_raw") == "s3://runner/tables/orders_raw"
    assert storage.read("orders_raw").height == 40
    assert storage.read("customer_totals")["customer_id"].is_unique().all()


def test_push_replaces_the_collection(s3_server, tmp_path):
    source = tmp_path / "collection"
    (source / "pipelines").mkdir(parents=True)
    (source / "pipelines" / "a.json").write_text('{"name": "a", "steps": []}')
    s3.upload(source, "s3://runner/b")
    (source / "pipelines" / "a.json").unlink()
    (source / "pipelines" / "b.json").write_text('{"name": "b", "steps": []}')
    s3.upload(source, "s3://runner/b")
    names = [d["name"] for d in open_collection("s3://runner/b").list("pipeline")]
    assert names == ["b"]
