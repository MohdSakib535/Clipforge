from __future__ import annotations

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import User

# Phase 1 is single-user (no auth). A default dev user owns all uploads; swap in
# real auth later without touching the rest of the API.
DEV_EMAIL = "dev@clipforge.local"


def get_current_user(db: Session = Depends(get_db)) -> User:
    user = db.query(User).filter(User.email == DEV_EMAIL).one_or_none()
    if user is None:
        user = User(email=DEV_EMAIL)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user
