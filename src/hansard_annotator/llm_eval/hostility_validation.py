"""Allowlisted identity for a future held-out hostility validation run.

This module only validates configuration.  It never calls a provider.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

HELDOUT_SAMPLE_ID = "hostility-heldout-100"
HELDOUT_SAMPLE_SHA256 = "f3d9451b3352025b769d263fc896f57683a741ea6fd9af0b6477651707cf3b36"
PROMPT_VERSION = "hostility-prompt-v2"
PROMPT_SHA256 = "185f5162348ae3b3d735ea43247da18cf6b3c1bb12f8a72cad57e38ce31cb1a8"
CONTRACT_SHA256 = "dc716b49e0759ba7063973038b234f6696e778c4d90ee4a405d9600a2435b11d"
MODEL = "gemini-2.5-flash"
THINKING_BUDGET = 0


@dataclass(frozen=True)
class HostilityValidationIdentity:
    sample_id: str
    sample_sha256: str
    prompt_version: str = PROMPT_VERSION
    prompt_sha256: str = PROMPT_SHA256
    prediction_contract_sha256: str = CONTRACT_SHA256
    model: str = MODEL
    thinking_budget: int = THINKING_BUDGET


def validate_heldout_definition(path: Path) -> HostilityValidationIdentity:
    import json

    document = json.loads(path.read_text(encoding="utf-8"))
    turns = document.get("turns")
    if not isinstance(turns, list) or len(turns) != 100:
        raise ValueError("held-out sample must contain exactly 100 turns")
    canonical = "\n".join(
        f"{row.get('sample_order')}|{row.get('turn_key')}|{row.get('primary_domain')}"
        for row in turns
        if isinstance(row, dict)
    )
    calculated = hashlib.sha256(canonical.encode()).hexdigest()
    if (
        document.get("sample_id") != HELDOUT_SAMPLE_ID
        or document.get("sample_sha256") != HELDOUT_SAMPLE_SHA256
        or calculated != HELDOUT_SAMPLE_SHA256
    ):
        raise ValueError("held-out sample identity drift")
    return HostilityValidationIdentity(HELDOUT_SAMPLE_ID, HELDOUT_SAMPLE_SHA256)
