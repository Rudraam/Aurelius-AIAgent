"""Canonical bytes for inputs, policies and plans (invariant 2, spec section 8).

Canonical JSON: keys sorted, no whitespace, UTF-8, Decimals as their exact strings, dates ISO. The
same inputs and policy always give the same bytes, so the same hash. A stored snapshot is loaded
back with floats refused, and recomputing from it must reproduce the stored plan byte for byte.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.integrations.json_decimal import assert_no_floats
from app.ledger.models import LedgerInputs
from app.ledger.plan import Plan
from app.ledger.policy import AllocationPolicy

SNAPSHOT_SCHEMA = 1


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _refuse_float(text: str) -> Any:
    raise ValueError(f"float {text} in a ledger snapshot; amounts are strings")


def snapshot(inputs: LedgerInputs, policy: AllocationPolicy) -> bytes:
    return _canonical(
        {
            "schema": SNAPSHOT_SCHEMA,
            "inputs": inputs.model_dump(mode="json"),
            "policy": policy.model_dump(mode="json"),
        }
    )


def snapshot_hash(inputs: LedgerInputs, policy: AllocationPolicy) -> str:
    return hashlib.sha256(snapshot(inputs, policy)).hexdigest()


def load_snapshot(raw: bytes) -> tuple[LedgerInputs, AllocationPolicy]:
    """Inverse of ``snapshot``. Refuses floats, unknown fields and other schema versions."""
    data = json.loads(raw, parse_float=_refuse_float)
    assert_no_floats(data)
    if data.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError(f"unsupported snapshot schema {data.get('schema')!r}")
    inputs = LedgerInputs.model_validate_json(_canonical(data["inputs"]))
    policy = AllocationPolicy.model_validate_json(_canonical(data["policy"]))
    return inputs, policy


def plan_bytes(plan: Plan) -> bytes:
    return _canonical(plan.model_dump(mode="json"))
