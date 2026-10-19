from __future__ import annotations

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: object | None = None


class ErrorBody(BaseModel):
    error: ErrorDetail
