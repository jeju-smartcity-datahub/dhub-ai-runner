"""Turn a D.Hub for AI collection export into a runner collection.

A collection export is a ZIP laid out as::

    manifest.json                 {"entities": {"datasets": [...], "codes": [...], ...},
                                   "data_files": {...}, "manifests": [...]}
    collections/ datasets/ codes/(<name>.json + <name>.py) pipelines/
    ontologies/ folders/ sources/ views/ knowledges/
    manifests/*.json              definition-only resources, each with a "category"
                                  (agent, agent.tool, agent.actor, llm.model, prompt,
                                  connector, secret, ...)
    data/parquet/<dataset>.parquet, data/csv/<dataset>.csv, data/<dataset>.json

References inside an export are names, which is what a collection uses too. Secret
values are never exported: every ``secret://<secret>/<key>`` the export uses
becomes a ``secrets/*.json`` entry listing the keys to set in the secret store,
and an LLM model whose key was left out gets ``api_key_ref: secret://<model>/api_key``.
Categories the runner does not execute (ontologies, the semantic layer,
folders, knowledges, dashboards, ...) are reported and left out.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .collection import DATA_SUFFIXES, KINDS, Collection

# Collection directory per D.Hub for AI category.
CATEGORY_DIRS = {
    "dataset": "datasets",
    "code": "codes",
    "pipeline": "pipelines",
    "agent": "agents",
    "agent.tool": "tools",
    "agent.actor": "actors",
    "llm.model": "llm",
    "prompt": "prompts",
    "connector": "connectors",
    "secret": "secrets",
}
# Category of an export file that does not state one, by its directory.
DIR_CATEGORIES = {
    "collections": "collection",
    "folders": "folder",
    "datasets": "dataset",
    "sources": "source",
    "views": "view",
    "codes": "code",
    "pipelines": "pipeline",
    "ontologies/entities": "ontology.entity",
    "ontologies/relations": "ontology.relation",
    "ontologies/models": "ontology.model",
    "knowledges": "knowledge",
    "agents": "agent",
    "tools": "agent.tool",
    "actors": "agent.actor",
    "llm": "llm.model",
    "prompts": "prompt",
    "connectors": "connector",
    "secrets": "secret",
}
_SECRET_REF = re.compile(r"secret://([^/\s\"']+)/([A-Za-z0-9_.\-]+)")


@dataclass
class ImportReport:
    imported: dict[str, list[str]] = field(default_factory=dict)
    skipped: dict[str, list[str]] = field(default_factory=dict)
    data: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "imported": self.imported,
            "skipped": self.skipped,
            "data": self.data,
            "secrets": self.secrets,
        }

    def note_needs(self, dest: Path) -> None:
        """Record the secret keys the collection at ``dest`` needs, as ``<secret>/<key>``."""
        self.secrets = [f"{name}/{key}" for name, key in Collection(dest).secret_keys()]


def import_export(source: str | Path, dest: str | Path) -> ImportReport:
    """Write the export at ``source`` (a .zip or a directory) as a collection in
    ``dest``, which must not exist or be empty."""
    source, dest = Path(source), Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise ValueError(f"{dest} is not empty")
    with tempfile.TemporaryDirectory(prefix="dhub-runner-export-") as tmp:
        if source.is_file():
            with zipfile.ZipFile(source) as archive:
                archive.extractall(tmp)
            root = _export_root(Path(tmp))
        else:
            root = _export_root(source)
        report = _convert(root, dest)
    Collection(dest)  # what was written must load
    return report


def import_archive(data: bytes, dest: Path) -> ImportReport:
    """A .zip that is either a D.Hub for AI collection export or a runner collection."""
    with tempfile.TemporaryDirectory(prefix="dhub-runner-archive-") as tmp:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            archive.extractall(tmp)
        if any(Path(tmp).rglob("manifest.json")):
            return import_export(Path(tmp), dest)
        root = _collection_root(Path(tmp))
        shutil.copytree(root, dest, dirs_exist_ok=True)
    collection = Collection(dest)
    report = ImportReport(data=sorted(collection.data_files()))
    for kind in KINDS:
        names = [d["name"] for d in collection.list(kind)]
        if names:
            report.imported[kind] = names
    report.note_needs(dest)
    return report


def import_resources(files: list[tuple[str, bytes]], dest: Path, name: str) -> ImportReport:
    """Single resources as the D.Hub for AI API returns them (JSON with a
    ``category``), code bodies (``<code name>.py`` / ``.sql``) and data files
    (``<dataset>.csv`` / ``.json`` / ``.parquet``), written as a collection in ``dest``."""
    with tempfile.TemporaryDirectory(prefix="dhub-runner-resources-") as tmp:
        root = Path(tmp)
        for filename, content in files:
            path = Path(filename)
            if path.suffix == ".json":
                doc = json.loads(content)
                category = doc.get("category") if isinstance(doc, dict) else None
                if not category:
                    raise ValueError(f'{path.name}: a resource JSON needs its "category"')
                directory = "codes" if category == "code" else "manifests"
            elif path.suffix in (".py", ".sql"):
                directory = "codes"
            elif path.suffix in DATA_SUFFIXES:
                directory = "data"
            else:
                raise ValueError(
                    f"{path.name}: not a resource (.json), code (.py/.sql) or data file"
                )
            target = root / directory / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (root / "manifest.json").write_text(json.dumps({"name": name}), "utf-8")
        return _convert(root, dest)


def _collection_root(path: Path) -> Path:
    """The directory of a runner collection archive (the ZIP root or one level down)."""
    for candidate in [path, *sorted(p for p in path.iterdir() if p.is_dir())]:
        if any((candidate / sub).is_dir() for sub in KINDS.values()):
            return candidate
    raise ValueError("the archive is neither a collection export nor a runner collection")


def _export_root(path: Path) -> Path:
    """The directory holding manifest.json (the ZIP root or one level down)."""
    candidates = sorted(p.parent for p in path.rglob("manifest.json") if "__MACOSX" not in p.parts)
    if not candidates:
        raise ValueError(f"{path}: manifest.json not found; is this a collection export?")
    return min(candidates, key=lambda p: len(p.parts))


def _convert(root: Path, dest: Path) -> ImportReport:
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    report = ImportReport()
    dest.mkdir(parents=True, exist_ok=True)
    docs = _collect(root, manifest)

    secret_names = _secret_names(docs, manifest)
    for path, category, doc in docs:
        name = doc.get("name") or path.stem
        if category == "collection":
            _write_json(dest / "collection.json", doc)
            report.imported.setdefault(category, []).append(name)
            continue
        directory = CATEGORY_DIRS.get(category)
        if directory is None:
            report.skipped.setdefault(category, []).append(name)
            continue
        target = dest / directory / f"{_file_name(name)}.json"
        if category == "code":
            _copy_code_source(doc, path, target)
        elif category in ("agent.tool", "agent.actor"):
            _inline_tool_source(doc, path)
        elif category == "llm.model" and _needs_key(doc):
            # The export leaves keys out; the key becomes a secret named after the model.
            doc["api_key_ref"] = f"secret://{name}/api_key"
        elif category == "secret":
            doc.setdefault("keys", sorted(doc.get("values") or doc.get("key_names") or []))
        _write_json(target, doc)
        report.imported.setdefault(category, []).append(name)

    _write_secrets(dest, docs, secret_names, manifest)
    report.data = _copy_data(root, manifest, dest)
    report.note_needs(dest)
    return report


def _collect(root: Path, manifest: dict[str, Any]) -> list[tuple[Path, str, dict[str, Any]]]:
    """Every resource file of the export, with its category."""
    paths: dict[Path, str | None] = {}

    def listed(node: Any, keys: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                listed(child, (*keys, key))
        elif isinstance(node, list):
            category = DIR_CATEGORIES.get("/".join(keys))
            for rel in node:
                if isinstance(rel, str):
                    paths.setdefault(root / rel, category)

    listed(manifest.get("entities") or {}, ())
    for rel in manifest.get("manifests") or []:
        if isinstance(rel, str):
            paths.setdefault(root / rel, None)
    # Files the manifest does not list are still part of the export.
    for directory, category in DIR_CATEGORIES.items():
        for path in sorted((root / directory).glob("*.json")):
            paths.setdefault(path, category)
    for path in sorted((root / "manifests").glob("*.json")):
        paths.setdefault(path, None)

    docs = []
    for path, category in paths.items():
        if not path.exists():
            continue
        doc = json.loads(path.read_text("utf-8"))
        if not isinstance(doc, dict):
            continue
        if "schema_" in doc:
            doc.setdefault("schema", doc.pop("schema_"))
        category = doc.get("category") or category
        if category in (None, "ontology") and "ontologies" in path.parts:
            category = DIR_CATEGORIES.get("/".join(path.parts[-3:-1]))
        docs.append((path, category or "unknown", doc))
    return docs


def _needs_key(model: dict[str, Any]) -> bool:
    """A model whose key the export left out. ``has_api_key: false`` is a model
    D.Hub for AI calls without one (an OpenAI-compatible server that takes none)."""
    if model.get("api_key") or model.get("api_key_ref"):
        return False
    return model.get("has_api_key") is not False


def _file_name(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name).strip("._") or "unnamed"


def _write_json(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", "utf-8")


def _copy_code_source(doc: dict[str, Any], path: Path, target: Path) -> None:
    """Copy the code body next to its manifest. The portal export writes SQL
    code to <name>.sql; <name>.py is accepted for both."""
    sources = [path.with_suffix(s) for s in (".sql", ".py")]
    source = next((s for s in sources if s.exists()), None)
    if doc.get("content") or source is None:
        return
    suffix = ".sql" if doc.get("type") == "sql" else ".py"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target.with_suffix(suffix))


def _inline_tool_source(doc: dict[str, Any], path: Path) -> None:
    function = doc.get("function") or {}
    source = path.with_suffix(".py")
    if not function.get("source") and source.exists():
        function["source"] = source.read_text("utf-8")
        doc["function"] = function


def _secret_names(
    docs: list[tuple[Path, str, dict[str, Any]]], manifest: dict[str, Any]
) -> dict[str, str]:
    """``{secret id or name: secret name}`` from the exported secret manifests and
    any ``{id, name}`` pairs in the export's requirements checklist."""
    names = {}
    for _, category, doc in docs:
        if category == "secret" and doc.get("name"):
            names[doc["name"]] = doc["name"]
            if doc.get("id"):
                names[doc["id"]] = doc["name"]

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ident, name = node.get("id"), node.get("name")
            if isinstance(ident, str) and ident.startswith("secret.") and isinstance(name, str):
                names.setdefault(ident, name)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(manifest.get("requirements"))
    return names


def _write_secrets(
    dest: Path,
    docs: list[tuple[Path, str, dict[str, Any]]],
    names: dict[str, str],
    manifest: dict[str, Any],
) -> None:
    """A secrets/*.json per secret the export references or its requirements
    checklist lists, with the keys a value must be set for."""
    refs: dict[str, set[str]] = {}
    for _, _, doc in docs:
        for secret, key in _SECRET_REF.findall(json.dumps(doc)):
            refs.setdefault(secret, set()).add(key)
    requirements = manifest.get("requirements")
    listed = requirements.get("secrets") if isinstance(requirements, dict) else None
    for entry in listed if isinstance(listed, list) else []:
        secret = isinstance(entry, dict) and (entry.get("id") or entry.get("name"))
        if isinstance(secret, str):
            refs.setdefault(secret, set()).update(
                k for k in entry.get("keys") or [] if isinstance(k, str)
            )
    for secret, keys in refs.items():
        name = names.get(secret, secret)
        path = dest / "secrets" / f"{_file_name(name)}.json"
        doc = json.loads(path.read_text("utf-8")) if path.exists() else {"name": name}
        if secret != name:
            doc["id"] = secret
        doc["keys"] = sorted(set(doc.get("keys") or []) | keys)
        _write_json(path, doc)


def _copy_data(root: Path, manifest: dict[str, Any], dest: Path) -> list[str]:
    raw = manifest.get("data_files") or []
    listed = [f for files in raw.values() for f in files] if isinstance(raw, dict) else raw
    files = {root / f for f in listed if isinstance(f, str)}
    files |= {p for p in (root / "data").rglob("*") if p.suffix in DATA_SUFFIXES}
    copied = []
    for path in sorted(f for f in files if f.is_file()):
        inside = path.is_relative_to(root / "data")
        target = dest / "data" / (path.relative_to(root / "data") if inside else path.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        copied.append(path.stem)
    return copied
