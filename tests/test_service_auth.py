"""Per-client API keys: registry parsing, token resolution, key scoping."""

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException

from service.core.auth import (
    Client,
    enforce_prefix,
    enforce_request_scope,
    parse_api_keys,
    registry,
    resolve,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("NESTER_API_KEYS", raising=False)
    monkeypatch.delenv("NESTER_SERVICE_TOKEN", raising=False)


# --- parsing --------------------------------------------------------------- #

def test_parse_two_clients_with_prefixes():
    clients = parse_api_keys("harriet:abc123:records/,web:def456:web/")
    assert clients == [
        Client("harriet", "abc123", "records/"),
        Client("web", "def456", "web/"),
    ]


def test_parse_prefix_optional_and_whitespace_tolerated():
    clients = parse_api_keys(" harriet:abc123 , web:def456:web/ ")
    assert clients[0] == Client("harriet", "abc123", "")
    assert clients[1] == Client("web", "def456", "web/")


def test_parse_skips_malformed_entries():
    # bare token with no client id, empty entry, and a lone id -> all unusable
    assert parse_api_keys("justatoken,,:tok,web:") == []


def test_parse_prefix_may_contain_colons():
    assert parse_api_keys("web:tok:a:b/")[0].key_prefix == "a:b/"


# --- registry -------------------------------------------------------------- #

def test_legacy_token_registers_as_harriet_unscoped(monkeypatch):
    monkeypatch.setenv("NESTER_SERVICE_TOKEN", "legacy-tok")
    assert registry() == [Client("harriet", "legacy-tok", "")]


def test_api_keys_entry_wins_over_legacy_for_same_client(monkeypatch):
    monkeypatch.setenv("NESTER_API_KEYS", "harriet:new-tok:records/")
    monkeypatch.setenv("NESTER_SERVICE_TOKEN", "legacy-tok")
    assert registry() == [Client("harriet", "new-tok", "records/")]


def test_legacy_kept_alongside_a_different_client(monkeypatch):
    monkeypatch.setenv("NESTER_API_KEYS", "web:web-tok:web/")
    monkeypatch.setenv("NESTER_SERVICE_TOKEN", "legacy-tok")
    assert registry() == [
        Client("web", "web-tok", "web/"),
        Client("harriet", "legacy-tok", ""),
    ]


# --- resolution ------------------------------------------------------------ #

def test_resolve_fails_closed_when_nothing_configured():
    with pytest.raises(HTTPException) as e:
        resolve("Bearer anything")
    assert e.value.status_code == 503


def test_resolve_maps_token_to_client(monkeypatch):
    monkeypatch.setenv("NESTER_API_KEYS", "harriet:tok-h:records/,web:tok-w:web/")
    assert resolve("Bearer tok-w").client_id == "web"
    assert resolve("Bearer tok-h").client_id == "harriet"


def test_resolve_rejects_wrong_or_missing_token(monkeypatch):
    monkeypatch.setenv("NESTER_API_KEYS", "harriet:tok-h")
    for header in ("Bearer nope", "tok-h", None, ""):
        with pytest.raises(HTTPException) as e:
            resolve(header)
        assert e.value.status_code == 401


# --- key scoping ----------------------------------------------------------- #

def test_prefix_enforced_on_keys_and_out_prefix():
    web = Client("web", "t", "web/")
    enforce_request_scope(web, ["web/a.igs", "web/b.igs"], "web/out/job1")
    with pytest.raises(HTTPException) as e:
        enforce_request_scope(web, ["records/co/a.igs"], "web/out")
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        enforce_request_scope(web, ["web/a.igs"], "records/co/out")
    assert e.value.status_code == 403


def test_unscoped_client_may_use_any_key():
    enforce_request_scope(Client("harriet", "t", ""), ["anything/at/all.igs"], "other/out")


def test_missing_out_prefix_is_not_a_violation():
    enforce_prefix(Client("web", "t", "web/"), ["web/a.igs", None])
