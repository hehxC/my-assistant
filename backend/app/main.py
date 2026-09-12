from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .auth import CurrentUser, router as auth_router
from .config import get_settings
from .database import create_tables, get_db
from .graph import create_chat_graph
from .models import ChatThread
from .study import router as study_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 应用启动时确保学习记录表存在，再开始接收请求。
    create_tables()

    settings = get_settings()

    # RedisSaver 的连接必须覆盖整个应用生命周期，Graph 才能稳定读写会话状态。
    async with AsyncRedisSaver.from_conn_string(
        settings.redis_url,
    ) as checkpointer:
        # 第一次使用时创建 RedisJSON 和 RediSearch 索引。
        await checkpointer.asetup()

        app.state.chat_graph = create_chat_graph(checkpointer)

        yield


class ChatRequest(BaseModel):

    thread_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=8000)


class ChatResponse(BaseModel):

    content: str
    model: str


class ChatThreadResponse(BaseModel):
    thread_id: str


class ChatHistoryMessage(BaseModel):
    role: str
    content: str


class ChatHistoryResponse(BaseModel):
    messages: list[ChatHistoryMessage]


# 创建 FastAPI 应用。
app = FastAPI(title="个人工作助手 API", version="1.0.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(study_router)

# 允许本地 Vue 开发服务器访问后端接口。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/api/health")
async def health() -> dict[str, str]:

    return {"status": "ok"}


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


@app.post("/api/chat/threads", response_model=ChatThreadResponse, status_code=201)
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


@app.get(
    "/api/chat/threads/{thread_id}/messages",
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
            messages.append(ChatHistoryMessage(role="assistant", content=str(message.content)))
    return ChatHistoryResponse(messages=messages)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    request: Request,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
) -> ChatResponse:

    # 先验证线程归属，再访问 Redis，避免通过猜测 thread_id 越权读取或续写对话。
    get_owned_thread(db, payload.thread_id, current_user.id)

    message = HumanMessage(content=payload.message)

    config = {
        "configurable": {
            "thread_id": payload.thread_id,
        }
    }

    try:
        # lifespan 中创建的 Graph 持有同一个 RedisSaver，并通过 thread_id 恢复历史消息。
        result = await request.app.state.chat_graph.ainvoke(
            {"messages": [message]},
            config=config,
        )

        reply = result["messages"][-1].content
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="暂时无法连接 DeepSeek，请稍后再试") from exc

    if not isinstance(reply, str):
        reply = str(reply)

    return ChatResponse(content=reply, model=get_settings().deepseek_model)
