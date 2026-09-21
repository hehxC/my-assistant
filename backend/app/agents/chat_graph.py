import asyncio
import json
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from ..config import get_settings
from ..services.long_term_memory import (
    AssistantRecommendation,
    ChatTurnOutput,
    LongTermMemoryRepository,
    MemoryChange,
    MemoryContext,
    MemoryExtraction,
    ProposedMemoryAction,
)
from ..services.library import LibrarySource
from .prompts.talk_agent_prompt import (
    CHAT_OUTPUT_PROMPT,
    LIBRARY_CONTEXT_PROMPT,
    MEMORY_CONTEXT_PROMPT,
    MEMORY_EXTRACTION_PROMPT,
    TALK_AGENT_PROMPT,
    TOOL_DECISION_PROMPT,
)


class ChatToolRequest(BaseModel):
    tool_name: Literal["retrieve_library"]
    query: str = Field(min_length=1, max_length=2000)


class ChatToolDecision(BaseModel):
    tool_requests: list[ChatToolRequest] = Field(default_factory=list, max_length=1)


class ChatToolExecutionResult(BaseModel):
    tool_name: Literal["retrieve_library"]
    query: str
    succeeded: bool
    sources: list[LibrarySource] = Field(default_factory=list)
    error: str | None = None


class ChatState(TypedDict, total=False):
    """同时保存线程消息，以及每轮覆盖的长期记忆处理结果。"""

    messages: Annotated[list[AnyMessage], add_messages]
    active_memories: list[MemoryContext]
    known_memories: list[MemoryContext]
    pending_recommendations: list[MemoryContext]
    user_memory_actions: list[ProposedMemoryAction]
    recommendation_requested: bool
    assistant_recommendations: list[AssistantRecommendation]
    memory_changes: list[MemoryChange]
    memory_warning: str | None
    rag_scope: str
    rag_book_ids: list[int]
    tool_requests: list[ChatToolRequest]
    tool_results: list[ChatToolExecutionResult]
    rag_sources: list[LibrarySource]
    used_sources: list[LibrarySource]
    rag_warning: str | None


def _runtime_identity(config: RunnableConfig) -> tuple[int, str]:
    configurable = config.get("configurable", {})
    try:
        return int(configurable["user_id"]), str(configurable["thread_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("聊天缺少用户或线程信息") from exc


def _latest_user_message(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


def _recent_conversation(messages: list[AnyMessage]) -> list[dict[str, str]]:
    recent = []
    for message in messages[-10:]:
        if isinstance(message, HumanMessage):
            role = "user"
        elif isinstance(message, AIMessage):
            role = "assistant"
        else:
            continue
        recent.append({"role": role, "content": str(message.content)[:2000]})
    return recent


async def _invoke_with_retry(
    runnable: Any,
    messages: list[AnyMessage],
    attempts: int = 3,
) -> Any:
    """调用结构化模型并在解析失败时重试，规避 DeepSeek 偶发的非法 JSON 输出。

    DeepSeek 的 JSON Mode 偶尔会在字符串字段内输出未转义的英文双引号，
    导致返回内容不是合法 JSON，解析失败。这里在失败时附带明确的纠错提示重试，
    并保留最后一次异常供上层统一处理。
    """

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await runnable.ainvoke(messages)
        except Exception as exc:
            last_error = exc
            if attempt < attempts - 1:
                messages = [
                    *messages,
                    SystemMessage(
                        content=(
                            "上一次输出不是严格有效的 JSON，常见原因是字符串字段内出现了"
                            "未转义的英文双引号。请重新输出符合 Schema 的有效 JSON："
                            "字符串字段内的双引号必须转义为 \\\"，或改用中文引号「」『』，"
                            "且不要输出 JSON 对象之外的任何额外内容。"
                        )
                    ),
                ]
    raise last_error


def _json_mode_history(messages: list[AnyMessage]) -> list[AnyMessage]:
    """让历史助手消息与当前聊天模型要求的结构化输出格式保持一致。"""

    converted = []
    for message in messages:
        if isinstance(message, AIMessage):
            # Redis 继续保存供前端展示的纯文本；这里只转换传给 JSON Mode 模型的副本。
            converted.append(
                AIMessage(
                    content=ChatTurnOutput(
                        reply=str(message.content),
                        recommendations=[],
                        source_ids=[],
                    ).model_dump_json(),
                )
            )
        else:
            converted.append(message)
    return converted


def create_chat_graph(
    checkpointer: BaseCheckpointSaver,
    memory_repository: LongTermMemoryRepository | None = None,
    chat_model: Any | None = None,
    memory_extractor: Any | None = None,
    tool_decider: Any | None = None,
    library_retriever: Any | None = None,
):
    """创建并行回复与长期记忆提取的聊天 Graph。"""

    settings = get_settings()
    needs_default_model = (
        chat_model is None
        or memory_extractor is None
        or (library_retriever is not None and tool_decider is None)
    )
    if not settings.deepseek_api_key and needs_default_model:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")

    repository = memory_repository or LongTermMemoryRepository()
    if chat_model is None:
        base_chat_model = ChatOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            temperature=0.8,
            max_tokens=800,
        )
        chat_model = base_chat_model.with_structured_output(
            ChatTurnOutput,
            method="json_mode",
        )
    if memory_extractor is None:
        extraction_model = ChatOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            temperature=0,
            max_tokens=800,
        )
        memory_extractor = extraction_model.with_structured_output(
            MemoryExtraction,
            method="json_mode",
        )
    if library_retriever is not None and tool_decider is None:
        decision_model = ChatOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            temperature=0,
            max_tokens=400,
        )
        tool_decider = decision_model.with_structured_output(
            ChatToolDecision,
            method="json_mode",
        )

    async def load_memories(_state: ChatState, config: RunnableConfig):
        """加载当前账号的有效记忆；读取失败时让聊天在无记忆模式下继续。"""

        user_id, _ = _runtime_identity(config)

        try:
            # SQLAlchemy 当前使用同步驱动，将查询放到工作线程，避免阻塞异步 Graph 的事件循环。
            context = await asyncio.to_thread(repository.load_context, user_id)
            return {
                "active_memories": context.active_memories,
                "known_memories": context.known_memories,
                "pending_recommendations": context.pending_recommendations,
                "user_memory_actions": [],
                "recommendation_requested": False,
                "assistant_recommendations": [],
                "memory_changes": [],
                "memory_warning": None,
            }
        except Exception:
            return {
                "active_memories": [],
                "known_memories": [],
                "pending_recommendations": [],
                "user_memory_actions": [],
                "recommendation_requested": False,
                "assistant_recommendations": [],
                "memory_changes": [],
                "memory_warning": "长期记忆暂时不可用",
            }

    async def decide_tools(state: ChatState):
        """根据最新消息按需选择书库工具；决策失败时跳过检索。"""

        if library_retriever is None or tool_decider is None:
            return {"tool_requests": [], "rag_warning": None}

        payload = {
            "latest_user_message": _latest_user_message(state["messages"]),
            "recent_conversation": _recent_conversation(state["messages"]),
        }
        messages = [
            SystemMessage(content=TOOL_DECISION_PROMPT),
            HumanMessage(
                content=(
                    f"输出 Schema：{json.dumps(ChatToolDecision.model_json_schema(), ensure_ascii=False)}\n"
                    f"判断上下文：{json.dumps(payload, ensure_ascii=False)}"
                )
            ),
        ]
        try:
            decision = ChatToolDecision.model_validate(
                await _invoke_with_retry(tool_decider, messages)
            )
            return {"tool_requests": decision.tool_requests, "rag_warning": None}
        except Exception:
            return {
                "tool_requests": [],
                "rag_warning": "书库检索暂时不可用",
            }

    async def execute_tools(state: ChatState, config: RunnableConfig):
        """执行已经校验的工具请求，并从运行时注入账号与书籍范围。"""

        requests = state.get("tool_requests", [])
        if not requests or library_retriever is None:
            return {
                "tool_results": [],
                "rag_sources": [],
                "used_sources": [],
            }

        request = requests[0]
        user_id, _ = _runtime_identity(config)
        book_ids = (
            state.get("rag_book_ids", [])
            if state.get("rag_scope") == "selected"
            else None
        )
        try:
            sources = await library_retriever.retrieve(user_id, request.query, book_ids)
            result = ChatToolExecutionResult(
                tool_name=request.tool_name,
                query=request.query,
                succeeded=True,
                sources=sources,
            )
            return {
                "tool_results": [result],
                "rag_sources": sources,
                "used_sources": [],
                "rag_warning": None,
            }
        except Exception:
            result = ChatToolExecutionResult(
                tool_name=request.tool_name,
                query=request.query,
                succeeded=False,
                error="书库检索暂时不可用",
            )
            return {
                "tool_results": [result],
                "rag_sources": [],
                "used_sources": [],
                "rag_warning": "书库检索暂时不可用",
            }

    async def talk_and_collect_recommendations(state: ChatState):
        """一次模型调用同时生成聊天正文和本轮结构化推荐。"""

        model_messages = [
            SystemMessage(content=TALK_AGENT_PROMPT),
            # DeepSeek 中JSON Mode 要求提示明确出现 JSON，并由调用方提供目标 Schema。
            SystemMessage(
                content=(
                    f"{CHAT_OUTPUT_PROMPT}\n"
                    f"输出 JSON Schema："
                    f"{json.dumps(ChatTurnOutput.model_json_schema(), ensure_ascii=False)}"
                )
            ),
        ]
        if state["active_memories"]:
            memory_payload = [item.model_dump() for item in state["active_memories"]]
            model_messages.append(
                SystemMessage(
                    content=(
                        f"{MEMORY_CONTEXT_PROMPT}\n"
                        f"用户长期记忆：{json.dumps(memory_payload, ensure_ascii=False)}"
                    )
                )
            )
        if state["pending_recommendations"]:
            pending_payload = [
                item.model_dump() for item in state["pending_recommendations"]
            ]
            model_messages.append(
                SystemMessage(
                    content=(
                        "以下是仍在等待用户反馈的旧推荐。只有当前话题相关时才自然询问体验，"
                        "不得在无关话题中主动提及，也不得把它们当作本轮新推荐："
                        f"{json.dumps(pending_payload, ensure_ascii=False)}"
                    )
                )
            )
        if state.get("rag_sources"):
            source_payload = [source.model_dump() for source in state["rag_sources"]]
            model_messages.append(
                SystemMessage(
                    content=(
                        f"{LIBRARY_CONTEXT_PROMPT}\n"
                        f"书库参考资料：{json.dumps(source_payload, ensure_ascii=False)}"
                    )
                )
            )
        history = _json_mode_history(state["messages"])
        turn = ChatTurnOutput.model_validate(
            await _invoke_with_retry(chat_model, [*model_messages, *history])
        )
        source_map = {source.source_id: source for source in state.get("rag_sources", [])}
        used_sources = []
        seen_source_ids = set()
        for source_id in turn.source_ids:
            if source_id in source_map and source_id not in seen_source_ids:
                used_sources.append(source_map[source_id])
                seen_source_ids.add(source_id)
        return {
            "messages": [
                AIMessage(
                    content=turn.reply,
                    additional_kwargs={
                        "sources": [source.model_dump() for source in used_sources]
                    },
                )
            ],
            "assistant_recommendations": turn.recommendations,
            "used_sources": used_sources,
        }

    async def extract_user_memory_and_feedback(state: ChatState):
        """从本轮表达提取记忆；只读取分叉前状态，不依赖 talk 的模型回复。"""

        latest_message = _latest_user_message(state["messages"])
        payload = {
            "existing_memories": [item.model_dump() for item in state["known_memories"]],
            "pending_recommendations": [
                item.model_dump() for item in state["pending_recommendations"]
            ],
            "recent_conversation": _recent_conversation(state["messages"]),
            "latest_user_message": latest_message,
        }
        messages = [
            SystemMessage(content=MEMORY_EXTRACTION_PROMPT),
            HumanMessage(
                content=(
                    f"输出 Schema：{json.dumps(MemoryExtraction.model_json_schema(), ensure_ascii=False)}\n"
                    f"提取上下文：{json.dumps(payload, ensure_ascii=False)}"
                )
            ),
        ]
        try:
            result = MemoryExtraction.model_validate(
                await _invoke_with_retry(memory_extractor, messages)
            )
            return {
                "user_memory_actions": result.actions,
                "recommendation_requested": result.recommendation_requested,
            }
        except Exception:
            return {
                "user_memory_actions": [],
                "recommendation_requested": False,
                "memory_warning": "长期记忆暂时不可用",
            }

    async def persist_memory(state: ChatState, config: RunnableConfig):
        """事务化执行候选操作，失败时不撤销已经生成的聊天回复。"""

        user_actions = state.get("user_memory_actions", [])
        recommendations = (
            state.get("assistant_recommendations", [])
            if state.get("recommendation_requested")
            else []
        )
        if not user_actions and not recommendations:
            return {}
        user_id, thread_id = _runtime_identity(config)
        try:
            changes = await asyncio.to_thread(
                repository.apply_turn,
                user_id,
                thread_id,
                user_actions,
                recommendations,
            )
            return {"memory_changes": changes}
        except Exception:
            return {
                "memory_changes": [],
                "memory_warning": "长期记忆暂时不可用",
            }

    builder = StateGraph(ChatState)
    builder.add_node("load_memories", load_memories)
    builder.add_node("decide_tools", decide_tools)
    builder.add_node("execute_tools", execute_tools)
    builder.add_node("talk_and_collect_recommendations", talk_and_collect_recommendations)
    builder.add_node(
        "extract_user_memory_and_feedback",
        extract_user_memory_and_feedback,
    )
    builder.add_node("persist_memory", persist_memory)

    builder.add_edge(START, "load_memories")
    builder.add_edge(START, "decide_tools")
    builder.add_edge("decide_tools", "execute_tools")
    # 长期记忆和按需工具执行并行，二者就绪后才生成包含完整上下文的回复。
    builder.add_edge(
        ["load_memories", "execute_tools"],
        "talk_and_collect_recommendations",
    )
    builder.add_edge("load_memories", "extract_user_memory_and_feedback")
    # 列表形式的起点是汇合条件：两路结果齐备后才在一个事务中统一保存。
    builder.add_edge(
        ["talk_and_collect_recommendations", "extract_user_memory_and_feedback"],
        "persist_memory",
    )
    builder.add_edge("persist_memory", END)
    return builder.compile(checkpointer=checkpointer)
