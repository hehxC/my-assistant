from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..agents.plan_agent import (
    HobbyInput,
    PlanDraft,
    PlanGenerationError,
    PlanItem,
    PlanLocationInput,
)
from ..agents.weather import LocationNotFoundError, WeatherProviderError, geocode_china_city
from ..database import get_db
from ..models import DailyPlan, Hobby, PlanPreference
from .auth import CurrentUser


router = APIRouter(prefix="/api/plans", tags=["plans"])
CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today_in_china() -> date:
    return datetime.now(CHINA_TIMEZONE).date()


class LocationPayload(BaseModel):
    city: str = Field(min_length=1, max_length=100)


class LocationResponse(BaseModel):
    city_query: str
    city_name: str
    admin1: str
    adcode: str | None = None
    latitude: float
    longitude: float
    timezone: str


class DailyPlanResponse(BaseModel):
    date: date
    generated_at: datetime
    location: LocationResponse | None
    summary: str
    context_summary: str
    items: list[PlanItem]


class TodayPlanResponse(BaseModel):
    location: LocationResponse | None
    plan: DailyPlanResponse | None


def location_response(preference: PlanPreference) -> LocationResponse:
    return LocationResponse(
        city_query=preference.city_query,
        city_name=preference.city_name,
        admin1=preference.admin1,
        adcode=preference.adcode,
        latitude=preference.latitude,
        longitude=preference.longitude,
        timezone=preference.timezone,
    )


def daily_plan_response(plan: DailyPlan) -> DailyPlanResponse:
    content = plan.content
    draft = PlanDraft.model_validate(content)
    return DailyPlanResponse(
        date=plan.plan_date,
        generated_at=plan.generated_at,
        location=(
            LocationResponse.model_validate(content["location"])
            if content.get("location")
            else None
        ),
        summary=draft.summary,
        context_summary=draft.context_summary,
        items=[item.model_dump() for item in draft.items],
    )


@router.get("/today", response_model=TodayPlanResponse)
def get_today_plan(current_user: CurrentUser, db: Session = Depends(get_db)):
    preference = db.get(PlanPreference, current_user.id)
    plan = db.scalar(
        select(DailyPlan).where(
            DailyPlan.user_id == current_user.id,
            DailyPlan.plan_date == today_in_china(),
        )
    )
    return TodayPlanResponse(
        # 旧地点没有高德行政区编码，不能继续用于天气查询，要求用户重新确认一次城市。
        location=location_response(preference) if preference and preference.adcode else None,
        plan=daily_plan_response(plan) if plan else None,
    )


@router.put("/location", response_model=LocationResponse)
async def update_plan_location(
    payload: LocationPayload,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    city = payload.city.strip()
    if not city:
        raise HTTPException(status_code=422, detail="请输入国内城市")
    try:
        resolved = await geocode_china_city(city)
    except LocationNotFoundError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except WeatherProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    preference = db.get(PlanPreference, current_user.id)
    if preference is None:
        preference = PlanPreference(user_id=current_user.id)
        db.add(preference)

    preference.city_query = resolved.city_query
    preference.city_name = resolved.city_name
    preference.admin1 = resolved.admin1
    preference.adcode = resolved.adcode
    preference.latitude = resolved.latitude
    preference.longitude = resolved.longitude
    preference.timezone = resolved.timezone
    preference.updated_at = utc_now()
    try:
        db.commit()
        db.refresh(preference)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="暂时无法保存计划地点") from exc
    return location_response(preference)


@router.post("/today/generate", response_model=DailyPlanResponse)
async def generate_today_plan(
    request: Request,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    preference = db.get(PlanPreference, current_user.id)
    hobbies = db.scalars(
        select(Hobby).where(Hobby.user_id == current_user.id).order_by(Hobby.id)
    ).all()
    if not hobbies:
        raise HTTPException(status_code=409, detail="请先添加至少一个爱好")

    plan_date = today_in_china()
    location = location_response(preference) if preference and preference.adcode else None
    state = {
        "hobbies": [HobbyInput(id=item.id, name=item.name, note=item.note) for item in hobbies],
        "location": PlanLocationInput(**location.model_dump()) if location else None,
        "plan_date": plan_date.isoformat(),
    }
    try:
        graph_result = await request.app.state.plan_graph.ainvoke(state)
        draft = PlanDraft.model_validate(graph_result["result"])
    except PlanGenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="暂时无法生成今日计划") from exc

    content = {
        **draft.model_dump(mode="json"),
        "location": location.model_dump(mode="json") if location else None,
    }
    plan = db.scalar(
        select(DailyPlan).where(
            DailyPlan.user_id == current_user.id,
            DailyPlan.plan_date == plan_date,
        )
    )
    generated_at = utc_now()
    if plan is None:
        plan = DailyPlan(
            user_id=current_user.id,
            plan_date=plan_date,
            content=content,
            generated_at=generated_at,
        )
        db.add(plan)
    else:
        # 同一账号每天只保留最新版，生成失败时不会执行到这里覆盖旧计划。
        plan.content = content
        plan.generated_at = generated_at

    try:
        db.commit()
        db.refresh(plan)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="暂时无法保存今日计划") from exc
    return daily_plan_response(plan)
