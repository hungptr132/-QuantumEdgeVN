"""Canonical serialisation and hashing — one implementation, used everywhere.

Three different things depend on hashes being stable across machines and runs:
  * `raw_document.content_hash`  — the ingest idempotency guarantee
  * `fact_sheet.payload_hash`    — detects whether a rebuild actually changed anything
  * `rule_spec_version`          — the content hash of the rule YAML, which drives
                                   regeneration when a threshold changes

If two call sites serialised differently, all three guarantees would quietly rot, so
there is exactly one canonical form here and nothing else may invent its own.
"""

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


def _default(obj: Any) -> str:
    """Stable text for types json cannot encode.

    Decimal becomes its exact string form, never a float — binary rounding would make
    the same number hash differently depending on how it was loaded.
    """
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, set | frozenset):
        return json.dumps(sorted(map(str, obj)), ensure_ascii=False)
    raise TypeError(f"no canonical form for {type(obj).__name__}")


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace, UTF-8 preserved.

    `ensure_ascii=False` keeps Vietnamese text readable in committed artifacts, which
    matters because those files are reviewed by eye.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_default,
    )


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_payload(value: Any) -> str:
    """Hash any JSON-compatible structure via its canonical form."""
    return sha256_hex(canonical_json(value))


def hash_file(path: Path) -> str:
    """Hash raw file bytes. Used for rule specs, so a threshold edit changes the version."""
    return sha256_hex(path.read_bytes())


def hash_files(paths: list[Path]) -> str:
    """Combined hash over several files, order-independent.

    Used for `rule_spec_version` across the whole rules directory: editing any one of
    them changes the result, which is what marks every derived row stale.
    """
    parts = sorted(f"{p.name}:{hash_file(p)}" for p in paths)
    return sha256_hex("|".join(parts))
