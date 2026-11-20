"""``http``: a generic REST connector (D.Hub for AI's builtin ``http`` driver).

Options: ``base_url``, ``auth_type`` (``none`` | ``basic`` | ``bearer`` |
``api_key`` | ``oauth2_client_credentials``), ``username``/``password``,
``token``, ``auth_in`` (``header`` | ``query``) / ``auth_name`` / ``api_key``,
``oauth_url``/``client_id``/``client_secret``/``scope``, ``headers``,
``timeout``, ``verify_ssl``. Credentials arrive resolved from their ``*_ref``
secrets.
"""

from __future__ import annotations

import time
from typing import Any

import httpx


def connect(options: dict[str, Any], collection: str | None = None) -> httpx.Client:
    auth_type = (options.get("auth_type") or "none").lower()
    headers = dict(options.get("headers") or {})
    params: dict[str, str] = {}
    auth: httpx.Auth | None = None
    if auth_type == "basic":
        auth = httpx.BasicAuth(_required(options, "username"), _required(options, "password"))
    elif auth_type == "bearer":
        headers["Authorization"] = f"Bearer {_required(options, 'token')}"
    elif auth_type == "api_key":
        name, key = _required(options, "auth_name"), _required(options, "api_key")
        if (options.get("auth_in") or "header").lower() == "query":
            params[name] = key
        else:
            headers[name] = key
    elif auth_type == "oauth2_client_credentials":
        auth = _ClientCredentials(options)
    elif auth_type != "none":
        raise ValueError(f"http connector: unknown auth_type {auth_type!r}")
    return httpx.Client(
        base_url=options.get("base_url") or "",
        headers=headers,
        params=params,
        auth=auth,
        timeout=options.get("timeout", 30),
        verify=options.get("verify_ssl", True),
    )


def _required(options: dict[str, Any], key: str) -> str:
    value = options.get(key)
    if not value:
        raise ValueError(f"http connector: option {key!r} is required for this auth_type")
    return value


class _ClientCredentials(httpx.Auth):
    """OAuth 2.0 client credentials, the token reused until shortly before it expires."""

    def __init__(self, options: dict[str, Any]):
        self.url = _required(options, "oauth_url")
        self.form = {
            "grant_type": "client_credentials",
            "client_id": _required(options, "client_id"),
            "client_secret": _required(options, "client_secret"),
        }
        if options.get("scope"):
            self.form["scope"] = options["scope"]
        self.token: tuple[str, float] | None = None

    def auth_flow(self, request: httpx.Request):
        if self.token is None or time.monotonic() > self.token[1]:
            response = yield httpx.Request("POST", self.url, data=self.form)
            response.read()
            response.raise_for_status()
            body = response.json()
            lifetime = float(body.get("expires_in") or 3600)
            self.token = (body["access_token"], time.monotonic() + max(lifetime - 30, 0))
        request.headers["Authorization"] = f"Bearer {self.token[0]}"
        yield request


def _body(response: httpx.Response, raw: bool = False) -> Any:
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    if raw:
        return response.content
    kind = response.headers.get("content-type", "")
    if "json" in kind:
        return response.json() if response.content else None
    if kind.startswith("text/") or "xml" in kind:
        return response.text
    return response.content


def request(
    client: httpx.Client,
    method: str,
    path: str,
    body: Any = None,
    params: dict[str, Any] | None = None,
    content: Any = None,
    headers: dict[str, str] | None = None,
    root: bool = False,
    raw: bool = False,
) -> Any:
    """Any request. ``body`` is sent as JSON, ``content`` as given; ``root``
    resolves ``path`` against the origin of ``base_url``."""
    if body is not None and content is not None:
        raise ValueError("pass either body (JSON) or content, not both")
    if root:
        base = client.base_url
        path = f"{base.scheme}://{base.netloc.decode()}/{path.lstrip('/')}"
    payload = {"json": body} if content is None else {"content": content}
    response = client.request(method.upper(), path, params=params, headers=headers, **payload)
    return _body(response, raw)


def get(
    client: httpx.Client, path: str, params: dict[str, Any] | None = None, raw: bool = False
) -> Any:
    return request(client, "GET", path, params=params, raw=raw)


def post(client: httpx.Client, path: str, body: Any = None) -> Any:
    return request(client, "POST", path, body)


def put(client: httpx.Client, path: str, body: Any = None) -> Any:
    return request(client, "PUT", path, body)


def patch(client: httpx.Client, path: str, body: Any = None) -> Any:
    return request(client, "PATCH", path, body)


def delete(client: httpx.Client, path: str) -> Any:
    return request(client, "DELETE", path)


def close(client: httpx.Client) -> None:
    client.close()
