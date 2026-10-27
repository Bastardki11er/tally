"""API clients and their keys.

A key is shown once, when it's created, and only its SHA-256 is stored. Keys are 256
random bits, not passwords, so a fast hash is the right one: there's nothing to brute
force, and every request can afford the lookup. A client may hold several keys at a
time, which is how a key is rotated without downtime: create the new one, move the
callers over, revoke the old one.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from psycopg import Connection

from .errors import conflict, not_found

KEY_PREFIX = "tly_"


@dataclass(frozen=True)
class Actor:
    """Who is making a request: the client whose books it may touch, and the key it used."""

    client_id: int
    key_id: int


def hash_key(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()


def find_key(conn: Connection, key: str, request_id: str | None = None) -> Actor | None:
    """The key's client, or None if the key is unknown or revoked.

    It also tells the database who is acting, for the rest of this transaction only: the
    audit triggers (migration 006) read tally.api_key_id and tally.request_id.
    """
    row = conn.execute(
        "SELECT id, client_id, set_config('tally.api_key_id', id::text, true), "
        "set_config('tally.request_id', coalesce(%s, ''), true) "
        "FROM api_keys WHERE key_hash = %s AND revoked_at IS NULL",
        (request_id, hash_key(key)),
    ).fetchone()
    return Actor(client_id=row["client_id"], key_id=row["id"]) if row else None


def create_key(conn: Connection, client_name: str) -> tuple[dict, str]:
    """Issues a new key for an existing client. Returns the key's record and the key itself."""
    client = conn.execute("SELECT id FROM api_clients WHERE name = %s", (client_name,)).fetchone()
    if client is None:
        raise not_found("CLIENT_NOT_FOUND", f"No client named {client_name!r}")
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    record = conn.execute(
        "INSERT INTO api_keys (client_id, key_hash, key_prefix) VALUES (%s, %s, %s) "
        "RETURNING id, client_id, key_prefix, created_at",
        (client["id"], hash_key(key), key[: len(KEY_PREFIX) + 6]),
    ).fetchone()
    return record, key


def create_client(conn: Connection, name: str) -> tuple[dict, str]:
    """A new client with its first key."""
    if conn.execute("SELECT 1 FROM api_clients WHERE name = %s", (name,)).fetchone():
        raise conflict("CLIENT_EXISTS", f"A client named {name!r} already exists")
    conn.execute("INSERT INTO api_clients (name) VALUES (%s)", (name,))
    return create_key(conn, name)


def revoke_key(conn: Connection, key_id: int) -> dict:
    record = conn.execute(
        "UPDATE api_keys SET revoked_at = coalesce(revoked_at, now()) WHERE id = %s "
        "RETURNING id, client_id, key_prefix, revoked_at",
        (key_id,),
    ).fetchone()
    if record is None:
        raise not_found("KEY_NOT_FOUND", f"No API key with id {key_id}")
    return record


def list_keys(conn: Connection) -> list[dict]:
    return conn.execute(
        "SELECT k.id, c.name AS client, k.key_prefix, k.created_at, k.revoked_at "
        "FROM api_keys k JOIN api_clients c ON c.id = k.client_id ORDER BY k.id"
    ).fetchall()
