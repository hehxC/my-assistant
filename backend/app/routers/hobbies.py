from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Hobby
from .auth import CurrentUser


router = APIRouter(prefix="/api/hobbies", tags=["hobbies"])


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class HobbyPayload(BaseModel):
    name: str
    note: str = Field(default="", max_length=1000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        name = value.strip()
        if not 1 <= len(name) <= 50:
            raise ValueError("爱好名称长度必须为 1 到 50 个字符")
        return name

    @field_validator("note")
    @classmethod
    def trim_note(cls, value: str) -> str:
        return value.strip()


class HobbyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    note: str
    created_at: datetime
    updated_at: datetime


def get_owned_hobby(db: Session, hobby_id: int, user_id: int) -> Hobby:
    hobby = db.scalar(
        select(Hobby).where(Hobby.id == hobby_id, Hobby.user_id == user_id)
    )
    if hobby is None:
        raise HTTPException(status_code=404, detail="没有找到这个爱好")
    return hobby


@router.get("", response_model=list[HobbyResponse])
def list_hobbies(current_user: CurrentUser, db: Session = Depends(get_db)):
    return db.scalars(
        select(Hobby)
        .where(Hobby.user_id == current_user.id)
        .order_by(Hobby.created_at.desc(), Hobby.id.desc())
    ).all()


@router.post("", response_model=HobbyResponse, status_code=201)
def create_hobby(
    payload: HobbyPayload,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    now = utc_now()
    hobby = Hobby(
        user_id=current_user.id,
        name=payload.name,
        note=payload.note,
        created_at=now,
        updated_at=now,
    )
    try:
        db.add(hobby)
        db.commit()
        db.refresh(hobby)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="这个爱好已经存在") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="暂时无法保存爱好") from exc
    return hobby


@router.put("/{hobby_id}", response_model=HobbyResponse)
def update_hobby(
    hobby_id: int,
    payload: HobbyPayload,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    hobby = get_owned_hobby(db, hobby_id, current_user.id)
    hobby.name = payload.name
    hobby.note = payload.note
    hobby.updated_at = utc_now()

    try:
        db.commit()
        db.refresh(hobby)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="这个爱好已经存在") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="暂时无法更新爱好") from exc
    return hobby


@router.delete("/{hobby_id}", status_code=204)
def delete_hobby(
    hobby_id: int,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
) -> Response:
    hobby = get_owned_hobby(db, hobby_id, current_user.id)
    try:
        db.delete(hobby)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="暂时无法删除爱好") from exc
    return Response(status_code=204)
