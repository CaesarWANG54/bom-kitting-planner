"""Stable error codes for callers and the command line."""

from typing import Any


class PlannerError(Exception):
    code = "PLANNER_ERROR"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(PlannerError):
    code = "VALIDATION_ERROR"


class UnsafeConfigurationError(PlannerError):
    code = "UNSAFE_CONFIGURATION"


class BomCycleError(PlannerError):
    code = "BOM_CYCLE"


class ProhibitedAutomationError(PlannerError):
    code = "PROHIBITED_AUTOMATION"
