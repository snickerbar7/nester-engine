"""Cloudflare R2 access via the S3 API (boto3).

R2 is S3-compatible. The service reads input CAD files by key and writes shop
artifacts by key. Harriet's key scheme (do not diverge):

    records/{companyId}/{recordId}/documents/{timestamp}-{uuid}.ext

Credentials come from the environment (a BUCKET-SCOPED R2 token, minted for this
service — never the account-wide one):

    R2_ACCOUNT_ID · R2_ACCESS_KEY_ID · R2_SECRET_ACCESS_KEY · R2_BUCKET_NAME
"""

from __future__ import annotations

import os
from functools import lru_cache

import boto3
from botocore.config import Config


class R2ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise R2ConfigError(f"missing required env var {name}")
    return val


@lru_cache(maxsize=1)
def _client():
    account_id = _require("R2_ACCOUNT_ID")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=_require("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_require("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
    )


def bucket() -> str:
    return _require("R2_BUCKET_NAME")


def get_bytes(key: str) -> bytes:
    """Download one object's bytes by key."""
    resp = _client().get_object(Bucket=bucket(), Key=key)
    return resp["Body"].read()


def put_bytes(key: str, data: bytes, content_type: str) -> None:
    """Upload bytes to a key (immutable, like Harriet's uploadService)."""
    _client().put_object(
        Bucket=bucket(),
        Key=key,
        Body=data,
        ContentType=content_type,
        CacheControl="public, max-age=31536000, immutable",
    )
