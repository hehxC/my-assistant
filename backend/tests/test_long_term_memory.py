import asyncio
import json
from datetime import datetime, timedelta

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.agents.chat_graph import (
    ChatToolDecision,
    ChatToolRequest,
    create_chat_graph,
)
from backend.app.database import Base
from backend.app.models import ChatThread, LongTermMemory, User
from backend.app.services.library import LibrarySource
from backend.app.services.long_term_memory import (
    AssistantRecommendation,
    ChatTurnOutput,
    DerivedPreference,
    LongTermMemoryRepository,
    MemoryExtraction,
    ProposedMemoryAction,
)


class ResultRunnable:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        result = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def memory_context():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    now = datetime(2026, 9, 14, 8, 0, 0)
    with testing_session.begin() as db:
        db.add_all(
            [
                User(
                    id=1,
                    username="alice",
                    username_normalized="alice",
                    password_hash="hash",
                    created_at=now,
                ),
                User(
                    id=2,
                    username="bobby",
                    username_normalized="bobby",
                    password_hash="hash",
                    created_at=now,
                ),
                ChatThread(id="thread-1", user_id=1, created_at=now),
                ChatThread(id="thread-2", user_id=1, created_at=now),
                ChatThread(id="bob-thread", user_id=2, created_at=now),
            ]
        )
    yield LongTermMemoryRepository(testing_session), testing_session, now
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def create_goal(content="最近正在找工作"):
    return ProposedMemoryAction(
        action="create",
        category="current_goal",
        topic="求职状态",
        content=content,
    )


def recommend(subject_type="电影", subject_name="星际穿越"):
    return AssistantRecommendation(
        subject_type=subject_type,
        subject_name=subject_name,
        reason="适合喜欢宏大叙事的用户",
        attributes=["科幻", "太空", "情感"],
    )


def test_long_term_memory_table_and_columns_have_database_comments():
    table = LongTermMemory.__table__

    assert table.comment
    assert all(column.comment for column in table.columns)


def test_current_goal_refreshes_for_30_days_without_duplication(memory_context):
    repository, testing_session, now = memory_context
    first = repository.apply_turn(1, "thread-1", [create_goal()], [], now)
    later = now + timedelta(days=5)
    second = repository.apply_turn(
        1,
        "thread-2",
        [create_goal("正在寻找后端开发工作")],
        [],
        later,
    )

    with testing_session() as db:
        records = db.scalars(select(LongTermMemory)).all()
        assert len(records) == 1
        assert records[0].content == "正在寻找后端开发工作"
        assert records[0].expires_at == later + timedelta(days=30)
        assert records[0].source_thread_id == "thread-2"
    assert first[0].action == "created"
    assert second[0].action == "updated"


def test_preference_never_expires_and_expired_goal_is_not_loaded(memory_context):
    repository, testing_session, now = memory_context
    repository.apply_turn(
        1,
        "thread-1",
        [
            ProposedMemoryAction(
                action="create",
                category="preference",
                topic="回复风格",
                content="喜欢简短回答",
            ),
            create_goal(),
        ],
        [],
        now,
    )

    active = repository.load_context(1, now + timedelta(days=31)).active_memories

    assert [item.content for item in active] == ["喜欢简短回答"]
    with testing_session() as db:
        preference = db.scalar(
            select(LongTermMemory).where(LongTermMemory.category == "preference")
        )
        assert preference.expires_at is None


def test_complete_delete_and_user_ownership(memory_context):
    repository, testing_session, now = memory_context
    repository.apply_turn(1, "thread-1", [create_goal()], [], now)
    memory_id = repository.load_context(1, now).active_memories[0].id

    completed = repository.apply_turn(
        1,
        "thread-1",
        [ProposedMemoryAction(action="complete", memory_id=memory_id)],
        [],
        now,
    )
    assert completed[0].action == "completed"
    assert repository.load_context(1, now).active_memories == []

    with pytest.raises(ValueError):
        repository.apply_turn(
            2,
            "bob-thread",
            [ProposedMemoryAction(action="delete", memory_id=memory_id)],
            [],
            now,
        )

    deleted = repository.apply_turn(
        1,
        "thread-1",
        [ProposedMemoryAction(action="delete", memory_id=memory_id)],
        [],
        now,
    )
    assert deleted[0].action == "deleted"
    with testing_session() as db:
        assert db.get(LongTermMemory, memory_id) is None


def test_recommendations_are_saved_separately_and_duplicates_do_not_extend(memory_context):
    repository, testing_session, now = memory_context
    recommendations = [recommend(), recommend("书籍", "深度工作")]

    first = repository.apply_turn(1, "thread-1", [], recommendations, now)
    duplicate = repository.apply_turn(
        1,
        "thread-2",
        [],
        [recommend()],
        now + timedelta(days=2),
    )

    assert [change.action for change in first] == ["awaiting_feedback"] * 2
    assert duplicate == []
    context = repository.load_context(1, now + timedelta(days=2))
    assert len(context.pending_recommendations) == 2
    with testing_session() as db:
        movie = db.scalar(
            select(LongTermMemory).where(LongTermMemory.topic == "电影/星际穿越")
        )
        assert movie.feedback_status == "pending"
        assert movie.expires_at == now + timedelta(days=7)
        assert movie.details["attributes"] == ["科幻", "太空", "情感"]


def test_expired_recommendation_is_retained_then_can_be_reactivated(memory_context):
    repository, testing_session, now = memory_context
    repository.apply_turn(1, "thread-1", [], [recommend()], now)

    expired_context = repository.load_context(1, now + timedelta(days=7))

    assert expired_context.pending_recommendations == []
    with testing_session() as db:
        record = db.scalar(select(LongTermMemory))
        assert record.feedback_status == "expired"

    changes = repository.apply_turn(
        1,
        "thread-2",
        [],
        [recommend()],
        now + timedelta(days=8),
    )
    assert changes[0].action == "awaiting_feedback"
    with testing_session() as db:
        record = db.scalar(select(LongTermMemory))
        assert record.feedback_status == "pending"
        assert record.expires_at == now + timedelta(days=15)


def test_feedback_deletes_recommendation_and_creates_preference(memory_context):
    repository, testing_session, now = memory_context
    repository.apply_turn(1, "thread-1", [], [recommend()], now)
    recommendation_id = repository.load_context(1, now).pending_recommendations[0].id
    action = ProposedMemoryAction(
        action="feedback",
        memory_id=recommendation_id,
        content="很好看，我喜欢这种科幻题材",
        derived_preference=DerivedPreference(
            topic="电影题材",
            content="喜欢科幻题材电影",
        ),
    )

    changes = repository.apply_turn(
        1,
        "thread-2",
        [action],
        [],
        now + timedelta(days=1),
    )

    assert [change.action for change in changes] == ["feedback_received", "created"]
    with testing_session() as db:
        records = db.scalars(select(LongTermMemory)).all()
        assert len(records) == 1
        assert records[0].category == "preference"
        assert records[0].content == "喜欢科幻题材电影"
        assert records[0].expires_at is None


def test_feedback_without_evaluation_only_deletes_recommendation(memory_context):
    repository, testing_session, now = memory_context
    repository.apply_turn(1, "thread-1", [], [recommend()], now)
    recommendation_id = repository.load_context(1, now).pending_recommendations[0].id

    changes = repository.apply_turn(
        1,
        "thread-2",
        [
            ProposedMemoryAction(
                action="feedback",
                memory_id=recommendation_id,
                content="我看过了",
            )
        ],
        [],
        now + timedelta(days=1),
    )

    assert [change.action for change in changes] == ["feedback_received"]
    with testing_session() as db:
        assert db.scalars(select(LongTermMemory)).all() == []


def test_expired_or_other_users_recommendation_cannot_receive_feedback(memory_context):
    repository, _, now = memory_context
    repository.apply_turn(1, "thread-1", [], [recommend()], now)
    recommendation_id = repository.load_context(1, now).pending_recommendations[0].id
    feedback = ProposedMemoryAction(
        action="feedback",
        memory_id=recommendation_id,
        content="很好",
    )

    with pytest.raises(ValueError):
        repository.apply_turn(2, "bob-thread", [feedback], [], now)
    with pytest.raises(ValueError):
        repository.apply_turn(
            1,
            "thread-2",
            [feedback],
            [],
            now + timedelta(days=7),
        )


def test_chat_turn_saves_each_structured_recommendation_with_one_model_call(memory_context):
    repository, _, now = memory_context
    chat_model = ResultRunnable(
        ChatTurnOutput(
            reply="可以试试《星际穿越》和《深度工作》。",
            recommendations=[recommend(), recommend("书籍", "深度工作")],
        )
    )
    graph = create_chat_graph(
        InMemorySaver(),
        memory_repository=repository,
        chat_model=chat_model,
        memory_extractor=ResultRunnable(
            MemoryExtraction(recommendation_requested=True, actions=[])
        ),
    )

    result = asyncio.run(
        graph.ainvoke(
            {"messages": [HumanMessage(content="推荐一部电影和一本书")]},
            {"configurable": {"thread_id": "thread-1", "user_id": 1}},
        )
    )

    assert len(chat_model.calls) == 1
    assert result["messages"][-1].content == "可以试试《星际穿越》和《深度工作》。"
    assert [change.action for change in result["memory_changes"]] == [
        "awaiting_feedback",
        "awaiting_feedback",
    ]
    assert len(repository.load_context(1, now).pending_recommendations) == 2


def test_unsolicited_assistant_suggestion_is_not_saved(memory_context):
    repository, _, now = memory_context
    graph = create_chat_graph(
        InMemorySaver(),
        memory_repository=repository,
        chat_model=ResultRunnable(
            ChatTurnOutput(
                reply="也许可以看看《星际穿越》。",
                recommendations=[recommend()],
            )
        ),
        memory_extractor=ResultRunnable(MemoryExtraction(actions=[])),
    )

    result = asyncio.run(
        graph.ainvoke(
            {"messages": [HumanMessage(content="今天有点累")]},
            {"configurable": {"thread_id": "thread-1", "user_id": 1}},
        )
    )

    assert result["memory_changes"] == []
    assert repository.load_context(1, now).pending_recommendations == []


def test_chat_graph_recalls_memory_in_a_new_thread(memory_context):
    repository, _, _ = memory_context
    chat_model = ResultRunnable(
        ChatTurnOutput(reply="先一起看看机会。"),
        ChatTurnOutput(reply="记得。"),
    )
    extractor = ResultRunnable(
        MemoryExtraction(actions=[create_goal()]),
        MemoryExtraction(actions=[]),
    )
    graph = create_chat_graph(
        InMemorySaver(),
        memory_repository=repository,
        chat_model=chat_model,
        memory_extractor=extractor,
    )

    first = asyncio.run(
        graph.ainvoke(
            {"messages": [HumanMessage(content="我最近在找工作")]},
            {"configurable": {"thread_id": "thread-1", "user_id": 1}},
        )
    )
    second = asyncio.run(
        graph.ainvoke(
            {"messages": [HumanMessage(content="你还记得我最近在做什么吗？")]},
            {"configurable": {"thread_id": "thread-2", "user_id": 1}},
        )
    )

    assert first["memory_changes"][0].content == "最近正在找工作"
    assert second["memory_changes"] == []
    assert any("最近正在找工作" in str(message.content) for message in chat_model.calls[1])


def test_chat_model_receives_json_wrapped_assistant_history(memory_context):
    repository, _, _ = memory_context
    chat_model = ResultRunnable(
        ChatTurnOutput(reply="第一轮回复。"),
        ChatTurnOutput(reply="第二轮回复。"),
    )
    graph = create_chat_graph(
        InMemorySaver(),
        memory_repository=repository,
        chat_model=chat_model,
        memory_extractor=ResultRunnable(MemoryExtraction(actions=[])),
    )
    config = {"configurable": {"thread_id": "thread-1", "user_id": 1}}

    asyncio.run(
        graph.ainvoke(
            {"messages": [HumanMessage(content="第一轮问题")]},
            config,
        )
    )
    asyncio.run(
        graph.ainvoke(
            {"messages": [HumanMessage(content="第二轮问题")]},
            config,
        )
    )

    assistant_history = [
        message for message in chat_model.calls[1] if isinstance(message, AIMessage)
    ]
    assert len(assistant_history) == 1
    assert json.loads(str(assistant_history[0].content)) == {
        "reply": "第一轮回复。",
        "recommendations": [],
        "source_ids": [],
    }


def test_sensitive_memory_is_filtered_before_persistence(memory_context):
    repository, _, now = memory_context
    graph = create_chat_graph(
        InMemorySaver(),
        memory_repository=repository,
        chat_model=ResultRunnable(ChatTurnOutput(reply="请不要公开密钥。")),
        memory_extractor=ResultRunnable(
            MemoryExtraction(
                actions=[
                    ProposedMemoryAction(
                        action="create",
                        category="preference",
                        topic="API Key",
                        content="API Key 是 secret-value",
                    )
                ]
            )
        ),
    )

    result = asyncio.run(
        graph.ainvoke(
            {"messages": [HumanMessage(content="我的 API Key 是 secret-value")]},
            {"configurable": {"thread_id": "thread-1", "user_id": 1}},
        )
    )

    assert result["memory_changes"] == []
    assert repository.load_context(1, now).active_memories == []


def test_memory_failure_does_not_block_chat():
    class FailingRepository:
        def load_context(self, _user_id):
            raise RuntimeError("database down")

        def apply_turn(self, *_args):
            raise RuntimeError("database down")

    graph = create_chat_graph(
        InMemorySaver(),
        memory_repository=FailingRepository(),
        chat_model=ResultRunnable(ChatTurnOutput(reply="仍然可以回复。")),
        memory_extractor=ResultRunnable(MemoryExtraction(actions=[])),
    )

    result = asyncio.run(
        graph.ainvoke(
            {"messages": [HumanMessage(content="你好")]},
            {"configurable": {"thread_id": "thread-1", "user_id": 1}},
        )
    )

    assert result["messages"][-1].content == "仍然可以回复。"
    assert result["memory_changes"] == []
    assert result["memory_warning"] == "长期记忆暂时不可用"


def test_chat_graph_injects_library_context_and_keeps_only_valid_sources(memory_context):
    repository, _, _ = memory_context

    class FakeLibrary:
        async def retrieve(self, user_id, query, book_ids):
            assert user_id == 1
            assert query == "RAG 的定义和工作流程"
            assert book_ids == [7]
            return [
                LibrarySource(
                    source_id="S1",
                    book_id=7,
                    title="RAG 入门",
                    locator="第 12 页",
                    excerpt="RAG 会先检索相关资料，再生成回答。",
                )
            ]

    chat_model = ResultRunnable(
        ChatTurnOutput(
            reply="书中认为 RAG 会先检索再回答。",
            source_ids=["S1", "S999"],
        )
    )
    graph = create_chat_graph(
        InMemorySaver(),
        memory_repository=repository,
        chat_model=chat_model,
        memory_extractor=ResultRunnable(MemoryExtraction(actions=[])),
        tool_decider=ResultRunnable(
            ChatToolDecision(
                tool_requests=[
                    ChatToolRequest(
                        tool_name="retrieve_library",
                        query="RAG 的定义和工作流程",
                    )
                ]
            )
        ),
        library_retriever=FakeLibrary(),
    )

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="这本书如何解释 RAG？")],
                "rag_scope": "selected",
                "rag_book_ids": [7],
            },
            {"configurable": {"thread_id": "thread-1", "user_id": 1}},
        )
    )

    assert any("RAG 入门" in str(message.content) for message in chat_model.calls[0])
    saved_sources = result["messages"][-1].additional_kwargs["sources"]
    assert [source["source_id"] for source in saved_sources] == ["S1"]


def test_ordinary_chat_does_not_call_library(memory_context):
    repository, _, _ = memory_context

    class FailingLibrary:
        calls = 0

        async def retrieve(self, *_args):
            self.calls += 1
            raise RuntimeError("vector index down")

    library = FailingLibrary()
    graph = create_chat_graph(
        InMemorySaver(),
        memory_repository=repository,
        chat_model=ResultRunnable(ChatTurnOutput(reply="仍然可以正常回复。")),
        memory_extractor=ResultRunnable(MemoryExtraction(actions=[])),
        tool_decider=ResultRunnable(ChatToolDecision(tool_requests=[])),
        library_retriever=library,
    )

    result = asyncio.run(
        graph.ainvoke(
            {"messages": [HumanMessage(content="我最近很迷茫")], "rag_scope": "all"},
            {"configurable": {"thread_id": "thread-1", "user_id": 1}},
        )
    )

    assert result["messages"][-1].content == "仍然可以正常回复。"
    assert library.calls == 0
    assert result.get("rag_warning") is None


def test_requested_library_failure_does_not_block_chat(memory_context):
    repository, _, _ = memory_context

    class FailingLibrary:
        async def retrieve(self, user_id, query, book_ids):
            assert user_id == 1
            assert query == "书中对 RAG 的解释"
            assert book_ids is None
            raise RuntimeError("vector index down")

    graph = create_chat_graph(
        InMemorySaver(),
        memory_repository=repository,
        chat_model=ResultRunnable(ChatTurnOutput(reply="仍然可以正常回复。")),
        memory_extractor=ResultRunnable(MemoryExtraction(actions=[])),
        tool_decider=ResultRunnable(
            ChatToolDecision(
                tool_requests=[
                    ChatToolRequest(
                        tool_name="retrieve_library",
                        query="书中对 RAG 的解释",
                    )
                ]
            )
        ),
        library_retriever=FailingLibrary(),
    )

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="根据书库解释 RAG")],
                "rag_scope": "all",
            },
            {"configurable": {"thread_id": "thread-1", "user_id": 1}},
        )
    )

    assert result["messages"][-1].content == "仍然可以正常回复。"
    assert result["rag_warning"] == "书库检索暂时不可用"


def test_tool_decision_failure_retries_and_skips_library(memory_context):
    repository, _, _ = memory_context

    class RecordingLibrary:
        calls = 0

        async def retrieve(self, *_args):
            self.calls += 1
            return []

    library = RecordingLibrary()
    decider = ResultRunnable(ValueError("invalid json"))
    graph = create_chat_graph(
        InMemorySaver(),
        memory_repository=repository,
        chat_model=ResultRunnable(ChatTurnOutput(reply="先正常回复。")),
        memory_extractor=ResultRunnable(MemoryExtraction(actions=[])),
        tool_decider=decider,
        library_retriever=library,
    )

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="根据书库回答")],
                "rag_scope": "all",
            },
            {"configurable": {"thread_id": "thread-1", "user_id": 1}},
        )
    )

    assert len(decider.calls) == 3
    assert library.calls == 0
    assert result["messages"][-1].content == "先正常回复。"
    assert result["rag_warning"] == "书库检索暂时不可用"


def test_tool_decision_rejects_unknown_empty_and_multiple_requests():
    with pytest.raises(ValueError):
        ChatToolRequest(tool_name="weather", query="北京天气")
    with pytest.raises(ValueError):
        ChatToolRequest(tool_name="retrieve_library", query="")
    with pytest.raises(ValueError):
        ChatToolDecision(
            tool_requests=[
                ChatToolRequest(tool_name="retrieve_library", query="第一个查询"),
                ChatToolRequest(tool_name="retrieve_library", query="第二个查询"),
            ]
        )
