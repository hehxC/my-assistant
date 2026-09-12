from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth import hash_session_token, utc_now
from backend.app.database import Base, get_db
from backend.app.graph import create_chat_graph
from backend.app.main import app
from backend.app.models import ApplicationState, AuthSession, ChatThread, StudySession, User


class FakeGraph:
    def __init__(self):
        self.calls = []
        self.states = {}

    async def ainvoke(self, state, config):
        self.calls.append({"state": state, "config": config})
        thread_id = config["configurable"]["thread_id"]
        result = [*state["messages"], AIMessage(content="任务已经收到。")]
        self.states[thread_id] = result
        return {"messages": result}

    async def aget_state(self, config):
        thread_id = config["configurable"]["thread_id"]
        return SimpleNamespace(values={"messages": self.states.get(thread_id, [])})


@pytest.fixture
def api_context(monkeypatch):
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(bind=test_engine, expire_on_commit=False)
    Base.metadata.create_all(bind=test_engine)
    with testing_session.begin() as db:
        db.add(ApplicationState(id=1))

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    fake_graph = FakeGraph()
    monkeypatch.setattr(app.state, "chat_graph", fake_graph, raising=False)
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    yield client, testing_session, fake_graph

    app.dependency_overrides.pop(get_db, None)
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


def register(client, username="alice", password="password123", legacy_thread_id=None):
    payload = {"username": username, "password": password}
    if legacy_thread_id:
        payload["legacy_thread_id"] = legacy_thread_id
    return client.post("/api/auth/register", json=payload)


def test_chat_graph_uses_valid_checkpointer():
    checkpointer = InMemorySaver()
    graph = create_chat_graph(checkpointer)
    assert graph.checkpointer is checkpointer


def test_health():
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_register_login_logout_and_password_storage(api_context):
    client, testing_session, _ = api_context

    response = register(client)
    assert response.status_code == 201
    assert response.json()["user"]["username"] == "alice"
    assert response.json()["claimed_legacy_data"] is True
    assert "HttpOnly" in response.headers["set-cookie"]
    assert client.get("/api/auth/me").status_code == 200

    with testing_session() as db:
        user = db.scalar(select(User).where(User.username_normalized == "alice"))
        session = db.scalar(select(AuthSession).where(AuthSession.user_id == user.id))
        assert user.password_hash != "password123"
        assert session.token_hash != client.cookies.get("assistant_session")
        assert session.token_hash == hash_session_token(client.cookies["assistant_session"])

    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401

    login = client.post(
        "/api/auth/login",
        json={"username": " ALICE ", "password": "password123"},
    )
    assert login.status_code == 200
    assert client.get("/api/auth/me").json()["username"] == "alice"


def test_duplicate_username_wrong_password_and_expired_session(api_context):
    client, testing_session, _ = api_context
    assert register(client, "Alice").status_code == 201
    assert register(TestClient(app), " alice ").status_code == 409

    other_client = TestClient(app)
    failed = other_client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "wrong"},
    )
    assert failed.status_code == 401
    assert failed.json()["detail"] == "用户名或密码错误"

    with testing_session.begin() as db:
        auth_session = db.scalar(select(AuthSession))
        auth_session.expires_at = utc_now() - timedelta(seconds=1)
    assert client.get("/api/auth/me").status_code == 401


def test_first_account_claims_legacy_data_only_once(api_context):
    first_client, testing_session, _ = api_context
    with testing_session.begin() as db:
        db.add(
            StudySession(
                started_at=datetime(2026, 9, 12, 8, 0, 0),
                stopped_at=datetime(2026, 9, 12, 9, 0, 0),
                duration_seconds=3600,
            )
        )

    first = register(first_client, "first-user", legacy_thread_id="legacy-thread")
    second_client = TestClient(app)
    second = register(second_client, "second-user")

    assert first.json()["claimed_legacy_data"] is True
    assert second.json()["claimed_legacy_data"] is False
    with testing_session() as db:
        first_user = db.scalar(select(User).where(User.username_normalized == "first-user"))
        legacy_study = db.scalar(select(StudySession))
        legacy_thread = db.get(ChatThread, "legacy-thread")
        assert legacy_study.user_id == first_user.id
        assert legacy_thread.user_id == first_user.id


def test_study_records_are_isolated_between_users(api_context, monkeypatch):
    alice, _, _ = api_context
    bob = TestClient(app)
    register(alice, "alice")
    register(bob, "bobby")

    times = iter(
        [datetime(2026, 9, 12, 8, 0, 0), datetime(2026, 9, 12, 8, 1, 30)]
    )
    monkeypatch.setattr("backend.app.study.utc_now", lambda: next(times))

    started = alice.post("/api/study/start")
    session_id = started.json()["session_id"]
    assert bob.post("/api/study/stop", json={"session_id": session_id}).status_code == 404

    stopped = alice.post("/api/study/stop", json={"session_id": session_id})
    assert stopped.status_code == 200
    assert stopped.json()["duration_seconds"] == 90

    alice_stats = alice.get(
        "/api/study/stats",
        params={"start_date": "2026-09-12", "end_date": "2026-09-12"},
    )
    bob_stats = bob.get(
        "/api/study/stats",
        params={"start_date": "2026-09-12", "end_date": "2026-09-12"},
    )
    assert alice_stats.json()["total_seconds"] == 90
    assert alice_stats.json()["days"][0]["periods"] == [
        {"start_time": "16:00", "end_time": "16:01", "duration_seconds": 90}
    ]
    assert bob_stats.json()["total_seconds"] == 0


def test_study_periods_are_clipped_at_local_midnight(api_context):
    client, testing_session, _ = api_context
    register(client, "alice")

    with testing_session.begin() as db:
        user_id = db.scalar(select(User.id).where(User.username_normalized == "alice"))
        db.add(
            StudySession(
                user_id=user_id,
                # UTC 15:30 到 16:30 对应中国时间 23:30 到次日 00:30。
                started_at=datetime(2026, 9, 12, 15, 30, 0),
                stopped_at=datetime(2026, 9, 12, 16, 30, 0),
                duration_seconds=3600,
            )
        )

    response = client.get(
        "/api/study/stats",
        params={"start_date": "2026-09-12", "end_date": "2026-09-13"},
    )
    assert response.status_code == 200
    assert [day["duration_seconds"] for day in response.json()["days"]] == [1800, 1800]
    assert response.json()["days"][0]["periods"][0] == {
        "start_time": "23:30",
        "end_time": "24:00",
        "duration_seconds": 1800,
    }
    assert response.json()["days"][1]["periods"][0] == {
        "start_time": "00:00",
        "end_time": "00:30",
        "duration_seconds": 1800,
    }


def test_chat_thread_and_history_are_isolated(api_context):
    alice, _, fake_graph = api_context
    bob = TestClient(app)
    register(alice, "alice")
    register(bob, "bobby")

    created = alice.post("/api/chat/threads")
    thread_id = created.json()["thread_id"]
    response = alice.post(
        "/api/chat",
        json={"thread_id": thread_id, "message": "整理今天的任务"},
    )
    assert response.status_code == 200
    assert response.json()["content"] == "任务已经收到。"
    assert fake_graph.calls[0]["config"] == {"configurable": {"thread_id": thread_id}}

    history = alice.get(f"/api/chat/threads/{thread_id}/messages")
    assert history.status_code == 200
    assert [item["role"] for item in history.json()["messages"]] == ["user", "assistant"]

    assert bob.get(f"/api/chat/threads/{thread_id}/messages").status_code == 404
    assert bob.post(
        "/api/chat",
        json={"thread_id": thread_id, "message": "越权消息"},
    ).status_code == 404


def test_protected_routes_require_login(api_context):
    client, _, _ = api_context
    assert client.post("/api/chat/threads").status_code == 401
    assert client.post("/api/study/start").status_code == 401
