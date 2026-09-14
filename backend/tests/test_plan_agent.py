import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.app.agents.plan_agent import (
    DependencyAnalysis,
    DependencyAnalysisBatch,
    HobbyInput,
    PlanDraft,
    PlanGenerationError,
    PlanItem,
    PlanLocationInput,
    create_plan_graph,
)
from backend.app.agents.plan_tools import (
    PlanToolOutput,
    RegisteredPlanTool,
    ToolRequest,
)


class ResultRunnable:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    async def ainvoke(self, _messages):
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


def location():
    return PlanLocationInput(
        city_name="成都",
        admin1="四川省",
        adcode="510100",
        latitude=30.67,
        longitude=104.07,
    )


def registered_tool(name="weather", executor=None, cache_key=None):
    return RegisteredPlanTool(
        name=name,
        description=f"查询{name}",
        executor=executor or AsyncMock(return_value=PlanToolOutput(data={"result": "ok"})),
        cache_key=cache_key or (lambda _context, query: f"{name}:{query}"),
    )


def invoke(graph, hobbies):
    return asyncio.run(
        graph.ainvoke(
            {"hobbies": hobbies, "location": location(), "plan_date": "2026-09-14"}
        )
    )


def test_hobbies_without_tool_requests_skip_registered_tools():
    analyzer = ResultRunnable(
        DependencyAnalysisBatch(items=[DependencyAnalysis(hobby_id=1)])
    )
    composer = ResultRunnable(
        PlanDraft(
            summary="今天读书。",
            items=[
                PlanItem(
                    hobby_id=1,
                    hobby_name="读书",
                    suitability="no_check",
                    advice="晚上读半小时。",
                )
            ],
        )
    )
    executor = AsyncMock(return_value=PlanToolOutput(data={}))
    graph = create_plan_graph(analyzer, composer, [registered_tool(executor=executor)])

    result = invoke(graph, [HobbyInput(id=1, name="读书")])

    executor.assert_not_awaited()
    assert result["result"].items[0].suitability == "no_check"


def test_tool_registry_executes_selected_tool_and_reuses_cache():
    hobbies = [HobbyInput(id=1, name="骑车"), HobbyInput(id=2, name="跑步")]
    analyzer = ResultRunnable(
        DependencyAnalysisBatch(
            items=[
                DependencyAnalysis(
                    hobby_id=hobby.id,
                    factors=["降雨和风力"],
                    tool_requests=[ToolRequest(tool_name="weather", query=hobby.name)],
                )
                for hobby in hobbies
            ]
        )
    )
    composer = ResultRunnable(
        PlanDraft(
            summary="上午适合户外活动。",
            context_summary="天气稳定。",
            items=[
                PlanItem(
                    hobby_id=hobby.id,
                    hobby_name=hobby.name,
                    suitability="suitable",
                    advice="上午进行。",
                    recommended_periods=["白天"],
                )
                for hobby in hobbies
            ],
        )
    )
    executor = AsyncMock(
        return_value=PlanToolOutput(
            data={"weather": "fine"},
            allowed_periods=["白天", "夜间"],
        )
    )
    tool = registered_tool(executor=executor, cache_key=lambda _context, _query: "today")
    graph = create_plan_graph(analyzer, composer, [tool])

    result = invoke(graph, hobbies)

    executor.assert_awaited_once()
    assert [item.used_tools for item in result["result"].items] == [["weather"], ["weather"]]


def test_new_tool_can_be_registered_without_changing_graph_flow():
    analyzer = ResultRunnable(
        DependencyAnalysisBatch(
            items=[
                DependencyAnalysis(
                    hobby_id=1,
                    factors=["赛事日程"],
                    tool_requests=[ToolRequest(tool_name="cs_matches", query="今天的 CS 比赛")],
                )
            ]
        )
    )
    composer = ResultRunnable(
        PlanDraft(
            summary="晚上看比赛。",
            context_summary="今晚有一场比赛。",
            items=[
                PlanItem(
                    hobby_id=1,
                    hobby_name="看 CS 比赛",
                    suitability="suitable",
                    advice="20 点观看。",
                )
            ],
        )
    )
    executor = AsyncMock(return_value=PlanToolOutput(data={"match": "20:00"}))
    graph = create_plan_graph(
        analyzer,
        composer,
        [registered_tool(name="cs_matches", executor=executor)],
    )

    result = invoke(graph, [HobbyInput(id=1, name="看 CS 比赛")])

    executor.assert_awaited_once()
    assert result["result"].items[0].used_tools == ["cs_matches"]


def test_missing_and_failed_tools_are_degraded_without_fabrication():
    hobbies = [HobbyInput(id=1, name="看电影"), HobbyInput(id=2, name="骑车")]
    analyzer = ResultRunnable(
        DependencyAnalysisBatch(
            items=[
                DependencyAnalysis(
                    hobby_id=1,
                    factors=["电影排片"],
                    unsupported_factors=["电影排片"],
                ),
                DependencyAnalysis(
                    hobby_id=2,
                    factors=["天气"],
                    tool_requests=[ToolRequest(tool_name="weather", query="骑车天气")],
                ),
            ]
        )
    )
    composer = ResultRunnable(
        PlanDraft(
            summary="按情况安排。",
            context_summary="虚构的外部信息",
            items=[
                PlanItem(
                    hobby_id=hobby.id,
                    hobby_name=hobby.name,
                    suitability="suitable",
                    advice="直接去。",
                )
                for hobby in hobbies
            ],
        )
    )
    executor = AsyncMock(side_effect=RuntimeError("tool down"))
    graph = create_plan_graph(analyzer, composer, [registered_tool(executor=executor)])

    result = invoke(graph, hobbies)["result"]

    assert [item.suitability for item in result.items] == [
        "tool_unavailable",
        "tool_unavailable",
    ]
    assert "电影排片" in result.items[0].advice
    assert result.context_summary == ""


def test_unknown_tool_name_retries_then_fails():
    invalid = DependencyAnalysisBatch(
        items=[
            DependencyAnalysis(
                hobby_id=1,
                factors=["未知因素"],
                tool_requests=[ToolRequest(tool_name="invented_tool")],
            )
        ]
    )
    analyzer = ResultRunnable(invalid, invalid)
    graph = create_plan_graph(analyzer, ResultRunnable(), [])

    with pytest.raises(PlanGenerationError):
        invoke(graph, [HobbyInput(id=1, name="读书")])
    assert analyzer.calls == 2


def test_legacy_weather_plan_can_still_be_loaded():
    draft = PlanDraft.model_validate(
        {
            "summary": "旧计划",
            "weather_summary": "晴天",
            "items": [
                {
                    "hobby_id": 1,
                    "hobby_name": "骑车",
                    "factor_type": "weather",
                    "factor": "天气",
                    "suitability": "suitable",
                    "advice": "可以骑车",
                    "recommended_time_ranges": [
                        {"start": "09:00", "end": "11:00"}
                    ],
                }
            ],
        }
    )

    assert draft.context_summary == "晴天"
    assert draft.items[0].used_tools == ["weather"]
    assert draft.items[0].recommended_periods == ["09:00–11:00"]
