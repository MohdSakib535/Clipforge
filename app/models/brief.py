from __future__ import annotations

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, uuid_str


class Brief(Base, TimestampMixin):
    """A creator's brief for a whole kind of reel ("Brief reels"): the niche,
    how its scripts are told and how its pictures look, pasted once and used
    for every reel made from it (services/brief_script.py). Its parts are
    split out of ``content`` when it is saved, and can be edited."""

    __tablename__ = "briefs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    content: Mapped[str] = mapped_column(Text)  # everything as pasted
    niche: Mapped[str] = mapped_column(
        Text, default="", server_default=""
    )  # what the reels are about
    rules: Mapped[str] = mapped_column(
        Text, default="", server_default=""
    )  # story shape, narration
    # In English, put before every picture prompt: how the pictures are drawn.
    visual_style: Mapped[str] = mapped_column(Text, default="", server_default="")
