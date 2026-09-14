import json
from typing import Any, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, model_validator

from ..config import get_settings
from .plan_tools import (
    PlanToolContext,
    RegisteredPlanTool,
    ToolExecutionResult,
    ToolRequest,
)
from .prompts.plan_agent_prompt import DEPENDENCY_ANALYSIS_PROMPT, PLAN_COMPOSITION_PROMPT
from .weather import WEATHER_PLAN_TOOL


Suitability = Literal[
    "suitable",
    "conditional",
    "unsuitable",
    "no_check",
    "tool_unavailable",
]


class PlanGenerationError(RuntimeError):
    pass


class HobbyInput(BaseModel):
    id: int
    name: str
    note: str = ""


class PlanLocationInput(BaseModel):
    city_name: str
    admin1: str = ""
    adcode: str | None = None
    latitude: float
    longitude: float
    timezone: str = "Asia/Shanghai"


class DependencyAnalysis(BaseModel):
    hobby_id: int
    factors: list[str] = Field(default_factory=list)
    tool_requests: list[ToolRequest] = Field(default_factory=list)
    unsupported_factors: list[str] = Field(default_factory=list)


class DependencyAnalysisBatch(BaseModel):
    items: list[DependencyAnalysis]


class PlanItem(BaseModel):
    hobby_id: int
    hobby_name: str
    factors: list[str] = Field(default_factory=list)
    used_tools: list[str] = Field(default_factory=list)
    unsupported_factors: list[str] = Field(default_factory=list)
    suitability: Suitability
    advice: str
    recommended_periods: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_plan_item(cls, value):
        """兼容重构前按 weather/other/none 保存的计划内容。"""

        if not isinstance(value, dict) or "factor_type" not in value:
            return value
        migrated = dict(value)
        factor = migrated.get("factor", "")
        migrated.setdefault("factors", [factor] if factor else [])
        factor_type = migrated.get("factor_type")
        migrated.setdefault("used_tools", ["weather"] if factor_type == "weather" else [])
        migrated.setdefault(
            "unsupported_factors",
            [factor] if factor_type == "other" and factor else [],
        )
        if "recommended_periods" not in migrated:
            migrated["recommended_periods"] = [
                f"{item.get('start', '')}–{item.get('end', '')}"
                for item in migrated.get("recommended_time_ranges", [])
                if item.get("start") and item.get("end")
            ]
        return migrated


class PlanDraft(BaseModel):
    summary: str
    context_summary: str = ""
    items: list[PlanItem]

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_summary(cls, value):
        """把旧计划中的 weather_summary 映射到通用外部信息摘要。"""

        if isinstance(value, dict) and "context_summary" not in value:
            value = {**value, "context_summary": value.get("weather_summary", "")}
        return value


class PlanGraphState(TypedDict, total=False):
    hobbies: list[HobbyInput]
    location: PlanLocationInput | None
    plan_date: str
    analyses: list[DependencyAnalysis]
    tool_results: list[ToolExecutionResult]
    result: PlanDraft


async def _invoke_with_retry(runnable: Any, messages: list, validate):
    """调用结构化模型并校验结果，失败时额外重试一次。"""

    last_error: Exception | None = None
    for _ in range(2):
        try:
            return validate(await runnable.ainvoke(messages))
        except Exception as exc:
            last_error = exc
    raise PlanGenerationError("模型没有返回有效的计划数据") from last_error


def _validate_ids(actual_ids: list[int], hobbies: list[HobbyInput]) -> None:
    """确保模型没有遗漏、重复或凭空添加爱好。"""

    expected_ids = [hobby.id for hobby in hobbies]
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected_ids):
        raise ValueError("模型返回的爱好 ID 与输入不一致")


def _validate_recommended_periods(
    item: PlanItem,
    successful_results: list[ToolExecutionResult],
) -> None:
    """确保推荐时段来自相关工具声明的可选时段。"""

    constrained_results = [
        result
        for result in successful_results
        if result.output and result.output.allowed_periods
    ]
    if len(item.recommended_periods) != len(set(item.recommended_periods)):
        raise ValueError("推荐时段不能重复")
    for recommended in item.recommended_periods:
        for result in constrained_results:
            if recommended not in result.output.allowed_periods:
                raise ValueError("推荐时段不在工具结果提供的范围内")


def _normalize_plan(
    draft: PlanDraft,
    hobbies: list[HobbyInput],
    analyses: list[DependencyAnalysis],
    tool_results: list[ToolExecutionResult],
) -> PlanDraft:
    """用真实工具执行状态修正模型结果，防止编造未获取的外部信息。"""

    _validate_ids([item.hobby_id for item in draft.items], hobbies)
    hobby_by_id = {hobby.id: hobby for hobby in hobbies}
    analysis_by_id = {item.hobby_id: item for item in analyses}
    has_successful_tool = False

    for item in draft.items:
        hobby = hobby_by_id[item.hobby_id]
        analysis = analysis_by_id[item.hobby_id]
        related_results = [result for result in tool_results if result.hobby_id == item.hobby_id]
        successful_results = [result for result in related_results if result.succeeded]
        failed_tools = [result.tool_name for result in related_results if not result.succeeded]

        item.hobby_name = hobby.name
        item.factors = analysis.factors
        item.used_tools = [result.tool_name for result in successful_results]
        item.unsupported_factors = analysis.unsupported_factors
        has_successful_tool = has_successful_tool or bool(successful_results)

        unavailable = [*analysis.unsupported_factors, *failed_tools]
        if unavailable:
            item.suitability = "tool_unavailable"
            item.advice = f"{', '.join(unavailable)}暂时无法查询，请确认后再决定具体安排。"
            item.recommended_periods = []
        elif successful_results:
            if item.suitability not in {"suitable", "conditional", "unsuitable"}:
                raise ValueError("使用工具的计划返回了无效结论")
            _validate_recommended_periods(item, successful_results)
        else:
            item.suitability = "no_check"
            item.used_tools = []
            item.recommended_periods = []

    if not has_successful_tool:
        draft.context_summary = ""
    return draft


def create_plan_graph(
    analyzer: Any | None = None,
    composer: Any | None = None,
    tools: list[RegisteredPlanTool] | None = None,
):
    """创建“分析依赖、执行已注册工具、生成计划”的通用 LangGraph。"""

    registered_tools = tools if tools is not None else [WEATHER_PLAN_TOOL]
    tool_by_name = {tool.name: tool for tool in registered_tools}

    if analyzer is None or composer is None:
        settings = get_settings()
        if not settings.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        model = ChatOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            temperature=0.2,
            max_tokens=1600,
        )
        analyzer = analyzer or model.with_structured_output(
            DependencyAnalysisBatch,
            method="json_mode",
        )
        composer = composer or model.with_structured_output(PlanDraft, method="json_mode")

    async def analyze_dependencies(state: PlanGraphState):
        """让模型描述影响因素，并从运行时工具目录中选择所需工具。"""

        payload = [hobby.model_dump() for hobby in state["hobbies"]]
        tool_catalog = [
            {"name": tool.name, "description": tool.description}
            for tool in registered_tools
        ]
        messages = [
            SystemMessage(content=DEPENDENCY_ANALYSIS_PROMPT),
            HumanMessage(
                content=(
                    f"输出 Schema：{json.dumps(DependencyAnalysisBatch.model_json_schema(), ensure_ascii=False)}\n"
                    f"可用工具：{json.dumps(tool_catalog, ensure_ascii=False)}\n"
                    f"爱好列表：{json.dumps(payload, ensure_ascii=False)}"
                )
            ),
        ]

        def validate(result: DependencyAnalysisBatch):
            """验证爱好覆盖完整，并拒绝模型虚构的工具名。"""

            _validate_ids([item.hobby_id for item in result.items], state["hobbies"])
            requested_names = {
                request.tool_name
                for item in result.items
                for request in item.tool_requests
            }
            if not requested_names.issubset(tool_by_name):
                raise ValueError("模型选择了未注册的工具")
            return result.items

        analyses = await _invoke_with_retry(analyzer, messages, validate)
        return {"analyses": analyses}

    async def execute_tools(state: PlanGraphState):
        """通过统一注册表执行工具，并按缓存键复用相同外部查询。"""

        context = PlanToolContext(
            plan_date=state["plan_date"],
            **(state["location"].model_dump() if state.get("location") else {}),
        )
        cache = {}
        results = []
        for analysis in state["analyses"]:
            for request in analysis.tool_requests:
                tool = tool_by_name[request.tool_name]
                cache_key = tool.cache_key(context, request.query)
                if cache_key not in cache:
                    try:
                        cache[cache_key] = (True, await tool.executor(context, request.query), None)
                    except Exception as exc:
                        cache[cache_key] = (False, None, str(exc))
                succeeded, output, error = cache[cache_key]
                results.append(
                    ToolExecutionResult(
                        hobby_id=analysis.hobby_id,
                        tool_name=request.tool_name,
                        query=request.query,
                        succeeded=succeeded,
                        output=output,
                        error=error,
                    )
                )
        return {"tool_results": results}

    async def compose_plan(state: PlanGraphState):
        """汇总爱好、依赖和通用工具结果，生成最终结构化计划。"""

        context = {
            "date": state["plan_date"],
            "location": state["location"].model_dump() if state.get("location") else None,
            "hobbies": [item.model_dump() for item in state["hobbies"]],
            "analyses": [item.model_dump() for item in state["analyses"]],
            "tool_results": [item.model_dump() for item in state["tool_results"]],
        }
        messages = [
            SystemMessage(content=PLAN_COMPOSITION_PROMPT),
            HumanMessage(
                content=(
                    f"输出 Schema：{json.dumps(PlanDraft.model_json_schema(), ensure_ascii=False)}\n"
                    f"今日计划上下文：{json.dumps(context, ensure_ascii=False)}"
                )
            ),
        ]

        def validate(result: PlanDraft):
            """在接受模型结果前执行通用工具与计划约束。"""

            return _normalize_plan(
                result,
                state["hobbies"],
                state["analyses"],
                state["tool_results"],
            )

        result = await _invoke_with_retry(composer, messages, validate)
        return {"result": result}

    builder = StateGraph(PlanGraphState)
    builder.add_node("analyze_dependencies", analyze_dependencies)
    builder.add_node("execute_tools", execute_tools)
    builder.add_node("compose_plan", compose_plan)
    builder.add_edge(START, "analyze_dependencies")
    builder.add_edge("analyze_dependencies", "execute_tools")
    builder.add_edge("execute_tools", "compose_plan")
    builder.add_edge("compose_plan", END)
    return builder.compile()
