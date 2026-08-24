"""Per-client API keys and R2 key scoping.

One deployment serves several clients (Harriet, nester-web, ...). A bearer token
resolves to a `Client`; the client optionally carries a `key_prefix` that every
object key and output prefix in a request must start with, so no client can read
or write another's objects.

Configuration (env):

    NESTER_API_KEYS="harriet:tok1:records/,web:tok2:web/"

        comma-separated `client_id:token[:key_prefix]` entries. The prefix is
        optional — omit it (`web:tok2`) for an unscoped client.

    NESTER_SERVICE_TOKEN="tok"

        LEGACY, kept so the currently deployed Harriet token keeps working
        before anyone touches the Render env: treated as client `harriet` with
        no prefix restriction. Entries in NESTER_API_KEYS win on `client_id`.

Fails CLOSED: with neither var configured every authenticated route returns 503.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from fastapi import Header, HTTPException

LEGACY_TOKEN_ENV = "NESTER_SERVICE_TOKEN"
LEGACY_CLIENT_ID = "harriet"
API_KEYS_ENV = "NESTER_API_KEYS"


@dataclass(frozen=True)
class Client:
    """One caller: who they are, their token, and the key prefix they're scoped to."""

    client_id: str
    token: str
    key_prefix: str = ""


def parse_api_keys(raw: str) -> List[Client]:
    """Parse `NESTER_API_KEYS`. Malformed / empty entries are skipped, not fatal."""
    clients: List[Client] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) < 2:
            continue  # no token -> unusable, ignore rather than half-configure
        client_id, token = parts[0].strip(), parts[1].strip()
        prefix = ":".join(parts[2:]).strip() if len(parts) > 2 else ""
        if not client_id or not token:
            continue
        clients.append(Client(client_id=client_id, token=token, key_prefix=prefix))
    return clients


def registry() -> List[Client]:
    """Every configured client, NESTER_API_KEYS first, then the legacy token.

    Read from the environment on each call (no caching) so a restart isn't
    needed to pick up a rotated key, and so tests can set it per-case.
    """
    clients = parse_api_keys(os.environ.get(API_KEYS_ENV, ""))
    legacy = os.environ.get(LEGACY_TOKEN_ENV)
    if legacy and not any(c.client_id == LEGACY_CLIENT_ID for c in clients):
        clients.append(Client(client_id=LEGACY_CLIENT_ID, token=legacy, key_prefix=""))
    return clients


def _bearer(authorization: Optional[str]) -> str:
    prefix = "Bearer "
    if authorization and authorization.startswith(prefix):
        return authorization[len(prefix):]
    return ""


def resolve(authorization: Optional[str]) -> Client:
    """Bearer token -> Client. 503 if nothing is configured, 401 if no match."""
    clients = registry()
    if not clients:
        raise HTTPException(status_code=503, detail="service token not configured")
    got = _bearer(authorization)
    match: Optional[Client] = None
    for client in clients:
        # Compare every entry (no early break) so timing doesn't leak position.
        if hmac.compare_digest(got, client.token) and match is None:
            match = client
    if match is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return match


def require_client(authorization: Optional[str] = Header(default=None)) -> Client:
    """FastAPI dependency: the authenticated client for this request."""
    return resolve(authorization)


def enforce_prefix(client: Client, keys: Iterable[Optional[str]]) -> None:
    """403 unless every supplied object key / out_prefix is inside the client's prefix.

    Keys are opaque strings — this is a scoping check, not an interpretation.
    """
    if not client.key_prefix:
        return
    for key in keys:
        if key is None:
            continue
        if not key.startswith(client.key_prefix):
            raise HTTPException(
                status_code=403,
                detail=f"key {key!r} outside client prefix {client.key_prefix!r}",
            )


def enforce_request_scope(client: Client, file_keys: Sequence[str],
                          out_prefix: Optional[str] = None) -> None:
    """Scope check for a whole request: every input key plus the output prefix."""
    enforce_prefix(client, list(file_keys) + [out_prefix])
