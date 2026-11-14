"""Persist actual CPA attempts and the reason for continuing an unresolved doubt.

Three unresolved answers increase scrutiny; they are not a call cap. An exact
question/model/input replay returns its original raw answer. A changed question
is permitted, including on the same audio, with an explicit useful plan after
three unresolved answers. This module never supplies mutation or release rights.
"""
from __future__ import annotations


class RetryNeedsPlan(RuntimeError):
    pass


class CallNotDispatched(RuntimeError):
    """Adapter proof that failure occurred before entering any transport.

    Never wrap a timeout, connection failure or lost response in this type.
    An adapter may raise it only when no provider dispatch was attempted.
    """

    def __init__(self, reason_code: str):
        if not isinstance(reason_code, str) or not reason_code.isidentifier() or not reason_code.isupper():
            raise ValueError('a non-secret uppercase reason code is required')
        self.reason_code = reason_code
        super().__init__(reason_code)


