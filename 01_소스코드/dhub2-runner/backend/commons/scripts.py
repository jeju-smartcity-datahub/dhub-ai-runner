"""User code: ``@use_*`` decorators, script connectors and package installs.

User code (pipeline steps, agent tools, connector scripts) is written for
dhub2. A ``@use_*`` decorator declares a resource that is injected
into the module as a global under the declared name::

    @use_connector("api", ref="my_openapi")
    @use_llm("llm", model="default")
    def run(options=None, contexts=None): ...

Supported here: use_connector, use_connectors, use_llm.
"""

from __future__ import annotations

import ast
import functools
import importlib.util
import inspect
import logging
import subprocess
import sys
import threading
import types
from collections.abc import Callable
from typing import Any

from .collection import Collection
from .drivers import driver_module
from .llm import create_chat_model, resolve_llm

log = logging.getLogger(__name__)

SUPPORTED = {"use_connector", "use_connectors", "use_llm"}


class ScriptError(Exception):
    pass


def scan_decorators(source: str) -> list[tuple[str, dict[str, Any]]]:
    """``[(decorator, {"name": ..., **kwargs})]`` for every ``@use_*(...)``."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Name)
                and deco.func.id.startswith("use_")
            ):
                continue
            try:
                args = [ast.literal_eval(a) for a in deco.args]
                kwargs = {k.arg: ast.literal_eval(k.value) for k in deco.keywords if k.arg}
            except ValueError as exc:
                raise ScriptError(f"@{deco.func.id}: arguments must be literals") from exc
            if args:
                kwargs["name"] = args[0]
            found.append((deco.func.id, kwargs))
    return found


class Connector:
    """A connector (its script, or a builtin driver) bound to the client its
    ``connect`` returned. ``connector.get(path, params)`` calls the module's
    ``get(client, path, params)``."""

    def __init__(self, name: str, module: types.ModuleType, options: dict[str, Any]):
        self._name = name
        self._module = module
        connect = getattr(self._module, "connect", None)
        if connect is None:
            self._client: Any = options
        elif len(inspect.signature(connect).parameters) >= 2:
            self._client = connect(options, None)
        else:
            self._client = connect(options)

    def __getattr__(self, attr: str) -> Callable[..., Any]:
        fn = getattr(self._module, attr, None)
        if attr.startswith("_") or attr == "connect" or not callable(fn):
            raise AttributeError(f"connector {self._name!r} has no function {attr!r}")
        return functools.partial(fn, self._client)

    def close(self) -> None:
        close = getattr(self._module, "close", None)
        if close is not None:
            close(self._client)


class ConnectorResolver:
    """What ``@use_connectors`` injects: ``get(name)`` opens a connector by name
    on demand (once per name), as dhub2's resolver does."""

    def __init__(self, resources: Resources):
        self._resources = resources
        self._opened: dict[str, Connector] = {}
        self._lock = threading.Lock()

    def get(self, name: str, collection: str | None = None) -> Connector:
        with self._lock:
            if name not in self._opened:
                self._opened[name] = self._resources.connector(name)
            return self._opened[name]

    def close(self) -> None:
        close_all(list(self._opened.values()))


# Functions a connector script of each type must define, as dhub2's type builders
# require them (health_check, patch, ... are optional there too).
SCRIPT_FUNCTIONS = {
    "db": ("connect", "query", "execute", "close"),
    "graph": ("connect", "query", "execute", "close"),
    "rest": ("connect", "get", "post", "put", "delete", "close"),
    "s3": ("connect", "read", "write", "list", "close"),
    "vector": ("connect", "search", "scroll", "upsert", "delete", "close"),
}


def _check_script_functions(ref: str, kind: str | None, module: types.ModuleType) -> None:
    required = SCRIPT_FUNCTIONS.get(kind or "")
    if required is None:
        raise ScriptError(
            f"connector {ref!r}: unknown connector type {kind!r}; types: {sorted(SCRIPT_FUNCTIONS)}"
        )
    missing = [f for f in required if not callable(getattr(module, f, None))]
    if missing:
        raise ScriptError(f"connector {ref!r}: the script does not define {missing}")


def script_module(name: str, script: str) -> types.ModuleType:
    module = types.ModuleType(f"connector_{name}")
    exec(compile(script, f"<connector:{name}>", "exec"), module.__dict__)
    return module


def resolve_refs(collection: Collection, options: Any) -> Any:
    """Options with every ``<key>_ref: secret://...`` (at any depth) also given as
    ``<key>`` holding the secret's value, as dhub2 hydrates them."""
    if isinstance(options, list):
        return [resolve_refs(collection, v) for v in options]
    if not isinstance(options, dict):
        return options
    resolved = {k: resolve_refs(collection, v) for k, v in options.items()}
    for key, value in options.items():
        if key.endswith("_ref") and isinstance(value, str) and value.startswith("secret://"):
            resolved[key[: -len("_ref")]] = collection.secret_value(value)
    return resolved


class Resources:
    """Resolves declared resources against a collection."""

    def __init__(self, collection: Collection, install_packages: bool = True):
        self.collection = collection
        self.install_packages = install_packages

    def namespace(
        self, source: str, bindings: list[dict[str, Any]] | None = None
    ) -> tuple[dict[str, Any], list[Any]]:
        """Globals to load ``source`` with, and the resources to close afterwards.

        ``bindings`` (a tool's ``function.decorators``) override the source's
        own ``@use_*`` declaration of the same decorator and name."""
        namespace: dict[str, Any] = {}
        closables: list[Any] = []
        declared = {(d, decl.get("name")): decl for d, decl in scan_decorators(source)}
        for binding in bindings or []:
            decorator = binding["decorator"]
            declared[(decorator, binding["name"])] = {
                "name": binding["name"],
                **(binding.get("arguments") or {}),
            }
        try:
            for (decorator, _), decl in declared.items():
                if decorator not in SUPPORTED:
                    raise ScriptError(f"@{decorator} is not supported by dhub2-runner")
                if "name" not in decl:
                    raise ScriptError(f"@{decorator} needs a name")
                resource = self._resolve(decorator, decl)
                namespace[decl["name"]] = resource
                if isinstance(resource, (Connector, ConnectorResolver)):
                    closables.append(resource)
                namespace[decorator] = _passthrough
        except BaseException:
            close_all(closables)
            raise
        return namespace, closables

    def _resolve(self, decorator: str, decl: dict[str, Any]) -> Any:
        if decorator == "use_connector":
            return self.connector(decl.get("ref") or decl["name"])
        if decorator == "use_connectors":
            return ConnectorResolver(self)
        if not decl.get("model"):
            raise ScriptError(f"@use_llm({decl['name']!r}) needs model='<llm name>'")
        options = {k: v for k, v in decl.items() if k not in ("name", "model")}
        return create_chat_model(resolve_llm(self.collection, decl["model"]), **options)

    def connector(self, ref: str) -> Connector:
        name, module, options = self.connector_parts(ref)
        return Connector(name, module, options)

    def connector_parts(self, ref: str) -> tuple[str, types.ModuleType, dict[str, Any]]:
        """A connector's name, module (its script or builtin driver) and resolved options."""
        doc = self.collection.get("connector", ref)
        name = doc.get("name") or ref
        if doc.get("credentials_env"):
            raise ScriptError(
                f"connector {ref!r}: credentials are not read from environment variables; "
                "use an <option>_ref: secret://<secret>/<key> option"
            )
        if doc.get("script"):
            self.ensure_packages(doc.get("packages"))
            module = script_module(name, doc["script"])
            _check_script_functions(ref, doc.get("type"), module)
        elif doc.get("driver"):
            try:
                module = driver_module(doc["driver"])
            except LookupError as exc:
                raise ScriptError(f"connector {ref!r}: {exc}") from exc
        else:
            raise ScriptError(f"connector {ref!r} has neither a script nor a driver")
        options = resolve_refs(
            self.collection, self.collection.expand(dict(doc.get("options") or {}))
        )
        return name, module, options

    def ensure_packages(self, packages: list[str] | None) -> None:
        missing = [p for p in packages or [] if not _importable(p)]
        if not missing:
            return
        if not self.install_packages:
            raise ScriptError(f"missing packages: {missing}")
        log.info("installing packages %s", missing)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", *missing], check=True)


def _importable(requirement: str) -> bool:
    name = requirement.split(";")[0]
    for sep in "<>=!~[ ":
        name = name.split(sep)[0]
    return importlib.util.find_spec(name.strip().replace("-", "_")) is not None


def _passthrough(*args: Any, **kwargs: Any) -> Callable[[Any], Any]:
    return lambda fn: fn


def load_function(source: str, name: str, namespace: dict[str, Any], label: str) -> Callable:
    module = types.ModuleType(label)
    module.__dict__.update(namespace)
    exec(compile(source, f"<{label}>", "exec"), module.__dict__)
    fn = getattr(module, name, None)
    if not callable(fn):
        raise ScriptError(f"{label}: function {name!r} not found")
    return fn


def close_all(closables: list[Any]) -> None:
    for item in closables:
        try:
            item.close()
        except Exception:  # closing is best effort
            log.warning("failed to close %r", item, exc_info=True)


def missing_secrets(collection: Collection) -> list[str]:
    """The secret keys the collection uses that have no value, as ``<secret>/<key>``."""
    return [
        f"{name}/{key}"
        for name, key in collection.secret_keys()
        if collection.secrets.get(name, key) is None
    ]
