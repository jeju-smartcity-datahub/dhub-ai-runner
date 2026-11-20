"""Where secret values are kept.

A secret is addressed as in dhub2: a name and a key
(``secret://<name>/<key>``). Collections never hold values; a ``SecretStore``
does. One store serves one collection.

- ``FileSecretStore``: a file encrypted with a master key (Fernet), the local default.
- ``PostgresSecretStore``: rows in Postgres, values encrypted with the same master
  key; for several runner processes or pods that share secrets.
- ``ChainSecretStore``: the first store that has a value wins; writes go to the
  first writable store.

A Kubernetes store (one k8s Secret per collection) fits the same interface.

Secrets are never passed in environment variables. The only variables the
runner reads are its own infrastructure: RustFS (``AWS_*``), Postgres
(``RUNNER_DATABASE_URL``) and the master key (``RUNNER_SECRET_KEY``).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken


class SecretStoreError(Exception):
    pass


class SecretStore(Protocol):
    writable: bool

    def get(self, name: str, key: str) -> str | None:
        """The value, or None when this store does not have it."""
        ...

    def keys(self) -> dict[str, list[str]]:
        """``{secret name: [keys]}`` this store holds a value for (never values)."""
        ...

    def set(self, name: str, key: str, value: str) -> None: ...

    def delete(self, name: str, key: str | None = None) -> None:
        """Remove one key, or the whole secret when ``key`` is None."""
        ...


class FileSecretStore:
    """``{name: {key: value}}`` in one file encrypted with a Fernet key. The
    file and the key file it may create are readable by the owner only."""

    writable = True

    def __init__(self, path: Path, master_key: bytes):
        self.path = Path(path)
        try:
            self._fernet = Fernet(master_key)
        except ValueError as exc:
            raise SecretStoreError("the master key is not a valid Fernet key") from exc
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self._fernet.decrypt(self.path.read_bytes()))
        except InvalidToken as exc:
            raise SecretStoreError(
                f"{self.path} cannot be decrypted with this master key (RUNNER_SECRET_KEY)"
            ) from exc

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(self.path, self._fernet.encrypt(json.dumps(data).encode()))

    def get(self, name: str, key: str) -> str | None:
        return self._load().get(name, {}).get(key)

    def keys(self) -> dict[str, list[str]]:
        return {name: sorted(values) for name, values in self._load().items()}

    def set(self, name: str, key: str, value: str) -> None:
        with self._lock:
            data = self._load()
            data.setdefault(name, {})[key] = value
            self._save(data)

    def delete(self, name: str, key: str | None = None) -> None:
        with self._lock:
            data = self._load()
            if key is None:
                data.pop(name, None)
            else:
                data.get(name, {}).pop(key, None)
                if not data.get(name):
                    data.pop(name, None)
            self._save(data)


class PostgresSecretStore:
    """``runner_secrets(collection, name, key, value)`` in Postgres; ``value`` is
    encrypted with the master key, so the database never holds plain values."""

    writable = True

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS runner_secrets (
        collection TEXT NOT NULL,
        name TEXT NOT NULL,
        key TEXT NOT NULL,
        value BYTEA NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (collection, name, key)
    )
    """

    def __init__(self, url: str, collection: str, master_key: bytes):
        self.url = url
        self.collection = collection
        try:
            self._fernet = Fernet(master_key)
        except ValueError as exc:
            raise SecretStoreError("the master key is not a valid Fernet key") from exc
        with self._connect() as db:
            db.execute(self._SCHEMA)

    def _connect(self):
        import psycopg

        return psycopg.connect(self.url, autocommit=True)

    def get(self, name: str, key: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT value FROM runner_secrets WHERE collection = %s AND name = %s AND key = %s",
                (self.collection, name, key),
            ).fetchone()
        if row is None:
            return None
        try:
            return self._fernet.decrypt(bytes(row[0])).decode()
        except InvalidToken as exc:
            raise SecretStoreError(
                f"secret {name}/{key} cannot be decrypted with this master key (RUNNER_SECRET_KEY)"
            ) from exc

    def keys(self) -> dict[str, list[str]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT name, key FROM runner_secrets WHERE collection = %s ORDER BY name, key",
                (self.collection,),
            ).fetchall()
        keys: dict[str, list[str]] = {}
        for name, key in rows:
            keys.setdefault(name, []).append(key)
        return keys

    def set(self, name: str, key: str, value: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO runner_secrets (collection, name, key, value) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (collection, name, key) DO UPDATE "
                "SET value = excluded.value, updated_at = now()",
                (self.collection, name, key, self._fernet.encrypt(value.encode())),
            )

    def delete(self, name: str, key: str | None = None) -> None:
        with self._connect() as db:
            if key is None:
                db.execute(
                    "DELETE FROM runner_secrets WHERE collection = %s AND name = %s",
                    (self.collection, name),
                )
            else:
                db.execute(
                    "DELETE FROM runner_secrets WHERE collection = %s AND name = %s AND key = %s",
                    (self.collection, name, key),
                )


class ChainSecretStore:
    def __init__(self, *stores: SecretStore):
        self.stores = stores
        self.writable = any(s.writable for s in stores)

    def get(self, name: str, key: str) -> str | None:
        for store in self.stores:
            value = store.get(name, key)
            if value is not None:
                return value
        return None

    def source(self, name: str, key: str) -> int | None:
        """Index of the store the value comes from."""
        return next((i for i, s in enumerate(self.stores) if s.get(name, key) is not None), None)

    def keys(self) -> dict[str, list[str]]:
        merged: dict[str, set[str]] = {}
        for store in self.stores:
            for name, keys in store.keys().items():
                merged.setdefault(name, set()).update(keys)
        return {name: sorted(keys) for name, keys in merged.items()}

    def _writable(self) -> SecretStore:
        store = next((s for s in self.stores if s.writable), None)
        if store is None:
            raise SecretStoreError("no writable secret store")
        return store

    def set(self, name: str, key: str, value: str) -> None:
        self._writable().set(name, key, value)

    def delete(self, name: str, key: str | None = None) -> None:
        self._writable().delete(name, key)


# The store every collection falls back to (a collection's own values win).
SHARED = "__shared__"


def open_store(data_dir: Path, collection: str) -> SecretStore:
    """A collection's store, or the shared one for ``SHARED``: rows in Postgres
    when RUNNER_DATABASE_URL is set, else an encrypted file under ``data_dir``."""
    key = master_key(data_dir)
    url = os.environ.get("RUNNER_DATABASE_URL")
    if url:
        return PostgresSecretStore(url, collection, key)
    if collection == SHARED:
        return FileSecretStore(Path(data_dir) / "shared" / "secrets.enc", key)
    return FileSecretStore(Path(data_dir) / "state" / collection / "secrets.enc", key)


def collection_secrets(data_dir: Path, collection: str) -> ChainSecretStore:
    """What a collection reads: its own store, then the shared one."""
    return ChainSecretStore(open_store(data_dir, collection), open_store(data_dir, SHARED))


def master_key(data_dir: Path) -> bytes:
    """``RUNNER_SECRET_KEY``, or a key generated once into ``<data>/secret.key``."""
    if os.environ.get("RUNNER_SECRET_KEY"):
        return os.environ["RUNNER_SECRET_KEY"].encode()
    path = Path(data_dir) / "secret.key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(path, Fernet.generate_key())
    return path.read_bytes().strip()


def _write_private(path: Path, content: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
    os.replace(tmp, path)
