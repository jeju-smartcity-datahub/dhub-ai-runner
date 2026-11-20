"""Pipelines as Jobs and agents as pods, against a fake Kubernetes API."""

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import EXAMPLE_SECRETS
from kubernetes.client.exceptions import ApiException
from web import kubernetes as k8s
from web.workspace import Workspace

EXAMPLES = Path(__file__).parent.parent / "examples"


def ns(**kw):
    return SimpleNamespace(**kw)


class FakeBatch:
    def __init__(self):
        self.jobs = {}

    def create_namespaced_job(self, namespace, body):
        meta = body["metadata"]
        self.jobs[meta["name"]] = ns(
            body=body,
            metadata=ns(name=meta["name"], labels=meta["labels"], annotations=meta["annotations"]),
            status=ns(succeeded=None, failed=None, active=1),
        )

    def list_namespaced_job(self, namespace, label_selector):
        wanted = dict(item.split("=") for item in label_selector.split(","))
        return ns(
            items=[j for j in self.jobs.values() if wanted.items() <= j.metadata.labels.items()]
        )

    def delete_namespaced_job(self, name, namespace, propagation_policy=None):
        del self.jobs[name]


class FakeApps:
    def __init__(self):
        self.deployments = {}

    def read_namespaced_deployment(self, name, namespace):
        if name not in self.deployments:
            raise ApiException(status=404)
        return self.deployments[name]

    def read_namespaced_deployment_status(self, name, namespace):
        return ns(status=ns(ready_replicas=1))

    def create_namespaced_deployment(self, namespace, body):
        self.deployments[body["metadata"]["name"]] = body

    def list_namespaced_deployment(self, namespace, label_selector):
        wanted = dict(item.split("=") for item in label_selector.split(","))
        return ns(
            items=[
                ns(metadata=ns(name=n))
                for n, d in self.deployments.items()
                if wanted.items() <= d["metadata"]["labels"].items()
            ]
        )

    def delete_namespaced_deployment(self, name, namespace):
        del self.deployments[name]


class FakeCore:
    def __init__(self):
        self.services = {}

    def create_namespaced_service(self, namespace, body):
        self.services[body["metadata"]["name"]] = body

    def list_namespaced_service(self, namespace, label_selector):
        wanted = dict(item.split("=") for item in label_selector.split(","))
        return ns(
            items=[
                ns(metadata=ns(name=n))
                for n, s in self.services.items()
                if wanted.items() <= s["metadata"]["labels"].items()
            ]
        )

    def delete_namespaced_service(self, name, namespace):
        del self.services[name]


def zip_dir(path: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for file in path.rglob("*"):
            if file.is_file():
                archive.write(file, file.relative_to(path))
    return buffer.getvalue()


@pytest.fixture
def cluster(tmp_path):
    workspace = Workspace(
        str(tmp_path / "collections"), str(tmp_path / "tables"), tmp_path / "data"
    )
    workspace.import_files("agents", [("agents.zip", zip_dir(EXAMPLES / "agents"))])
    workspace.set_secrets(
        "__shared__",
        {(s, k): v for s, keys in EXAMPLE_SECRETS.items() for k, v in keys.items()},
    )
    config = k8s.KubernetesConfig(
        namespace="dhub", image="runner:1", env_from=[("secret", "runner-env")], ready_timeout=1
    )
    batch, apps, core = FakeBatch(), FakeApps(), FakeCore()
    agents = k8s.KubernetesAgents(workspace, config, apps, core)
    executor = k8s.KubernetesExecutor(workspace, config, batch)
    return workspace, executor, agents, batch, apps, core


def test_pipeline_run_is_a_job(cluster, tmp_path):
    workspace, executor, agents, batch, apps, core = cluster
    run_id = executor.submit("agents", "feedback_summary")

    (job,) = batch.jobs.values()
    container = job.body["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "runner:1"
    assert container["args"] == [
        "--collection", "agents",
        "--collection-dir", f"{tmp_path}/collections/agents",
        "--tables", f"{tmp_path}/tables/agents",
        "pipeline", "run", "feedback_summary", "--run-id", run_id,
    ]  # fmt: skip
    assert {"name": "RUNNER_EXECUTOR", "value": "kubernetes"} in container["env"]
    assert container["envFrom"] == [{"secretRef": {"name": "runner-env"}}]
    assert job.body["spec"]["template"]["spec"]["restartPolicy"] == "Never"

    state = workspace.open("agents").state
    assert state.get_run(run_id)["state"] == "queued"
    assert executor.active("agents") == {"feedback_summary": run_id}
    # A second request while it runs gets the same run.
    assert executor.submit("agents", "feedback_summary") == run_id

    # The pod died before it recorded anything: the run is marked failed.
    job.status.failed, job.status.active = 1, 0
    assert executor.active("agents") == {}
    assert state.get_run(run_id)["state"] == "error"


def test_cancel_deletes_the_job(cluster):
    workspace, executor, *_ = cluster
    run_id = executor.submit("agents", "feedback_summary")
    assert executor.cancel("agents", run_id)
    assert executor.active("agents") == {}
    assert workspace.open("agents").state.get_run(run_id)["state"] == "cancelled"
    assert not executor.cancel("agents", "nope")


def test_agent_pods_follow_changes(cluster, monkeypatch):
    workspace, executor, agents, batch, apps, core = cluster
    calls = []

    def post(url, json, timeout):
        calls.append((url, json))
        return SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {
                "content": "hi",
                "pending": [],
                "events": [],
                "session_id": json["session_id"],
            },
        )

    monkeypatch.setattr(k8s.httpx, "post", post)
    reply = agents.chat("agents", "payment-assistant", [{"role": "user", "content": "x"}], "s1")
    assert reply["content"] == "hi"
    service = k8s.agent_service("agents", "payment-assistant")
    assert apps.deployments[service]["spec"]["template"]["spec"]["containers"][0]["args"][-8:] == [
        "agent", "serve", "--agent", "payment-assistant", "--host", "0.0.0.0", "--port", "8000",
    ]  # fmt: skip
    assert calls[0][0] == f"http://{service}.dhub.svc:8000/v1/agents/payment-assistant/chat"

    # Stopping one agent removes its pod; status reports the pods that run.
    assert agents.status("agents", ["payment-assistant", "travel-planner"]) == {
        "payment-assistant": "running"
    }
    agents.stop("agents", "payment-assistant")
    assert service not in apps.deployments and service not in core.services
    assert agents.status("agents", ["payment-assistant"]) == {}
    agents.chat("agents", "payment-assistant", [], "s1")

    # New secrets: the collection's agent pods are replaced on next use.
    workspace.set_secrets("agents", {("x", "y"): "z"})
    assert apps.deployments == {} and core.services == {}
    agents.chat("agents", "payment-assistant", [], "s1")
    assert service in apps.deployments


def test_names_are_valid_and_stable():
    name = k8s.resource_name("agent", "도시 환경_collection" * 3, "City Analyst!")
    assert len(name) <= 63 and name == k8s.resource_name(
        "agent", "도시 환경_collection" * 3, "City Analyst!"
    )
    assert all(c.isalnum() and c.islower() or c.isdigit() or c == "-" for c in name)
    assert k8s.label("a/b:c" * 30).replace("-", "").isalnum()
