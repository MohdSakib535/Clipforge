"""Free disk space: delete every video and everything made from it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.errors import AppError, ConflictError
from app.db.session import get_db
from app.models import User
from app.schemas.data import DeleteAllData
from app.services.discard import busy_reason, data_usage, delete_all_data

router = APIRouter(tags=["data"])


@router.get("/data/usage")
def usage(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    """Counts and disk space that "delete all data" would free."""
    return data_usage(db, user.id)


@router.post("/data/delete-all")
def delete_all(
    body: DeleteAllData,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Delete every video, job, clip and transcript, with their files. The
    music library and the Whisper model are kept. Refused while work that
    writes files is still running."""
    if body.confirm != "DELETE":
        raise AppError(
            "Type DELETE to confirm", code="not_confirmed", status_code=status.HTTP_400_BAD_REQUEST
        )
    reason = busy_reason(db, user.id)
    if reason:
        raise ConflictError(reason)
    return delete_all_data(db, user.id)
