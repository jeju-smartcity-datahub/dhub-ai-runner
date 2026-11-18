def connect(options, collection=None):
    import httpx

    return httpx.Client(base_url=options["base_url"], timeout=options.get("timeout", 30))


def _send(client, method, path, params=None, body=None):
    resp = client.request(method, path.lstrip("/"), params=params, json=body)
    resp.raise_for_status()
    return resp.json() if resp.content else None


def get(client, path, params=None):
    return _send(client, "GET", path, params=params)


def post(client, path, body=None):
    return _send(client, "POST", path, body=body)


def put(client, path, body=None):
    return _send(client, "PUT", path, body=body)


def delete(client, path):
    return _send(client, "DELETE", path)


def close(client):
    client.close()
