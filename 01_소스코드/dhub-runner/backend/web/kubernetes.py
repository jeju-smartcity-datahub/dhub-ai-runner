"""Running pipelines and agents as Kubernetes pods.

Locally the web app runs both in its own process (``runs.ThreadExecutor`` and
in-process agents). On Kubernetes (``RUNNER_EXECUTOR=kubernetes``):

- each pipeline run is a Job, ``dhub-runner pipeline run`` in the runner image;
- each agent is a Deployment and Service, ``dhub-runner agent serve --agent``,
  started when it is first needed and replaced when its collection or secrets
  change; the web app forwards chats to it.

The pods share what the web app uses: collections and tables on RustFS, and
secrets and state (runs, checkpoints, sessions, dataset locks) in Postgres.
They get the same infrastructure settings through ``envFrom``
(RUNNER_K8S_ENV_FROM, e.g. ``secret/runner-env,configmap/runner-config``).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from agent.runtime import AgentError

from .workspace import Workspace

log = logging.getLogger(__name__)

MANAGED_BY = "dhub-runner"
AGENT_PORT = 8000
_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


def _dns(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")


def resource_name(prefix: str, *parts: str) -> str:
    """A DNS-1035 name for ``parts``, unique even when the readable part is cut."""
    digest = hashlib.sha1("/".join(parts).encode()).hexdigest()[:8]
    readable = "-".join(_dns(p) for p in parts)[: 52 - len(prefix)].strip("-")
    return f"{prefix}-{readable}-{digest}"


def label(text: str) -> str:
    """A label value for ``text`` (63 characters at most)."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text)[:63].strip("-_.") or "x"


def agent_service(collection: str, agent: str) -> str:
    return resource_name("agent", collection, agent)


def agent_url(collection: str, agent: str, namespace: str) -> str:
    """Where an agent's pod answers, from inside the cluster."""
    return f"http://{agent_service(collection, agent)}.{namespace}.svc:{AGENT_PORT}"


@dataclass
class KubernetesConfig:
    namespace: str
    image: str
    env_from: list[tuple[str, str]] = field(default_factory=list)  # (kind, name)
    service_account: str | None = None
    image_pull_policy: str = "IfNotPresent"
    job_ttl_seconds: int = 3600
    ready_timeout: float = 180

    @classmethod
    def from_env(cls) -> KubernetesConfig:
        """Settings of the runner's own deployment (not secrets of pipelines)."""
        namespace = os.environ.get("RUNNER_K8S_NAMESPACE")
        if not namespace and os.path.exists(_NAMESPACE_FILE):
            with open(_NAMESPACE_FILE) as handle:
                namespace = handle.read().strip()
        image = os.environ.get("RUNNER_K8S_IMAGE")
        if not namespace or not image:
            raise RuntimeError(
                "RUNNER_EXECUTOR=kubernetes needs RUNNER_K8S_NAMESPACE and RUNNER_K8S_IMAGE"
            )
        env_from = []
        for item in filter(None, (os.environ.get("RUNNER_K8S_ENV_FROM") or "").split(",")):
            kind, _, name = item.strip().partition("/")
            if kind not in ("secret", "configmap") or not name:
                raise RuntimeError(
                    f"RUNNER_K8S_ENV_FROM: {item!r} is not secret/<name> or configmap/<name>"
                )
            env_from.append((kind, name))
        return cls(
            namespace=namespace,
            image=image,
            env_from=env_from,
            service_account=os.environ.get("RUNNER_K8S_SERVICE_ACCOUNT") or None,
            image_pull_policy=os.environ.get("RUNNER_K8S_IMAGE_PULL_POLICY", "IfNotPresent"),
        )


def load_api() -> tuple[Any, Any, Any]:
    """(BatchV1Api, AppsV1Api, CoreV1Api) from the in-cluster service account,
    or the local kubeconfig outside a cluster."""
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.BatchV1Api(), client.AppsV1Api(), client.CoreV1Api()


class _Pods:
    """What the Job and Deployment pod specs share."""

    def __init__(self, workspace: Workspace, config: KubernetesConfig):
        self.workspace = workspace
        self.config = config

    def container(
        self, collection: str, args: list[str], env: dict[str, str], **extra: Any
    ) -> dict:
        base = [
            "--collection", collection,
            "--collection-dir", self.workspace.location(collection),
            "--tables", f"{self.workspace.tables}/{collection}",
        ]  # fmt: skip
        env = {
            "RUNNER_EXECUTOR": "kubernetes",
            "RUNNER_K8S_NAMESPACE": self.config.namespace,
            **env,
        }
        return {
            "name": "runner",
            "image": self.config.image,
            "imagePullPolicy": self.config.image_pull_policy,
            "args": base + args,
            "env": [{"name": k, "value": v} for k, v in env.items()],
            "envFrom": [
                {"secretRef": {"name": name}}
                if kind == "secret"
                else {"configMapRef": {"name": name}}
                for kind, name in self.config.env_from
            ],
            **extra,
        }

    def pod_spec(self, container: dict, restart: str) -> dict:
        spec = {"containers": [container], "restartPolicy": restart}
        if self.config.service_account:
            spec["serviceAccountName"] = self.config.service_account
        return spec

    @staticmethod
    def labels(collection: str, kind: str, **more: str) -> dict[str, str]:
        return {
            "app.kubernetes.io/managed-by": MANAGED_BY,
            "dhub-runner/kind": kind,
            "dhub-runner/collection": label(collection),
            **{f"dhub-runner/{k}": label(v) for k, v in more.items()},
        }


class KubernetesAgents(_Pods):
    """Agents in pods: one Deployment and Service per agent, created on demand."""

    def __init__(self, workspace: Workspace, config: KubernetesConfig, apps: Any, core: Any):
        super().__init__(workspace, config)
        self.apps, self.core = apps, core
        self._lock = threading.Lock()
        workspace.listeners.append(self.retire)

    def ensure(self, collection: str, agent: str) -> str:
        """Start the agent's pod unless it runs; wait until it is ready; return its URL."""
        name = agent_service(collection, agent)
        with self._lock:
            if not self._exists(name):
                self._create(collection, agent, name)
        deadline = time.monotonic() + self.config.ready_timeout
        while time.monotonic() < deadline:
            status = self.apps.read_namespaced_deployment_status(name, self.config.namespace).status
            if (status.ready_replicas or 0) >= 1:
                return agent_url(collection, agent, self.config.namespace)
            time.sleep(2)
        raise AgentError(
            f"agent {agent!r}: its pod did not become ready in {self.config.ready_timeout:.0f}s"
        )

    def _exists(self, name: str) -> bool:
        from kubernetes.client.exceptions import ApiException

        try:
            self.apps.read_namespaced_deployment(name, self.config.namespace)
            return True
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise

    def _create(self, collection: str, agent: str, name: str) -> None:
        labels = self.labels(collection, "agent", agent=agent)
        selector = {"dhub-runner/agent-id": name}
        container = self.container(
            collection,
            ["agent", "serve", "--agent", agent, "--host", "0.0.0.0", "--port", str(AGENT_PORT)],
            {},
            ports=[{"containerPort": AGENT_PORT, "name": "http"}],
            readinessProbe={"httpGet": {"path": "/health", "port": AGENT_PORT}, "periodSeconds": 3},
        )
        self.apps.create_namespaced_deployment(
            self.config.namespace,
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name, "labels": labels},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": selector},
                    "template": {
                        "metadata": {"labels": {**labels, **selector}},
                        "spec": self.pod_spec(container, "Always"),
                    },
                },
            },
        )
        self.core.create_namespaced_service(
            self.config.namespace,
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": name, "labels": labels},
                "spec": {
                    "selector": selector,
                    "ports": [{"name": "http", "port": AGENT_PORT, "targetPort": AGENT_PORT}],
                },
            },
        )
        log.info("started agent %s/%s as %s", collection, agent, name)

    def stop(self, collection: str, agent: str) -> None:
        """Remove one agent's Deployment and Service."""
        from kubernetes.client.exceptions import ApiException

        name = agent_service(collection, agent)
        with self._lock:
            for delete in (
                self.apps.delete_namespaced_deployment,
                self.core.delete_namespaced_service,
            ):
                try:
                    delete(name, self.config.namespace)
                except ApiException as exc:
                    if exc.status != 404:
                        raise

    def status(self, collection: str, agents: list[str]) -> dict[str, str]:
        """``{agent: "running" | "starting"}`` for the agents that have a pod."""
        selector = (
            f"app.kubernetes.io/managed-by={MANAGED_BY},dhub-runner/kind=agent,"
            f"dhub-runner/collection={label(collection)}"
        )
        ns = self.config.namespace
        deployed = {
            d.metadata.name
            for d in self.apps.list_namespaced_deployment(ns, label_selector=selector).items
        }
        out = {}
        for agent in agents:
            name = agent_service(collection, agent)
            if name in deployed:
                status = self.apps.read_namespaced_deployment_status(name, ns).status
                out[agent] = "running" if (status.ready_replicas or 0) >= 1 else "starting"
        return out

    def retire(self, collection: str | None) -> None:
        """Remove the agents of a collection (all for None); they start again,
        with the new definitions and secrets, when next needed."""
        selector = f"app.kubernetes.io/managed-by={MANAGED_BY},dhub-runner/kind=agent"
        if collection is not None:
            selector += f",dhub-runner/collection={label(collection)}"
        ns = self.config.namespace
        with self._lock:
            for item in self.apps.list_namespaced_deployment(ns, label_selector=selector).items:
                self.apps.delete_namespaced_deployment(item.metadata.name, ns)
            for item in self.core.list_namespaced_service(ns, label_selector=selector).items:
                self.core.delete_namespaced_service(item.metadata.name, ns)

    def chat(
        self, collection: str, agent: str, messages: list[dict], session_id: str | None
    ) -> dict:
        response = httpx.post(
            f"{self.ensure(collection, agent)}/v1/agents/{agent}/chat",
            json={"session_id": session_id, "messages": messages},
            timeout=600,
        )
        if response.status_code == 409:
            raise AgentError(response.json().get("detail"))
        response.raise_for_status()
        return response.json()

    def invoke(self, collection: str, agent: str, inputs: dict) -> Any:
        response = httpx.post(
            f"{self.ensure(collection, agent)}/v1/agents/{agent}/invoke",
            json={"inputs": inputs},
            timeout=600,
        )
        response.raise_for_status()
        return response.json()["output"]


class KubernetesExecutor(_Pods):
    """Pipeline runs as Jobs. One run per pipeline at a time; a run's state is
    what the Job's pod records in Postgres, and a Job that fails before it can
    record anything is marked failed here."""

    def __init__(self, workspace: Workspace, config: KubernetesConfig, batch: Any):
        super().__init__(workspace, config)
        self.batch = batch
        self._lock = threading.Lock()

    def submit(self, collection: str, pipeline: str) -> str:
        opened = self.workspace.open(collection)
        opened.runner.load(pipeline)  # PipelineError for a pipeline that cannot run
        with self._lock:
            running = self.active(collection)
            if pipeline in running:
                return running[pipeline]
            run_id = uuid.uuid4().hex[:12]
            opened.state.queue_run(run_id, pipeline)
            labels = self.labels(collection, "run", run=run_id)
            # Labels select; annotations keep the exact names (labels allow fewer characters).
            annotations = {"dhub-runner/pipeline": pipeline, "dhub-runner/run": run_id}
            container = self.container(
                collection, ["pipeline", "run", pipeline, "--run-id", run_id], {}
            )
            self.batch.create_namespaced_job(
                self.config.namespace,
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {
                        "name": resource_name("run", collection, pipeline, run_id),
                        "labels": labels,
                        "annotations": annotations,
                    },
                    "spec": {
                        "backoffLimit": 0,
                        "ttlSecondsAfterFinished": self.config.job_ttl_seconds,
                        "template": {
                            "metadata": {"labels": labels},
                            "spec": self.pod_spec(container, "Never"),
                        },
                    },
                },
            )
        log.info("started run %s of %s/%s as a Job", run_id, collection, pipeline)
        return run_id

    def _jobs(self, collection: str) -> list[Any]:
        selector = (
            f"app.kubernetes.io/managed-by={MANAGED_BY},dhub-runner/kind=run,"
            f"dhub-runner/collection={label(collection)}"
        )
        return self.batch.list_namespaced_job(self.config.namespace, label_selector=selector).items

    def active(self, collection: str) -> dict[str, str]:
        state = self.workspace.open(collection).state
        active = {}
        for job in self._jobs(collection):
            notes = job.metadata.annotations or {}
            pipeline, run_id = notes.get("dhub-runner/pipeline"), notes.get("dhub-runner/run")
            status = job.status
            finished = (status.succeeded or 0) > 0 or (status.failed or 0) > 0
            run = state.get_run(run_id) if run_id else None
            if not finished:
                active[pipeline] = run_id
            elif run and run["state"] in ("queued", "running"):
                # The pod ended without recording it (image pull, OOM kill, ...).
                state.end_run(
                    run_id,
                    "error" if status.failed else "interrupted",
                    "the run's pod ended without finishing the run",
                )
        return active

    def cancel(self, collection: str, run_id: str) -> bool:
        for job in self._jobs(collection):
            if (job.metadata.annotations or {}).get("dhub-runner/run") == run_id:
                self.batch.delete_namespaced_job(
                    job.metadata.name, self.config.namespace, propagation_policy="Background"
                )
                self.workspace.open(collection).state.end_run(run_id, "cancelled", "cancelled")
                return True
        return False
