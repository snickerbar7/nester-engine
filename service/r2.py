"""Cloudflare R2 access via the S3 API (boto3).

R2 is S3-compatible. The service reads input CAD files by key and writes shop
artifacts by key. Harriet's key scheme (do not diverge):

    records/{companyId}/{recordId}/documents/{timestamp}-{uuid}.ext

Bucket split (Harriet T13, 2026-07-15): `records/`-prefixed keys are PRIVATE and
live in a separate no-public-domain bucket; every other prefix (`catalog/`, the
fresh CAD drops this service reads) stays on the public bucket. The prefix→bucket
rule here mirrors Harriet's backend `r2Client.bucketForKey` exactly — keep them
in lockstep. With R2_PRIVATE_BUCKET_NAME unset, behavior is identical to the
pre-split single-bucket world (safe to deploy before the Doppler flip).

Credentials come from the environment (a BUCKET-SCOPED R2 token, minted for this
service — never the account-wide one; it must cover BOTH buckets):

    R2_ACCOUNT_ID · R2_ACCESS_KEY_ID · R2_SECRET_ACCESS_KEY · R2_BUCKET_NAME
    R2_PRIVATE_BUCKET_NAME (optional — the records/ bucket)
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


def private_bucket() -> str | None:
    """The records/ bucket — optional; unset means the pre-split single-bucket world."""
    return os.environ.get("R2_PRIVATE_BUCKET_NAME") or None


def bucket_for_key(key: str) -> str:
    """records/ keys → the private bucket (when configured); everything else → public.

    Mirrors Harriet backend `r2Client.bucketForKey` — keep in lockstep.
    """
    priv = private_bucket()
    if priv and key.startswith("records/"):
        return priv
    return bucket()


def get_bytes(key: str) -> bytes:
    """Download one object's bytes by key (bucket resolved by prefix)."""
    resp = _client().get_object(Bucket=bucket_for_key(key), Key=key)
    return resp["Body"].read()


def put_bytes(key: str, data: bytes, content_type: str) -> None:
    """Upload bytes to a key (bucket resolved by prefix).

    records/ artifacts are private-served through Harriet's gated routes — they get a
    private CacheControl; public-prefix objects keep the immutable-public header
    (like Harriet's uploadService).
    """
    private = key.startswith("records/") and private_bucket() is not None
    _client().put_object(
        Bucket=bucket_for_key(key),
        Key=key,
        Body=data,
        ContentType=content_type,
        CacheControl="private, max-age=0" if private else "public, max-age=31536000, immutable",
    )
