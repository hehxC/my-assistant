from datetime import datetime, timedelta, timezone
from hashlib import sha256
from secrets import token_urlsafe
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator
from pwdlib import PasswordHash
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import ApplicationState, AuthSession, ChatThread, StudySession, User


router = APIRouter(prefix="/api/auth", tags=["auth"])
SESSION_COOKIE_NAME = "assistant_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
password_hash = PasswordHash.recommended()
DUMMY_PASSWORD_HASH = password_hash.hash("dummy-password-for-timing-check")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_username(username: str) -> str:
    return username.strip().casefold()


def hash_session_token(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


class RegisterRequest(BaseModel):
    username: str
    password: str = Field(min_length=8, max_length=128)
    legacy_thread_id: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        stripped = value.strip()
        if not 3 <= len(stripped) <= 32:
            raise ValueError("用户名长度必须为 3 到 32 个字符")
        return stripped


class LoginRequest(BaseModel):
    username: str
    password: str = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    id: int
    username: str


class RegisterResponse(BaseModel):
    user: UserResponse
    claimed_legacy_data: bool


def create_auth_session(db: Session, user_id: int) -> str:
    token = token_urlsafe(32)
    now = utc_now()
    db.add(
        AuthSession(
            token_hash=hash_session_token(token),
            user_id=user_id,
            created_at=now,
            expires_at=now + timedelta(seconds=SESSION_MAX_AGE_SECONDS),
        )
    )
    return token


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=get_settings().session_cookie_secure,
        path="/",
    )


def get_current_user(
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
    db: Session = Depends(get_db),
) -> User:
    if not session_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")

    token_digest = hash_session_token(session_token)
    auth_session = db.scalar(
        select(AuthSession).where(AuthSession.token_hash == token_digest)
    )
    if auth_session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")

    if auth_session.expires_at <= utc_now():
        db.delete(auth_session)
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期")

    user = db.get(User, auth_session.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


@router.post("/register", response_model=RegisterResponse, status_code=201)
def register(payload: RegisterRequest, response: Response, db: Session = Depends(get_db)):
    normalized = normalize_username(payload.username)
    if db.scalar(select(User.id).where(User.username_normalized == normalized)) is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")

    try:
        # 锁住唯一的应用状态行，确保并发注册时也只有一个账号接管旧数据。
        app_state = db.scalar(
            select(ApplicationState).where(ApplicationState.id == 1).with_for_update()
        )
        if app_state is None:
            app_state = ApplicationState(id=1)
            db.add(app_state)
            db.flush()

        user = User(
            username=payload.username,
            username_normalized=normalized,
            password_hash=password_hash.hash(payload.password),
            created_at=utc_now(),
        )
        db.add(user)
        db.flush()

        claimed_legacy_data = app_state.legacy_claimed_by_user_id is None
        if claimed_legacy_data:
            db.execute(
                update(StudySession)
                .where(StudySession.user_id.is_(None))
                .values(user_id=user.id)
            )
            if payload.legacy_thread_id:
                db.add(
                    ChatThread(
                        id=payload.legacy_thread_id,
                        user_id=user.id,
                        created_at=utc_now(),
                    )
                )
            app_state.legacy_claimed_by_user_id = user.id

        token = create_auth_session(db, user.id)
        db.commit()
        db.refresh(user)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="用户名已存在") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="暂时无法创建账号") from exc

    set_session_cookie(response, token)
    return RegisterResponse(
        user=UserResponse(id=user.id, username=user.username),
        claimed_legacy_data=claimed_legacy_data,
    )


@router.post("/login", response_model=UserResponse)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(
        select(User).where(User.username_normalized == normalize_username(payload.username))
    )
    password_is_valid = password_hash.verify(
        payload.password,
        user.password_hash if user else DUMMY_PASSWORD_HASH,
    )
    if user is None or not password_is_valid:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    try:
        token = create_auth_session(db, user.id)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="暂时无法登录") from exc

    set_session_cookie(response, token)
    return UserResponse(id=user.id, username=user.username)


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
    db: Session = Depends(get_db),
) -> None:
    if session_token:
        db.query(AuthSession).filter(
            AuthSession.token_hash == hash_session_token(session_token)
        ).delete(synchronize_session=False)
        db.commit()
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


@router.get("/me", response_model=UserResponse)
def me(current_user: CurrentUser):
    return UserResponse(id=current_user.id, username=current_user.username)
