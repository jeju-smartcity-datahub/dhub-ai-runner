import shutil
import socket
import subprocess
import time

import pytest


class MemorySecretStore:
    """A SecretStore in a dict."""

    writable = True

    def __init__(self, values=None):
        self.values = {k: dict(v) for k, v in (values or {}).items()}

    def get(self, name, key):
        return self.values.get(name, {}).get(key)

    def keys(self):
        return {name: sorted(keys) for name, keys in self.values.items() if keys}

    def set(self, name, key, value):
        self.values.setdefault(name, {})[key] = value

    def delete(self, name, key=None):
        if key is None:
            self.values.pop(name, None)
        else:
            self.values.get(name, {}).pop(key, None)


# The examples' gemma4 model reads its endpoint and key from these secrets;
# tests replace the model itself, so any address will do.
EXAMPLE_SECRETS = {"dhub2_llm": {"base_url": "http://127.0.0.1:9/v1", "api_key": "test"}}


def example_secrets():
    return MemorySecretStore(EXAMPLE_SECRETS)


@pytest.fixture(scope="session")
def postgres_url(tmp_path_factory):
    """A throwaway local Postgres, when its binaries are installed."""
    import psycopg

    if not shutil.which("initdb") or not shutil.which("postgres"):
        pytest.skip("Postgres binaries are not installed")
    data = tmp_path_factory.mktemp("pg")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    subprocess.run(
        ["initdb", "-D", str(data), "-U", "runner", "--auth=trust"], check=True, capture_output=True
    )
    # TCP only: a pytest temp path is too long for a unix socket.
    server = subprocess.Popen(
        [
            "postgres",
            "-D",
            str(data),
            "-p",
            str(port),
            "-h",
            "127.0.0.1",
            "-c",
            "unix_socket_directories=",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"postgresql://runner@127.0.0.1:{port}/postgres"
    for _ in range(100):
        try:
            psycopg.connect(url).close()
            break
        except psycopg.OperationalError:
            time.sleep(0.1)
    else:
        server.kill()
        pytest.skip("local Postgres did not start")
    yield url
    server.terminate()
    server.wait(timeout=10)


# The rest of what dhub2 requires of a REST connector script, for test scripts
# that only exercise connect/get.
REST_VERBS = (
    "def post(client, path, body=None):\n    return None\n"
    "def put(client, path, body=None):\n    return None\n"
    "def delete(client, path):\n    return None\n"
    "def close(client):\n    pass\n"
)
