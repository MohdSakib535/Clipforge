from __future__ import annotations

from pydantic import BaseModel


class DeleteAllData(BaseModel):
    # Must be "DELETE", so a stray request can never wipe everything.
    confirm: str
