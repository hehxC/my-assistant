from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.redis.aio import AsyncRedisSaver

from .agents.chat_graph import create_chat_graph
from .agents.plan_agent import create_plan_graph
from .config import get_settings
from .database import create_tables
from .routers.auth import router as auth_router
from .routers.chat import router as chat_router
from .routers.hobbies import router as hobbies_router
from .routers.plans import router as plans_router
from .routers.study import router as study_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 应用启动时完成幂等建表，保证接口接收请求前数据库结构可用。
    create_tables()
    settings = get_settings()

    # RedisSaver 的连接覆盖整个应用生命周期，Graph 才能稳定读写会话状态。
    async with AsyncRedisSaver.from_conn_string(settings.redis_url) as checkpointer:
        await checkpointer.asetup()
        app.state.chat_graph = create_chat_graph(checkpointer)
        app.state.plan_graph = create_plan_graph()
        yield


app = FastAPI(title="个人工作助手 API", version="1.0.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(study_router)
app.include_router(hobbies_router)
app.include_router(plans_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
