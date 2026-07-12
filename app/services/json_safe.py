"""Shared JSON-safety and canonical-hashing utilities.

Used by both `narrative_payload.py` (Phase 6A) and the chat services
(Phase 6B) to persist bounded backend data as immutable JSONB snapshots and
to derive stable, reproducible hashes from them.
"""

import hashlib
import json
import uuid
from datetime import date, datetime


def to_json_safe(value):
    """Recursively converts UUIDs and dates/datetimes to strings so a dict
    can be stored as JSONB and round-tripped through `json.dumps` without
    a custom encoder.
    """
    if isinstance(value, dict):
        return {key: to_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json_safe(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def hash_json(payload: dict) -> str:
    """SHA-256 of a canonicalized (sorted-key, compact) JSON encoding.
    `payload` must already be JSON-safe (see `to_json_safe`).
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
