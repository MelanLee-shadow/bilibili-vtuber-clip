"""Deterministic per-question workflow. No clock, filesystem or provider I/O.

Receipts are content-addressed references. Their authority is checked by the
shell's original consumers, both before an event and during cold replay.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Literal

from .finding_lifecycle import ContractRejection


class ClosureContractError(ContractRejection):
    """A declared workflow contract rejection, never an inferred program bug."""
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code.split(':',1)[0]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FindingKey:
    physical: str
    materials: str


@dataclass(frozen=True)
class TaskSpec:
    key: FindingKey
    scope_cues: tuple[int, ...]
    local_text_sha256: str
    finding_ref: str
    check_ref: str
    materials_ref: str
    origin_check_ref: str = ""
    presence: Literal['PRESENT','ABSENT'] = 'PRESENT'
    projection_ref: str = ''
    applied_ruling_ref: str = ''


