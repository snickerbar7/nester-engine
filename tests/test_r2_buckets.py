"""Bucket routing (Harriet T13 + caller-specified pair) survived the restructure.

The rules under test mirror Harriet backend `r2Client.bucketForKey`:
records/-prefixed keys go to the private bucket when one exists; a
caller-supplied request-scoped pair wins over this service's env pair.
"""
import pytest

pytest.importorskip("boto3")

from service.core import r2


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("R2_BUCKET_NAME", "pub-env")
    monkeypatch.delenv("R2_PRIVATE_BUCKET_NAME", raising=False)
    tok = r2.caller_buckets.set(None)
    yield
    r2.caller_buckets.reset(tok)


def test_env_single_bucket_pre_split():
    assert r2.bucket_for_key("records/a/b.pdf") == "pub-env"
    assert r2.bucket_for_key("catalog/x.igs") == "pub-env"


def test_env_private_bucket_routes_records(monkeypatch):
    monkeypatch.setenv("R2_PRIVATE_BUCKET_NAME", "priv-env")
    assert r2.bucket_for_key("records/a/b.pdf") == "priv-env"
    assert r2.bucket_for_key("catalog/x.igs") == "pub-env"


def test_caller_pair_wins_over_env(monkeypatch):
    monkeypatch.setenv("R2_PRIVATE_BUCKET_NAME", "priv-env")
    r2.caller_buckets.set({"public": "pub-caller", "private": "priv-caller"})
    assert r2.bucket_for_key("records/a/b.pdf") == "priv-caller"
    assert r2.bucket_for_key("catalog/x.igs") == "pub-caller"


def test_caller_pair_without_private():
    r2.caller_buckets.set({"public": "pub-caller", "private": None})
    assert r2.bucket_for_key("records/a/b.pdf") == "pub-caller"


def test_harriet_requests_accept_buckets_field():
    from service.harriet.routes import ExtractRequest, NestRequest
    req = ExtractRequest(files=[{"key": "k", "filename": "f.igs"}],
                         buckets={"public": "p", "private": "q"})
    assert req.buckets.public == "p"
    req2 = NestRequest(files=[{"key": "k", "filename": "f.igs"}])
    assert req2.buckets is None
