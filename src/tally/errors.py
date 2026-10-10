"""Business errors. Each has a stable machine-readable ``code`` that clients can switch on."""

from __future__ import annotations


class LedgerError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def not_found(code: str, message: str) -> LedgerError:
    return LedgerError(404, code, message)


def conflict(code: str, message: str) -> LedgerError:
    return LedgerError(409, code, message)


def unprocessable(code: str, message: str) -> LedgerError:
    return LedgerError(422, code, message)
