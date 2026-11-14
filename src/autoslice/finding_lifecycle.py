"""One finding lifecycle, independent of provider/transport receipt statuses.

Legacy receipts are read at this boundary, never rewritten.  A lifecycle is a
derived routing view, not mutation or release authority: KEEP is revalidated
by the existing release predicate and EDIT still needs apply + whole review.
"""
from __future__ import annotations

from .review_errors import ContractRejection as ContractRejection


