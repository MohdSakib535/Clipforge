"""The "Brief reels" tab: saved briefs, and the reels made from them. A reel
from a brief is an ordinary Reel (``brief_id`` set), so its script, video,
music and bucket copy use the /reels endpoints; only its creation and list are
here, and /reels leaves it out of "Create a reel"."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.api import reels as reels_api
from app.api.deps import get_current_user
from app.api.errors import AppError, NotFoundError
from app.db.session import get_db
from app.models import Brief, Reel, User
from app.schemas.brief import BriefCreate, BriefOut, BriefReelCreate, BriefUpdate
from app.schemas.reel import ReelOut
from app.services import brief_script, reel_script
from app.tasks import dispatch

router = APIRouter(prefix="/briefs", tags=["briefs"])


def _get(db: Session, user: User, brief_id: str) -> Brief:
    brief = db.get(Brief, brief_id)
    if brief is None or brief.user_id != user.id:
        raise NotFoundError("Brief not found")
    return brief


def _name_of(content: str) -> str:
    """A brief's name when none was given: its first line (a heading, usually)."""
    first = next((ln for ln in content.splitlines() if ln.strip("#-*> \t")), "")
    name = first.strip("#-*> \t")
    return name if len(name) <= 60 else name[:59].rstrip() + "…"


def _split(content: str) -> dict[str, str]:
    """The brief's parts from the writer (a few seconds; it must be running)."""
    try:
        return brief_script.split_brief(content)
    except reel_script.ScriptError as exc:
        raise AppError(str(exc), code="brief_split_failed", status_code=502) from None


@router.get("", response_model=list[BriefOut])
def list_briefs(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return db.query(Brief).filter(Brief.user_id == user.id).order_by(Brief.created_at.desc()).all()


@router.post("", response_model=BriefOut, status_code=status.HTTP_201_CREATED)
def create_brief(
    body: BriefCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Brief:
    """Save a pasted brief, split into its parts by the writer."""
    name = body.name or _name_of(body.content)
    brief = Brief(user_id=user.id, name=name, content=body.content, **_split(body.content))
    db.add(brief)
    db.commit()
    db.refresh(brief)
    return brief


@router.put("/{brief_id}", response_model=BriefOut)
def update_brief(
    brief_id: str,
    body: BriefUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Brief:
    brief = _get(db, user, brief_id)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(brief, field, value)
    db.commit()
    db.refresh(brief)
    return brief


@router.post("/{brief_id}/split", response_model=BriefOut)
def split_again(
    brief_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Brief:
    """Split its text into the three parts again (the parts edited by hand are replaced)."""
    brief = _get(db, user, brief_id)
    for field, value in _split(brief.content).items():
        setattr(brief, field, value)
    db.commit()
    db.refresh(brief)
    return brief


@router.delete("/{brief_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_brief(
    brief_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Response:
    """Its reels are kept (their pictures keep the look); they can't be written again."""
    db.delete(_get(db, user, brief_id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/reels/all", response_model=list[ReelOut])
def list_brief_reels(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    reels = (
        db.query(Reel)
        .filter(Reel.user_id == user.id, Reel.brief_id.is_not(None))
        .order_by(Reel.created_at.desc())
        .all()
    )
    return [reels_api._out(r) for r in reels]


@router.post("/{brief_id}/reels", response_model=ReelOut, status_code=status.HTTP_202_ACCEPTED)
def create_brief_reel(
    brief_id: str,
    body: BriefReelCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReelOut:
    """Returns at once with status "scripting"; poll GET /reels/{id} until
    "script_ready". Its pictures are drawn in the brief's look."""
    brief = _get(db, user, brief_id)
    if body.platform not in reel_script.PLATFORMS:
        raise AppError(f"Unknown platform '{body.platform}'", code="bad_platform")
    reel = Reel(
        user_id=user.id,
        platform=body.platform,
        language=body.language,
        voice=reels_api._check_voice(body.language, body.voice),
        topic=body.topic,
        script_type="story",  # told calmly (reel_render's story)
        description=body.description or None,
        script_model=reels_api._check_script_model(body.script_model),
        duration_s=body.duration_s,
        brief_id=brief.id,
        custom_style=brief.visual_style or None,
        status="scripting",
        stage="Writing the script",
    )
    db.add(reel)
    db.commit()
    dispatch.enqueue_reel_script(reel.id)
    db.refresh(reel)
    return reels_api._out(reel)
