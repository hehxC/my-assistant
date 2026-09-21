from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from redis.asyncio import Redis

from .agents.chat_graph import create_chat_graph
from .agents.plan_agent import create_plan_graph
from .config import get_settings
from .database import create_tables
from .routers.auth import router as auth_router
from .routers.chat import router as chat_router
from .routers.hobbies import router as hobbies_router
from .routers.library import router as library_router
from .routers.plans import router as plans_router
from .routers.study import router as study_router
from .services.library import (
    DashScopeEmbeddingAdapter,
    LibraryIngestionWorker,
    LibraryService,
    RedisVectorIndex,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 应用启动时完成幂等建表，保证接口接收请求前数据库结构可用。
    create_tables()
    settings = get_settings()
    vector_redis = Redis.from_url(settings.redis_url, decode_responses=False)
    embedding = DashScopeEmbeddingAdapter(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
        model=settings.dashscope_embedding_model,
        dimensions=settings.dashscope_embedding_dimensions,
    )
    library_service = LibraryService(
        storage_root=settings.library_storage_dir,
        max_upload_bytes=settings.library_max_upload_mb * 1024 * 1024,
        embedding=embedding,
        vector_index=RedisVectorIndex(
            vector_redis,
            settings.dashscope_embedding_dimensions,
        ),
        embedding_model=settings.dashscope_embedding_model,
        embedding_dimension=settings.dashscope_embedding_dimensions,
    )
    library_worker = LibraryIngestionWorker(library_service)
    await library_worker.start()
    app.state.library_service = library_service
    app.state.library_worker = library_worker

    # RedisSaver 的连接覆盖整个应用生命周期，Graph 才能稳定读写会话状态。
    async with AsyncRedisSaver.from_conn_string(settings.redis_url) as checkpointer:
        await checkpointer.asetup()
        app.state.chat_graph = create_chat_graph(
            checkpointer,
            library_retriever=library_service,
        )
        app.state.plan_graph = create_plan_graph()
        try:
            yield
        finally:
            await library_worker.stop()
            await vector_redis.aclose()


app = FastAPI(title="个人工作助手 API", version="1.0.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(study_router)
app.include_router(hobbies_router)
app.include_router(plans_router)
app.include_router(library_router)

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
