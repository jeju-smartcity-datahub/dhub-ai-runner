"""dhub-runner command line.

    dhub-runner check
    dhub-runner pipeline run NAME | serve [NAME...] | status NAME | reset NAME
    dhub-runner agent serve [--agent NAME...] | chat NAME
    dhub-runner table show NAME

    dhub-runner secret set SECRET/KEY | list | delete SECRET[/KEY]   [--shared]

    dhub-runner collection push DIR
    dhub-runner import EXPORT DEST
    dhub-runner web

Locations come from --collection-dir / --tables / --data or RUNNER_COLLECTION_DIR /
RUNNER_TABLES / RUNNER_DATA_DIR. The collection and the tables may be on
S3-compatible storage such as RustFS (``s3://bucket/prefix``; see commons/s3.py).

Secrets are read from the collection's secret store (--collection, by default
the collection directory's name), never from environment variables; set them with
``dhub-runner secret set`` or in the web UI.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path

from commons import s3
from commons.collection import Collection, CollectionError, open_collection
from commons.scripts import missing_secrets
from commons.secrets import SHARED, SecretStoreError, collection_secrets, open_store
from commons.state import State, open_state
from commons.storage import Storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dhub-runner", description=__doc__.split("\n")[0])
    parser.add_argument(
        "--collection-dir",
        default=os.environ.get("RUNNER_COLLECTION_DIR", "collection"),
        help="the collection's definitions: a directory or s3://bucket/prefix",
    )
    parser.add_argument("--tables", default=os.environ.get("RUNNER_TABLES"))
    parser.add_argument("--data", default=os.environ.get("RUNNER_DATA_DIR", "data"))
    parser.add_argument("--log-level", default=os.environ.get("RUNNER_LOG_LEVEL", "INFO"))
    parser.add_argument(
        "--collection",
        default=os.environ.get("RUNNER_COLLECTION"),
        help="the collection's name, whose secrets to use (default: the --collection-dir name)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("check", help="validate every pipeline and agent in the collection")

    secret_cmd = commands.add_parser("secret").add_subparsers(dest="action", required=True)
    secret_set = secret_cmd.add_parser("set", help="set a secret key; the value is read from stdin")
    secret_set.add_argument("ref", metavar="SECRET/KEY")
    secret_list = secret_cmd.add_parser("list", help="list secret keys (never values)")
    secret_delete = secret_cmd.add_parser("delete", help="delete a secret key or a whole secret")
    secret_delete.add_argument("ref", metavar="SECRET[/KEY]")
    for sub in (secret_set, secret_list, secret_delete):
        sub.add_argument("--shared", action="store_true", help="the store every collection uses")

    collection_cmd = commands.add_parser("collection").add_subparsers(dest="action", required=True)
    push = collection_cmd.add_parser(
        "push", help="upload a local collection directory to --collection-dir (s3://)"
    )
    push.add_argument("source")

    import_cmd = commands.add_parser(
        "import", help="turn a D.Hub for AI collection export (.zip or directory) into a collection"
    )
    import_cmd.add_argument("source", help="the export: a .zip file or an unpacked directory")
    import_cmd.add_argument("dest", help="a new collection directory, or s3://bucket/prefix")

    pipeline = commands.add_parser("pipeline").add_subparsers(dest="action", required=True)
    run = pipeline.add_parser("run", help="run a pipeline once")
    run.add_argument("name")
    run.add_argument("--run-id", help="record the run under this id (set by the web app)")
    serve = pipeline.add_parser("serve", help="run pipelines on their schedules")
    serve.add_argument("names", nargs="*")
    pipeline.add_parser("status", help="step checkpoints and recent runs").add_argument("name")
    pipeline.add_parser("reset", help="forget step checkpoints (CDC offsets)").add_argument("name")
    pipeline.add_parser("seed", help="load the collection's data/ files into missing datasets")

    agent = commands.add_parser("agent").add_subparsers(dest="action", required=True)
    agent_serve = agent.add_parser("serve", help="serve agents over an OpenAI-compatible API")
    agent_serve.add_argument("--agent", action="append", dest="agents")
    agent_serve.add_argument("--host", default=os.environ.get("RUNNER_HOST", "127.0.0.1"))
    agent_serve.add_argument("--port", type=int, default=int(os.environ.get("RUNNER_PORT", "8000")))
    agent.add_parser("chat", help="chat with an agent in the terminal").add_argument("name")

    web = commands.add_parser(
        "web", help="serve the web UI and API over a directory of collections"
    )
    web.add_argument(
        "--collections",
        default=os.environ.get("RUNNER_COLLECTIONS"),
        help="where collections live: a directory or s3://bucket/prefix "
        "(default <data>/collections)",
    )
    web.add_argument("--host", default=os.environ.get("RUNNER_HOST", "127.0.0.1"))
    web.add_argument("--port", type=int, default=int(os.environ.get("RUNNER_PORT", "8080")))

    table = commands.add_parser("table").add_subparsers(dest="action", required=True)
    show = table.add_parser("show", help="print a dataset")
    show.add_argument("name")
    show.add_argument("--limit", type=int, default=20)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        if args.command == "collection":
            return _push(args.source, args.collection_dir)
        if args.command == "import":
            return _import(args.source, args.dest)
        if args.command == "web":
            return _web(args)
        if args.command == "secret":
            return _secret(args)
        collection = open_collection(
            args.collection_dir, secrets=collection_secrets(Path(args.data), _collection_name(args))
        )
        storage = Storage(args.tables or Path(args.data) / "tables")
        state = open_state(args.data, _collection_name(args))
        return _dispatch(args, collection, storage, state)
    except (CollectionError, KeyboardInterrupt) as exc:
        print(f"error: {exc}" if str(exc) else "interrupted", file=sys.stderr)
        return 1


def _dispatch(
    args: argparse.Namespace, collection: Collection, storage: Storage, state: State
) -> int:
    from pipeline.runner import PipelineError, PipelineRunner

    runner = PipelineRunner(collection, storage, state)
    if args.command == "check":
        return _check(collection, storage, runner)
    if args.command == "pipeline":
        if args.action == "run":
            try:
                print(json.dumps(runner.run(args.name, run_id=args.run_id)))
            except PipelineError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        elif args.action == "serve":
            from pipeline.scheduler import serve

            serve(runner, args.names or None)
        elif args.action == "status":
            report = {
                "steps": state.list_checkpoints(args.name),
                "runs": state.list_runs(args.name),
            }
            for step in report["steps"]:
                step.pop("stacktrace", None)
            print(json.dumps(report, indent=2, ensure_ascii=False))
        elif args.action == "reset":
            state.reset_checkpoints(args.name)
        elif args.action == "seed":
            print(json.dumps({"seeded": runner.seed()}))
        return 0
    if args.command == "agent":
        if args.action == "serve":
            import uvicorn
            from agent.server import create_app

            app = create_app(collection, state, args.agents, storage)
            uvicorn.run(app, host=args.host, port=args.port)
        else:
            _chat(collection, state, storage, args.name)
        return 0
    if args.command == "table":
        import polars as pl

        with pl.Config(tbl_rows=args.limit, tbl_cols=-1, fmt_str_lengths=60):
            print(storage.read(args.name).head(args.limit))
        return 0
    return 1


def _check(collection: Collection, storage: Storage, runner: object) -> int:
    from agent.runtime import Agent
    from commons.scripts import Resources

    failures = 0
    for doc in collection.list("pipeline"):
        try:
            runner.load(doc["name"])  # type: ignore[attr-defined]
            print(f"ok    pipeline {doc['name']}")
        except Exception as exc:  # report every problem, not just the first
            failures += 1
            print(f"FAIL  pipeline {doc['name']}: {exc}")
    resources = Resources(collection)
    for doc in collection.list("agent"):
        try:
            Agent(collection, resources, name=doc["name"], storage=storage)
            print(f"ok    agent {doc['name']}")
        except Exception as exc:
            failures += 1
            print(f"FAIL  agent {doc['name']}: {exc}")
    missing = missing_secrets(collection)
    if missing:
        print("secrets without a value: " + ", ".join(missing))
    return 1 if failures else 0


def _chat(collection: Collection, state: State, storage: Storage, name: str) -> None:
    from agent.runtime import Agent
    from commons.scripts import Resources

    agent = Agent(collection, Resources(collection), name=name, storage=storage)
    session_id = uuid.uuid4().hex[:12]
    session = agent.new_session()
    print(f"{agent.name} (session {session_id}) — empty line to quit")

    def show(kind: str, data: dict) -> None:
        if kind == "tool":
            print(f"  · {data['name']}({json.dumps(data['args'], ensure_ascii=False)})")

    messages: list[dict] = []
    while True:
        if not messages:
            text = input("> ").strip()
            if not text:
                return
            messages = [{"role": "user", "content": text}]
        result = agent.chat(session, messages, show)
        state.save_session(agent.name, session_id, session)
        messages = []
        if result["content"]:
            print(result["content"])
        for call in result["pending"]:
            answer = input(
                f"approve {call['name']}({json.dumps(call['args'], ensure_ascii=False)})? [y/N] "
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps({"approved": answer.strip().lower() == "y"}),
                }
            )


def _push(source: str, target: str) -> int:
    if not s3.is_s3(target):
        print(
            "error: --collection-dir / RUNNER_COLLECTION_DIR must be an s3:// location to push to",
            file=sys.stderr,
        )
        return 1
    Collection(source)  # refuse a directory that is not a collection
    count = s3.upload(Path(source), target)
    print(json.dumps({"uploaded": count, "collection": target}))
    return 0


def _import(source: str, dest: str) -> int:
    import tempfile

    from commons.importer import import_export

    try:
        if not s3.is_s3(dest):
            report = import_export(source, dest)
        else:
            with tempfile.TemporaryDirectory(prefix="dhub-runner-import-") as tmp:
                report = import_export(source, tmp)
                s3.upload(Path(tmp), dest)
    except ValueError as exc:  # not an export, or the destination is in use
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"collection": dest, **report.as_dict()}, ensure_ascii=False, indent=2))
    return 0


def _web(args: argparse.Namespace) -> int:
    import uvicorn
    from web.app import create_app
    from web.workspace import Workspace

    workspace = Workspace(
        collections=args.collections or str(Path(args.data) / "collections"),
        tables=args.tables or str(Path(args.data) / "tables"),
        data_dir=Path(args.data),
    )
    executor = pod_agents = None
    if os.environ.get("RUNNER_EXECUTOR") == "kubernetes":
        from web.kubernetes import KubernetesAgents, KubernetesConfig, KubernetesExecutor, load_api

        # Pods share collections and tables through RustFS, and secrets and state
        # through Postgres; a key generated per pod could not read the secrets.
        missing = [n for n in ("RUNNER_DATABASE_URL", "RUNNER_SECRET_KEY") if not os.environ.get(n)]
        if (
            missing
            or not workspace.collections.startswith("s3://")
            or not workspace.tables.startswith("s3://")
        ):
            raise SystemExit(
                "RUNNER_EXECUTOR=kubernetes needs RUNNER_COLLECTIONS/RUNNER_TABLES on s3:// "
                f"and {', '.join(missing) or 'the settings above'}"
            )
        config = KubernetesConfig.from_env()
        batch, apps, core = load_api()
        pod_agents = KubernetesAgents(workspace, config, apps, core)
        executor = KubernetesExecutor(workspace, config, batch)
    app = create_app(workspace, executor, pod_agents=pod_agents)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _collection_name(args: argparse.Namespace) -> str:
    return args.collection or str(args.collection_dir).rstrip("/").rsplit("/", 1)[-1]


def _secret(args: argparse.Namespace) -> int:
    import getpass

    store = open_store(Path(args.data), SHARED if args.shared else _collection_name(args))
    try:
        if args.action == "list":
            print(json.dumps(store.keys(), ensure_ascii=False, indent=2))
            return 0
        name, _, key = args.ref.partition("/")
        if args.action == "delete":
            store.delete(name, key or None)
            return 0
        if not key:
            print("error: use SECRET/KEY", file=sys.stderr)
            return 1
        value = getpass.getpass(f"{args.ref}: ") if sys.stdin.isatty() else sys.stdin.read()
        value = value.rstrip("\n")
        if not value:
            print("error: empty value", file=sys.stderr)
            return 1
        store.set(name, key, value)
        return 0
    except SecretStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
