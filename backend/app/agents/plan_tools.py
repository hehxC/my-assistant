from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


class PlanToolContext(BaseModel):
    plan_date: str
    adcode: str | None = None
    city_name: str | None = None
    admin1: str = ""
    latitude: float | None = None
    longitude: float | None = None
    timezone: str = "Asia/Shanghai"


class ToolRequest(BaseModel):
    tool_name: str
    query: str = ""


class PlanToolOutput(BaseModel):
    data: dict[str, Any]
    allowed_periods: list[str] = Field(default_factory=list)


class ToolExecutionResult(BaseModel):
    hobby_id: int
    tool_name: str
    query: str
    succeeded: bool
    output: PlanToolOutput | None = None
    error: str | None = None


ToolExecutor = Callable[[PlanToolContext, str], Awaitable[PlanToolOutput]]
ToolCacheKey = Callable[[PlanToolContext, str], str]


@dataclass(frozen=True)
class RegisteredPlanTool:
    """描述一个可由计划 Graph 选择并执行的工具 Adapter。"""

    name: str
    description: str
    executor: ToolExecutor
    cache_key: ToolCacheKey
