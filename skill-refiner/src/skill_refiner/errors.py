from __future__ import annotations


class RefinerError(Exception):
    code = "REFINER_ERROR"
    exit_code = 2
    recoverable = False

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


class InputError(RefinerError):
    code = "INPUT_ERROR"
    exit_code = 2


class EnvironmentError(RefinerError):
    code = "ENVIRONMENT_ERROR"
    exit_code = 3


class VerificationError(RefinerError):
    code = "VERIFICATION_FAILED"
    exit_code = 4


class BudgetExhausted(RefinerError):
    code = "BUDGET_EXHAUSTED"
    exit_code = 5


class NeedsUserDecision(RefinerError):
    code = "NEEDS_USER_DECISION"
    exit_code = 6
    recoverable = True


class TamperError(VerificationError):
    code = "TAMPER_DETECTED"


class AdapterError(RefinerError):
    code = "ADAPTER_ERROR"
    exit_code = 3

