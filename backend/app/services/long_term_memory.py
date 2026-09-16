from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from ..database import SessionLocal
from ..models import LongTermMemory


MemoryCategory = Literal["current_goal", "preference", "recommendation"]
MemoryAction = Literal["create", "update", "complete", "delete", "feedback", "none"]
MemoryChangeAction = Literal[
    "created",
    "updated",
    "completed",
    "deleted",
    "awaiting_feedback",
    "feedback_received",
]
CURRENT_GOAL_TTL = timedelta(days=30)
RECOMMENDATION_TTL = timedelta(days=7)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RecommendationDetails(BaseModel):
    subject_type: str = Field(min_length=1, max_length=30)
    subject_name: str = Fild(min_length=1, max_length=60)
    reason: str = Field(default="", max_length=200)
    attributes: list[str] = Field(default_factory=list, max_length=5)


class AssistantRecommendation(RecommendationDetails):
    pass


class ChatTurnOutput(BaseModel):
    reply: str = Field(min_length=1, max_length=8000)
    recommendations: list[AssistantRecommendation] = Field(default_factory=list, max_length=10)


class DerivedPreference(BaseModel):
    topic: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=200)


class MemoryContext(BaseModel):
    id: int
    category: MemoryCategory
    topic: str
    content: str
    status: Literal["active", "completed"]
    expired: bool = False
    feedback_status: Literal["pending", "expired"] | None = None
    details: RecommendationDetails | None = None


class MemoryLoadResult(BaseModel):
    active_memories: list[MemoryContext] = Field(default_factory=list)
    known_memories: list[MemoryContext] = Field(default_factory=list)
    pending_recommendations: list[MemoryContext] = Field(default_factory=list)


class ProposedMemoryAction(BaseModel):
    action: MemoryAction
    memory_id: int | None = None
    category: Literal["current_goal", "preference"] | None = None
    topic: str = Field(default="", max_length=100)
    content: str = Field(default="", max_length=200)
    derived_preference: DerivedPreference | None = None

    @model_validator(mode="after")
    def validate_action_fields(self):
        """限制每类操作所需字段，避免把不完整的模型输出交给数据库。"""

        if self.action == "create" and (
            self.memory_id is not None
            or self.category is None
            or not self.topic.strip()
            or not self.content.strip()
        ):
            raise ValueError("新增记忆缺少必要字段")
        if self.action == "update" and (
            self.memory_id is None or not self.content.strip()
        ):
            raise ValueError("更新记忆缺少必要字段")
        if self.action in {"complete", "delete"} and self.memory_id is None:
            raise ValueError("记忆操作缺少 memory_id")
        if self.action == "feedback" and (
            self.memory_id is None or not self.content.strip()
        ):
            raise ValueError("推荐反馈缺少必要字段")
        return self


class MemoryExtraction(BaseModel):
    recommendation_requested: bool = False
    actions: list[ProposedMemoryAction] = Field(default_factory=list, max_length=10)


class MemoryChange(BaseModel):
    action: MemoryChangeAction
    category: MemoryCategory
    content: str


SENSITIVE_MARKERS = (
    "密码",
    "口令",
    "apikey",
    "api key",
    "密钥",
    "身份证",
    "护照号",
    "银行卡",
    "卡号",
    "精确住址",
    "家庭住址",
    "病史",
    "诊断",
    "患有",
    "过敏",
    "用药",
    "手术",
    "抑郁症",
    "焦虑症",
    "性取向",
    "性生活",
    "政治倾向",
    "党员",
    "宗教信仰",
    "佛教徒",
    "基督徒",
    "穆斯林",
)


def contains_sensitive_content(value: str) -> bool:
    normalized = value.casefold().replace("_", " ").replace("-", " ")
    return any(marker in normalized for marker in SENSITIVE_MARKERS)


class LongTermMemoryRepository:
    """用两个公开方法封装长期记忆的查询、过期和事务写入规则。"""

    def __init__(self, session_factory: sessionmaker[Session] = SessionLocal):
        self.session_factory = session_factory

    def load_context(
        self,
        user_id: int,
        now: datetime | None = None,
    ) -> MemoryLoadResult:
        current_time = now or utc_now()
        with self.session_factory.begin() as db:
            # 推荐过期采用惰性同步，但查询条件同时按时间过滤，保证逻辑上准时失效。
            db.execute(
                update(LongTermMemory)
                .where(
                    LongTermMemory.user_id == user_id,
                    LongTermMemory.category == "recommendation",
                    LongTermMemory.feedback_status == "pending",
                    LongTermMemory.expires_at <= current_time,
                )
                .values(feedback_status="expired")
            )
            # 查询仍然生效的当前目标和稳定偏好，作为背景信息注入本轮聊天回复。
            # 推荐记忆单独由 pending_records 管理，避免和用户自身信息混在一起。
            active_records = db.scalars(
                select(LongTermMemory)
                .where(
                    LongTermMemory.user_id == user_id,
                    LongTermMemory.category.in_(["current_goal", "preference"]),
                    LongTermMemory.status == "active",
                    or_(
                        LongTermMemory.expires_at.is_(None),
                        LongTermMemory.expires_at > current_time,
                    ),
                )
                .order_by(LongTermMemory.updated_at.desc())
                .limit(50)
            ).all()
            # 查询七天有效期内仍在等待反馈的推荐，供助手在相关话题中追问，
            # 也供记忆提取模型把“很好看”“第一个不错”等反馈匹配到具体记录。
            pending_records = db.scalars(
                select(LongTermMemory)
                .where(
                    LongTermMemory.user_id == user_id,
                    LongTermMemory.category == "recommendation",
                    LongTermMemory.status == "active",
                    LongTermMemory.feedback_status == "pending",
                    LongTermMemory.expires_at > current_time,
                )
                .order_by(LongTermMemory.updated_at.desc())
                .limit(50)
            ).all()
            # 查询最近的全部记忆，包括已完成目标和已过期推荐。
            # 它们不会直接注入回复，但可用于识别更新、删除、重新激活等用户指令。
            known_records = db.scalars(
                select(LongTermMemory)
                .where(LongTermMemory.user_id == user_id)
                .order_by(LongTermMemory.updated_at.desc())
                .limit(50)
            ).all()

            return MemoryLoadResult(
                active_memories=[
                    self._to_context(record, current_time) for record in active_records
                ],
                pending_recommendations=[
                    self._to_context(record, current_time) for record in pending_records
                ],
                known_memories=[
                    self._to_context(record, current_time) for record in known_records
                ],
            )

    def apply_turn(
        self,
        user_id: int,
        thread_id: str,
        user_actions: list[ProposedMemoryAction],
        assistant_recommendations: list[AssistantRecommendation],
        now: datetime | None = None,
    ) -> list[MemoryChange]:
        current_time = now or utc_now()
        changes = []
        with self.session_factory.begin() as db:
            for action in user_actions:
                changes.extend(
                    self._apply_user_action(db, user_id, thread_id, action, current_time)
                )
            for recommendation in assistant_recommendations[:10]:
                change = self._save_recommendation(
                    db, user_id, thread_id, recommendation, current_time
                )
                if change:
                    changes.append(change)
        return changes

    def _apply_user_action(
        self,
        db: Session,
        user_id: int,
        thread_id: str,
        action: ProposedMemoryAction,
        now: datetime,
    ) -> list[MemoryChange]:
        if action.action == "none":
            return []
        if action.action == "create":
            if contains_sensitive_content(f"{action.topic} {action.content}"):
                return []
            return [
                self._upsert_personal_memory(
                    db,
                    user_id,
                    thread_id,
                    action.category,
                    action.topic,
                    action.content,
                    now,
                )
            ]

        # 所有按 ID 的操作都同时校验 user_id，模型无法修改其他账号的记忆。
        record = db.scalar(
            select(LongTermMemory).where(
                LongTermMemory.id == action.memory_id,
                LongTermMemory.user_id == user_id,
            )
        )
        if record is None:
            raise ValueError("模型引用了不存在的记忆")
        if action.action == "feedback":
            return self._apply_feedback(db, record, thread_id, action, now)
        if action.action == "delete":
            change = MemoryChange(
                action="deleted", category=record.category, content=record.content
            )
            db.delete(record)
            return [change]
        if record.category == "recommendation":
            raise ValueError("推荐记忆只能反馈或删除")
        if action.action == "update" and contains_sensitive_content(action.content):
            return []
        if action.action == "complete":
            if record.category != "current_goal":
                raise ValueError("只有当前目标可以标记为完成")
            record.status = "completed"
            change_action: MemoryChangeAction = "completed"
        else:
            record.content = action.content.strip()
            record.status = "active"
            record.last_confirmed_at = now
            record.expires_at = (
                now + CURRENT_GOAL_TTL if record.category == "current_goal" else None
            )
            change_action = "updated"
        record.source_thread_id = thread_id
        record.updated_at = now
        return [
            MemoryChange(
                action=change_action,
                category=record.category,
                content=record.content,
            )
        ]

    def _apply_feedback(
        self,
        db: Session,
        record: LongTermMemory,
        thread_id: str,
        action: ProposedMemoryAction,
        now: datetime,
    ) -> list[MemoryChange]:
        # action.content 保存本轮用户反馈原文，只用于模型判断和本次事务处理；
        # 当前产品不保留反馈历史，因此不会把原文写回 long_term_memories。

        # 第一步：反馈只能消费仍在七天有效期内、属于当前账号的待反馈推荐。
        if (
            record.category != "recommendation"
            or record.feedback_status != "pending"
            or record.expires_at is None
            or record.expires_at <= now
        ):
            raise ValueError("推荐已过期或不在等待反馈")

        subject_name = (record.details or {}).get("subject_name", record.content)

        # 第二步：MemoryChange 只返回给本次聊天接口，用于前端显示“已记录反馈”，
        # 它不是数据库模型，不会成为新的长期记忆。
        changes = [
            MemoryChange(
                action="feedback_received",
                category="recommendation",
                content=subject_name,
            )
        ]
        # 第三步：只有反馈中存在明确且非敏感的偏好时，才把归纳结果长期保存。
        if action.derived_preference and not contains_sensitive_content(
            f"{action.derived_preference.topic} {action.derived_preference.content}"
        ):
            preference = action.derived_preference
            changes.append(
                self._upsert_personal_memory(
                    db,
                    record.user_id,
                    thread_id,
                    "preference",
                    preference.topic,
                    preference.content,
                    now,
                )
            )
        # 第四步：反馈闭环完成后删除临时推荐；偏好（如果有）已经在同一事务中写入。
        db.delete(record)
        return changes

    def _upsert_personal_memory(
        self,
        db: Session,
        user_id: int,
        thread_id: str,
        category: Literal["current_goal", "preference"],
        topic: str,
        content: str,
        now: datetime,
    ) -> MemoryChange:
        normalized_topic = topic.strip()
        normalized_content = content.strip()
        record = db.scalar(
            select(LongTermMemory).where(
                LongTermMemory.user_id == user_id,
                LongTermMemory.category == category,
                LongTermMemory.topic == normalized_topic,
            )
        )
        change_action: MemoryChangeAction = "updated" if record else "created"
        if record is None:
            record = LongTermMemory(
                user_id=user_id,
                category=category,
                topic=normalized_topic,
                content=normalized_content,
                status="active",
                source_thread_id=thread_id,
                created_at=now,
                updated_at=now,
                last_confirmed_at=now,
            )
            db.add(record)
        else:
            record.content = normalized_content
            record.status = "active"
            record.source_thread_id = thread_id
            record.updated_at = now
            record.last_confirmed_at = now
        record.expires_at = now + CURRENT_GOAL_TTL if category == "current_goal" else None
        record.feedback_status = None
        record.details = None
        return MemoryChange(
            action=change_action,
            category=category,
            content=normalized_content,
        )

    def _save_recommendation(
        self,
        db: Session,
        user_id: int,
        thread_id: str,
        recommendation: AssistantRecommendation,
        now: datetime,
    ) -> MemoryChange | None:
        details = recommendation.model_dump()
        if contains_sensitive_content(str(details)):
            return None
        topic = f"{recommendation.subject_type.strip()}/{recommendation.subject_name.strip()}"
        record = db.scalar(
            select(LongTermMemory).where(
                LongTermMemory.user_id == user_id,
                LongTermMemory.category == "recommendation",
                LongTermMemory.topic == topic,
            )
        )
        if (
            record
            and record.feedback_status == "pending"
            and record.expires_at
            and record.expires_at > now
        ):
            return None
        if record is None:
            record = LongTermMemory(
                user_id=user_id,
                category="recommendation",
                topic=topic,
                content=recommendation.subject_name.strip(),
                status="active",
                source_thread_id=thread_id,
                created_at=now,
                updated_at=now,
                last_confirmed_at=now,
            )
            db.add(record)
        else:
            record.content = recommendation.subject_name.strip()
            record.status = "active"
            record.source_thread_id = thread_id
            record.updated_at = now
            record.last_confirmed_at = now
        record.expires_at = now + RECOMMENDATION_TTL
        record.feedback_status = "pending"
        record.details = details
        return MemoryChange(
            action="awaiting_feedback",
            category="recommendation",
            content=record.content,
        )

    @staticmethod
    def _to_context(record: LongTermMemory, now: datetime) -> MemoryContext:
        details = (
            RecommendationDetails.model_validate(record.details)
            if record.details
            else None
        )
        return MemoryContext(
            id=record.id,
            category=record.category,
            topic=record.topic,
            content=record.content,
            status=record.status,
            expired=bool(record.expires_at and record.expires_at <= now),
            feedback_status=record.feedback_status,
            details=details,
        )
