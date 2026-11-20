"""A collection: D.Hub for AI resources exported as JSON files, referenced by name.

Layout::

    pipelines/*.json  codes/*.json (+ <name>.py|.sql)  connectors/*.json
    secrets/*.json    llm/*.json   datasets/*.json
    agents/*.json     tools/*.json actors/*.json        prompts/*.json

Secret values never live in a collection, nor in environment variables: they are
read from the collection's secret store (``commons.secrets``) by
``secret://<secret>/<key>`` references and ``${secret:<secret>/<key>}``
placeholders.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

from .secrets import ChainSecretStore, SecretStore

KINDS = {
    "pipeline": "pipelines",
    "code": "codes",
    "connector": "connectors",
    "secret": "secrets",
    "llm": "llm",
    "dataset": "datasets",
    "agent": "agents",
    "tool": "tools",
    "actor": "actors",
    "prompt": "prompts",
}

DATA_SUFFIXES = (".parquet", ".csv", ".json")
_SECRET_REF = re.compile(r"^(?:secret://)?([^/]+)/(.+)$")
_SECRET_USE = re.compile(r"(?:secret://|\$\{secret:)([^/\s\"'}]+)/([A-Za-z0-9_.\-]+)")
_PLACEHOLDER = re.compile(r"\$\{secret:([^}]+)\}")


class CollectionError(Exception):
    pass


class Collection:
    def __init__(self, root: str | Path, secrets: SecretStore | None = None):
        self.root = Path(root)
        # Where secret values are read: the collection's store (and the shared one).
        self.secrets = ChainSecretStore(*([secrets] if secrets else []))
        if not self.root.is_dir():
            raise CollectionError(f"collection directory not found: {self.root}")
        self._docs: dict[str, list[dict[str, Any]]] = {kind: [] for kind in KINDS}
        self._index: dict[str, dict[str, dict[str, Any]]] = {kind: {} for kind in KINDS}
        for kind, sub in KINDS.items():
            for path in sorted((self.root / sub).glob("*.json")):
                doc = json.loads(path.read_text("utf-8"))
                if not isinstance(doc, dict):
                    continue
                doc.setdefault("name", path.stem)
                self._attach_source(kind, doc, path)
                self._docs[kind].append(doc)
                # Manifests exported with ids are found by id as well as by name.
                for key in (path.stem, doc.get("id"), doc.get("name")):
                    if key:
                        self._index[kind][key] = doc

    def _attach_source(self, kind: str, doc: dict[str, Any], path: Path) -> None:
        """Inline the source file a resource points at (``content_file``,
        ``script_file``, ``source_file``, or a sibling file of the same stem)."""
        if kind == "code" and not doc.get("content"):
            source = self._read_file(doc.get("content_file"), path)
            # A D.Hub for AI export keeps every code body, SQL included, in <name>.py.
            for suffix in (".sql", ".py") if doc.get("type") == "sql" else (".py",):
                sibling = path.with_suffix(suffix)
                if source is None and sibling.exists():
                    source = sibling.read_text("utf-8")
            doc["content"] = source
        elif kind == "connector" and not doc.get("script"):
            doc["script"] = self._read_file(doc.get("script_file"), path)
        elif kind in ("tool", "actor"):
            function = doc.get("function") or {}
            if not function.get("source"):
                source = self._read_file(doc.get("source_file"), path)
                if source is None and path.with_suffix(".py").exists():
                    source = path.with_suffix(".py").read_text("utf-8")
                if source is not None:
                    function["source"] = source
                    doc["function"] = function
        elif kind == "prompt" and not doc.get("content"):
            doc["content"] = self._read_file(doc.get("content_file"), path)

    def _read_file(self, name: str | None, path: Path) -> str | None:
        if not name:
            return None
        for candidate in (self.root / name, path.parent / name):
            if candidate.exists():
                return candidate.read_text("utf-8")
        raise CollectionError(f"{path}: file {name!r} not found")

    def data_files(self) -> dict[str, Path]:
        """Seed data per dataset name: ``data/**/<name>.parquet|.csv|.json``
        (the export layout, e.g. ``data/parquet/<name>.parquet``)."""
        return {
            path.stem: path
            for path in sorted((self.root / "data").rglob("*"))
            if path.suffix in DATA_SUFFIXES and path.is_file()
        }

    def find(self, kind: str, ref: str | None) -> dict[str, Any] | None:
        return self._index[kind].get(ref) if ref else None

    def get(self, kind: str, ref: str | None) -> dict[str, Any]:
        doc = self.find(kind, ref)
        if doc is None:
            raise CollectionError(f"{kind} {ref!r} not found in collection {self.root}")
        return doc

    def list(self, kind: str) -> list[dict[str, Any]]:
        return list(self._docs[kind])

    # -- secrets -----------------------------------------------------------

    def secret_name(self, ref_name: str) -> str:
        """A secret's name, also when the reference uses its id."""
        return (self.find("secret", ref_name) or {}).get("name") or ref_name

    def secret_value(self, ref: str) -> str:
        """Resolve ``secret://<secret>/<key>``."""
        match = _SECRET_REF.match(ref)
        if not match:
            raise CollectionError(f"invalid secret reference {ref!r}")
        name, key = self.secret_name(match.group(1)), match.group(2)
        value = self.secrets.get(name, key)
        if value is None:
            raise CollectionError(
                f"secret {name}/{key} is not set: set it in the web UI's secrets "
                f"or with `dhub-runner secret set {name}/{key}`"
            )
        return value

    def secret_keys(self) -> list[tuple[str, str]]:
        """Every ``(secret, key)`` the collection declares or references."""
        pairs = {(doc["name"], key) for doc in self.list("secret") for key in doc.get("keys") or []}
        for kind in ("pipeline", "connector", "llm"):
            for doc in self.list(kind):
                for name, key in _SECRET_USE.findall(json.dumps(doc)):
                    pairs.add((self.secret_name(name), key))
        for doc in self.list("llm"):
            # A model D.Hub for AI deploys itself needs an endpoint in the runner.
            if doc.get("type") == "internal" or (doc.get("deployment") and not doc.get("base_url")):
                pairs.add((doc["name"], "base_url"))
        return sorted(pairs)

    def expand(self, value: Any) -> Any:
        """Replace ``${secret:<secret>/<key>}`` in strings."""
        if isinstance(value, str):
            return _PLACEHOLDER.sub(self._placeholder, value)
        if isinstance(value, dict):
            return {k: self.expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.expand(v) for v in value]
        return value

    def _placeholder(self, match: re.Match) -> str:
        return self.secret_value(match.group(1))


def open_collection(location: str | Path, secrets: SecretStore | None = None) -> Collection:
    """A collection from a local directory, or from an S3 prefix (RustFS) copied
    to a temporary directory first."""
    from . import s3

    if not s3.is_s3(location):
        return Collection(location, secrets)
    local = Path(tempfile.mkdtemp(prefix="dhub-runner-collection-"))
    if not s3.download(str(location), local):
        raise CollectionError(f"collection {location} is empty or does not exist")
    return Collection(local, secrets)
