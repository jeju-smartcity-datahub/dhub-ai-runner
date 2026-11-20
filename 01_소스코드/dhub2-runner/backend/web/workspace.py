"""The collections the web app serves, and everything opened for each of them.

Collections live under one root, a local directory or an S3 prefix on RustFS
(``<root>/<collection>/pipelines/...``). Each collection gets its own tables
(``<tables>/<collection>/<dataset>``) and its own state directory
(``<data>/state/<collection>``: the SQLite state file, dataset locks and, without
Postgres, the encrypted secret store).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.server import AgentPool
from commons import s3
from commons.collection import Collection, open_collection
from commons.catalog import Catalog, open_catalog
from commons.importer import ImportReport, import_archive, import_resources
from commons.secrets import SHARED, ChainSecretStore, SecretStore, open_store
from commons.state import PostgresState, State, open_state
from commons.storage import Storage
from pipeline.runner import PipelineRunner

log = logging.getLogger(__name__)

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass
class Opened:
    collection: Collection
    storage: Storage
    state: State | PostgresState
    runner: PipelineRunner
    agents: AgentPool


class Workspace:
    def __init__(
        self,
        collections: str,
        tables: str,
        data_dir: Path,
        secret_store: Callable[[str], SecretStore] | None = None,
    ):
        """``secret_store`` builds the store for one collection (or ``SHARED``). The
        default keeps secrets in Postgres when RUNNER_DATABASE_URL is set, else
        in encrypted files under ``data_dir`` (``commons.secrets.open_store``)."""
        self.collections = str(collections).rstrip("/")
        self.tables = str(tables).rstrip("/")
        self.data_dir = Path(data_dir)
        if not s3.is_s3(self.collections):
            Path(self.collections).mkdir(parents=True, exist_ok=True)
        self._opened: dict[str, Opened] = {}
        self._stores: dict[str, SecretStore] = {}
        self._secret_store = secret_store or (lambda name: open_store(self.data_dir, name))
        # The dataset catalog, in Postgres (RUNNER_DATABASE_URL); None without it.
        self.catalog: Catalog | None = open_catalog()
        self._lock = threading.RLock()
        # Called with a collection name (or None for all) when its definitions or secrets change.
        self.listeners: list[Callable[[str | None], None]] = []

    # -- collections -------------------------------------------------------------

    def names(self) -> list[str]:
        if s3.is_s3(self.collections):
            return s3.list_dirs(self.collections)
        return sorted(p.name for p in Path(self.collections).iterdir() if p.is_dir())

    def check_name(self, name: str) -> str:
        if not NAME.match(name):
            raise ValueError(
                f"invalid collection name {name!r}: use letters, digits, '_', '-', '.'"
            )
        return name

    def location(self, name: str) -> str:
        return f"{self.collections}/{self.check_name(name)}"

    def state_dir(self, name: str) -> Path:
        return self.data_dir / "state" / self.check_name(name)

    def open(self, name: str) -> Opened:
        with self._lock:
            if name not in self._opened:
                if name not in self.names():
                    raise KeyError(name)
                collection = open_collection(
                    self.location(name),
                    secrets=ChainSecretStore(self.secret_store(name), self.secret_store(SHARED)),
                )
                storage = Storage(f"{self.tables}/{name}")
                state = open_state(self.data_dir, name)
                agents = AgentPool(collection, state, storage=storage)
                runner = PipelineRunner(collection, storage, state)
                self._opened[name] = Opened(collection, storage, state, runner, agents)
            return self._opened[name]

    def invalidate(self, name: str | None) -> None:
        """Drop what was opened for a collection (all collections for None) so the next
        use reads its definitions and secrets again, and tell the listeners
        (e.g. agent pods to restart)."""
        if name is None:
            for opened_name in list(self._opened):
                self.invalidate(opened_name)
            for listener in self.listeners:
                listener(None)
            return
        with self._lock:
            opened = self._opened.pop(name, None)
        for listener in self.listeners:
            listener(name)
        if opened and s3.is_s3(self.collections):
            shutil.rmtree(opened.collection.root, ignore_errors=True)

    def import_files(
        self, name: str, files: list[tuple[str, bytes]], replace: bool = False
    ) -> ImportReport:
        """A collection export or collection (.zip) creates or replaces the collection;
        resource files are added to it."""
        self.check_name(name)
        exists = name in self.names()
        archives = [f for f in files if f[0].lower().endswith(".zip")]
        if archives and len(files) != 1:
            raise ValueError("upload one .zip at a time, or resource files without a .zip")
        if archives and exists and not replace:
            raise FileExistsError(f"collection {name!r} already exists")
        with tempfile.TemporaryDirectory(prefix="dhub2-web-import-") as tmp:
            target = Path(tmp) / "collection"
            if archives:
                report = import_archive(archives[0][1], target)
            else:
                if exists:
                    self._copy_out(name, target)
                added = Path(tmp) / "added"
                report = import_resources(files, added, name)
                shutil.copytree(added, target, dirs_exist_ok=True)
                report.note_needs(target)
            Collection(target)  # what is published must load
            self._publish(name, target)
        self.invalidate(name)
        self.sync_catalog(name)
        return report

    def delete(self, name: str) -> None:
        """Remove the collection with its tables, state (run history, sessions) and
        secrets, so a later import under the same name starts clean."""
        self.invalidate(name)
        state = open_state(self.data_dir, name)
        if isinstance(state, PostgresState):
            state.delete_all()
        if self.catalog is not None:
            self.catalog.forget(name)
        store = self.secret_store(name)
        for secret in store.keys():
            store.delete(secret)
        for location in (self.location(name), f"{self.tables}/{name}"):
            if s3.is_s3(location):
                s3.delete(location)
            else:
                shutil.rmtree(location, ignore_errors=True)
        shutil.rmtree(self.state_dir(name), ignore_errors=True)
        with self._lock:
            self._stores.pop(name, None)

    def _copy_out(self, name: str, target: Path) -> None:
        location = self.location(name)
        if s3.is_s3(location):
            s3.download(location, target)
        else:
            shutil.copytree(location, target, dirs_exist_ok=True)

    def _publish(self, name: str, source: Path) -> None:
        location = self.location(name)
        if s3.is_s3(location):
            s3.upload(source, location)
        else:
            shutil.rmtree(location, ignore_errors=True)
            shutil.copytree(source, location)

    # -- catalog ---------------------------------------------------------------

    def sync_catalog(self, name: str | None = None, force: bool = False) -> list[dict[str, Any]]:
        """Bring the catalog up to date with one collection's datasets (every
        collection for None, forgetting the ones that are gone). A collection that
        cannot be opened or read is logged and reported, not raised."""
        if self.catalog is None:
            return []
        names = [name] if name else self.names()
        results = []
        for collection_name in names:
            try:
                o = self.open(collection_name)
                alias = collection_info(o.collection).get("alias")
                results.append(
                    self.catalog.sync(collection_name, o.collection, o.storage, alias, force)
                )
            except Exception as exc:  # one broken collection must not stop the others
                log.exception("catalog sync of %s failed", collection_name)
                results.append({"collection": collection_name, "error": str(exc)})
        if name is None:
            for gone in set(self.catalog.collections()) - set(names):
                self.catalog.forget(gone)
        return results

    # -- secrets ---------------------------------------------------------------

    def secret_store(self, name: str) -> SecretStore:
        """A collection's own store, or the shared one for ``SHARED``."""
        with self._lock:
            if name not in self._stores:
                self._stores[name] = self._secret_store(
                    name if name == SHARED else self.check_name(name)
                )
            return self._stores[name]

    def set_secrets(self, name: str, values: dict[tuple[str, str], str | None]) -> None:
        """Set (or, with None or "", remove) secret keys for one collection, or the
        shared ones for ``SHARED``."""
        store = self.secret_store(name)
        for (secret, key), value in values.items():
            if value:
                store.set(secret, key, value)
            else:
                store.delete(secret, key)
        self.invalidate(None if name == SHARED else name)

    def secret_status(self, name: str) -> list[dict[str, Any]]:
        """For each secret key the collection uses or its store holds: where the
        value comes from (``settings``, ``shared`` or None), never the value."""
        collection = self.open(name).collection
        own, shared = self.secret_store(name), self.secret_store(SHARED)
        pairs = set(collection.secret_keys())
        pairs |= {(s, k) for s, keys in own.keys().items() for k in keys}
        status = []
        for secret, key in sorted(pairs):
            source = (
                "settings"
                if own.get(secret, key) is not None
                else "shared"
                if shared.get(secret, key) is not None
                else None
            )
            status.append({"secret": secret, "key": key, "source": source})
        return status

    def shared(self) -> list[dict[str, str]]:
        """The shared secret keys (never values)."""
        return [
            {"secret": s, "key": k}
            for s, keys in sorted(self.secret_store(SHARED).keys().items())
            for k in keys
        ]


def collection_info(collection: Collection) -> dict[str, Any]:
    """The collection's ``collection.json`` (alias, description), if it has one."""
    path = collection.root / "collection.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else {}
