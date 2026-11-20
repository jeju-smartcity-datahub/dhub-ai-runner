"""Secret stores."""

import pytest
from commons.collection import Collection, CollectionError
from commons.secrets import (
    ChainSecretStore,
    FileSecretStore,
    PostgresSecretStore,
    SecretStoreError,
    master_key,
)
from cryptography.fernet import Fernet


def exercise(store) -> None:
    assert store.get("crm", "token") is None
    store.set("crm", "token", "t0k")
    store.set("crm", "user", "me")
    store.set("llm", "api_key", "k")
    assert store.get("crm", "token") == "t0k"
    assert store.keys() == {"crm": ["token", "user"], "llm": ["api_key"]}
    store.delete("crm", "token")
    assert store.keys() == {"crm": ["user"], "llm": ["api_key"]}
    store.delete("crm")
    assert store.keys() == {"llm": ["api_key"]}


def test_file_store_encrypts(tmp_path):
    key = Fernet.generate_key()
    store = FileSecretStore(tmp_path / "s.enc", key)
    exercise(store)
    assert b"api_key" not in (tmp_path / "s.enc").read_bytes()
    assert (tmp_path / "s.enc").stat().st_mode & 0o777 == 0o600
    with pytest.raises(SecretStoreError, match="cannot be decrypted"):
        FileSecretStore(tmp_path / "s.enc", Fernet.generate_key()).get("llm", "api_key")


def test_master_key(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNNER_SECRET_KEY", raising=False)
    first = master_key(tmp_path)
    assert master_key(tmp_path) == first
    assert (tmp_path / "secret.key").stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("RUNNER_SECRET_KEY", Fernet.generate_key().decode())
    assert master_key(tmp_path) != first


def test_chain_and_collection(tmp_path):
    (tmp_path / "b" / "secrets").mkdir(parents=True)
    (tmp_path / "b" / "secrets" / "crm.json").write_text(
        '{"name": "crm", "id": "secret.1", "keys": ["token"]}'
    )
    own = FileSecretStore(tmp_path / "own.enc", Fernet.generate_key())
    shared = FileSecretStore(tmp_path / "shared.enc", Fernet.generate_key())
    collection = Collection(tmp_path / "b", ChainSecretStore(own, shared))
    assert collection.secret_keys() == [("crm", "token")]
    with pytest.raises(CollectionError, match="crm/token is not set"):
        collection.secret_value("secret://crm/token")
    # The shared store is the fallback ...
    shared.set("crm", "token", "shared")
    assert collection.secret_value("secret://crm/token") == "shared"
    # ... the collection's own store wins, also for a reference by id.
    own.set("crm", "token", "own")
    assert collection.secret_value("secret://secret.1/token") == "own"
    # Environment variables are never read.
    assert Collection(tmp_path / "b").secrets.get("crm", "token") is None
    with pytest.raises(SecretStoreError):
        ChainSecretStore().set("a", "b", "c")


def test_postgres_store(postgres_url):
    import psycopg

    key = Fernet.generate_key()
    store = PostgresSecretStore(postgres_url, "demo", key)
    exercise(store)
    # Collections do not see each other's secrets.
    assert PostgresSecretStore(postgres_url, "other", key).keys() == {}
    # Only ciphertext reaches the database.
    with psycopg.connect(postgres_url) as db:
        value = db.execute("SELECT value FROM runner_secrets WHERE name = 'llm'").fetchone()[0]
    assert b"k" != bytes(value) and Fernet(key).decrypt(bytes(value)) == b"k"
