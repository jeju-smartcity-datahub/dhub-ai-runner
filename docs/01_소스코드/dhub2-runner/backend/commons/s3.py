"""S3-compatible object storage (RustFS) for collections and Delta tables.

Connection settings are the standard AWS variables, so the same values work
for boto3 and Delta Lake::

    AWS_ENDPOINT_URL=http://rustfs:9000
    AWS_ACCESS_KEY_ID=...  AWS_SECRET_ACCESS_KEY=...  AWS_REGION=us-east-1
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


def is_s3(location: str | Path) -> bool:
    return str(location).startswith("s3://")


def split(uri: str) -> tuple[str, str]:
    """``s3://bucket/a/b`` -> ``("bucket", "a/b")``."""
    bucket, _, prefix = uri.removeprefix("s3://").partition("/")
    return bucket, prefix.strip("/")


def client() -> Any:
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


def storage_options() -> dict[str, str]:
    """Delta Lake options for the configured endpoint."""
    options = {"AWS_REGION": os.environ.get("AWS_REGION", "us-east-1")}
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        if os.environ.get(key):
            options[key] = os.environ[key]
    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    if endpoint:
        options["AWS_ENDPOINT_URL"] = endpoint
        options["AWS_ALLOW_HTTP"] = "true" if endpoint.startswith("http://") else "false"
    # One pipeline-runner writes; without a lock service S3 commits need this.
    options["AWS_S3_ALLOW_UNSAFE_RENAME"] = "true"
    return options


def ensure_bucket(bucket: str) -> None:
    s3 = client()
    existing = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
    if bucket not in existing:
        s3.create_bucket(Bucket=bucket)


def download(uri: str, dest: Path) -> int:
    """Mirror every object under ``uri`` into ``dest`` (emptied first)."""
    bucket, prefix = split(uri)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    s3 = client()
    count = 0
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{prefix}/" if prefix else ""
    ):
        for item in page.get("Contents", []):
            relative = item["Key"][len(prefix) :].lstrip("/") if prefix else item["Key"]
            if not relative or relative.endswith("/"):
                continue
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, item["Key"], str(target))
            count += 1
    return count


def list_dirs(uri: str) -> list[str]:
    """The names directly under ``uri`` that hold objects (``s3://b/p/<name>/...``)."""
    bucket, prefix = split(uri)
    s3 = client()
    if bucket not in {b["Name"] for b in s3.list_buckets().get("Buckets", [])}:
        return []
    names = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{prefix}/" if prefix else "", Delimiter="/"
    ):
        names += [
            p["Prefix"].rstrip("/").rsplit("/", 1)[-1] for p in page.get("CommonPrefixes", [])
        ]
    return sorted(names)


def delete(uri: str) -> None:
    """Remove every object under ``uri``."""
    bucket, prefix = split(uri)
    s3 = client()
    stale = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{prefix}/" if prefix else ""
    ):
        stale += [{"Key": item["Key"]} for item in page.get("Contents", [])]
    for start in range(0, len(stale), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": stale[start : start + 1000]})


def upload(src: Path, uri: str) -> int:
    """Copy the files under ``src`` to ``uri``, replacing what was there."""
    bucket, prefix = split(uri)
    ensure_bucket(bucket)
    delete(uri)
    s3 = client()
    count = 0
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        key = "/".join(filter(None, [prefix, path.relative_to(src).as_posix()]))
        s3.upload_file(str(path), bucket, key)
        count += 1
    return count
