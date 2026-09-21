from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import ChatThread
from ..services.library import InvalidBookError, LibrarySource
from ..services.long_term_memory import MemoryChange
from .auth import CurrentUser

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=8000)
    rag_scope: Literal["all", "selected"] = "all"
    book_ids: list[int] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_rag_scope(self):
        if self.rag_scope == "selected" and not self.book_ids:
            raise ValueError("指定书籍检索时请至少选择一本书")
        self.book_ids = list(dict.fromkeys(self.book_ids))
        return self


class ChatSource(BaseModel):
    book_id: int
    title: str
    locator: str
    excerpt: str


class ChatResponse(BaseModel):
    content: str
    model: str
    memory_changes: list[MemoryChange] = Field(default_factory=list)
    memory_warning: str | None = None
    sources: list[ChatSource] = Field(default_factory=list)
    rag_warning: str | None = None


class ChatThreadResponse(BaseModel):
    thread_id: str


class ChatHistoryMessage(BaseModel):
    role: str
    content: str
    sources: list[ChatSource] = Field(default_factory=list)


class ChatHistoryResponse(BaseModel):
    messages: list[ChatHistoryMessage]


def get_owned_thread(db: Session, thread_id: str, user_id: int) -> ChatThread:
    chat_thread = db.scalar(
        select(ChatThread).where(
            ChatThread.id == thread_id,
            ChatThread.user_id == user_id,
        )
    )
    if chat_thread is None:
        raise HTTPException(status_code=404, detail="没有找到这段对话")
    return chat_thread


def _chat_sources(message: AIMessage) -> list[ChatSource]:
    sources = []
    for value in message.additional_kwargs.get("sources", []):
        try:
            source = LibrarySource.model_validate(value)
        except Exception:
            continue
        sources.append(
            ChatSource(
                book_id=source.book_id,
                title=source.title,
                locator=source.locator,
                excerpt=source.excerpt,
            )
        )
    return sources


@router.post("/threads", response_model=ChatThreadResponse, status_code=201)
def create_chat_thread(current_user: CurrentUser, db: Session = Depends(get_db)):
    chat_thread = ChatThread(
        id=str(uuid4()),
        user_id=current_user.id,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    try:
        db.add(chat_thread)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="暂时无法创建新对话") from exc
    return ChatThreadResponse(thread_id=chat_thread.id)


@router.get(
    "/threads/{thread_id}/messages",
    response_model=ChatHistoryResponse,
)
async def get_chat_history(
    thread_id: str,
    request: Request,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    get_owned_thread(db, thread_id, current_user.id)
    try:
        snapshot = await request.app.state.chat_graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="暂时无法读取聊天记录") from exc

    messages = []
    for message in snapshot.values.get("messages", []):
        if isinstance(message, HumanMessage):
            messages.append(ChatHistoryMessage(role="user", content=str(message.content)))
        elif isinstance(message, AIMessage):
            messages.append(
                ChatHistoryMessage(
                    role="assistant",
                    content=str(message.content),
                    sources=_chat_sources(message),
                )
            )
    return ChatHistoryResponse(messages=messages)


@router.post("", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    request: Request,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
) -> ChatResponse:
    # 先验证线程归属，再访问 Redis，避免通过猜测 thread_id 越权读取或续写对话。
    get_owned_thread(db, payload.thread_id, current_user.id)
    if payload.rag_scope == "selected":
        try:
            await request.app.state.library_service.validate_ready_books(
                current_user.id,
                payload.book_ids,
            )
        except InvalidBookError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    message = HumanMessage(content=payload.message)
    config = {
        "configurable": {
            "thread_id": payload.thread_id,
            "user_id": current_user.id,
        }
    }

    try:
        # lifespan 中创建的 Graph 持有同一个 RedisSaver，并通过 thread_id 恢复历史消息。
        result = await request.app.state.chat_graph.ainvoke(
            {
                "messages": [message],
                "rag_scope": payload.rag_scope,
                "rag_book_ids": payload.book_ids,
            },
            config=config,
        )
        reply_message = result["messages"][-1]
        reply = reply_message.content
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="暂时无法连接 DeepSeek，请稍后再试") from exc

    if not isinstance(reply, str):
        reply = str(reply)

    return ChatResponse(
        content=reply,
        model=get_settings().deepseek_model,
        memory_changes=result.get("memory_changes", []),
        memory_warning=result.get("memory_warning"),
        sources=_chat_sources(reply_message),
        rag_warning=result.get("rag_warning"),
    )
