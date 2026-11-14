"""Review failure taxonomy, independent of exception messages and content codes.

Only explicit contract and provider failures may become review outcomes. All
other exceptions retain their identity and traceback; the outer boundary logs
one PROGRAM_ERROR record and attaches that same record to the exception.
"""
from __future__ import annotations


from .contract_rejection import ContractRejection as ContractRejection


