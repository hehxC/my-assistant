from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import StudySession
from .auth import CurrentUser


router = APIRouter(prefix="/api/study", tags=["study"])
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")


class StudyStartResponse(BaseModel):
    session_id: int
    started_at: datetime


class StudyStopRequest(BaseModel):
    session_id: int = Field(gt=0)


class StudyStopResponse(BaseModel):
    session_id: int
    started_at: datetime
    stopped_at: datetime
    duration_seconds: int


class StudyPeriod(BaseModel):
    start_time: str
    end_time: str
    duration_seconds: int


class DailyStudyStat(BaseModel):
    date: date
    duration_seconds: int
    periods: list[StudyPeriod]


class StudyStatsResponse(BaseModel):
    start_date: date
    end_date: date
    total_seconds: int
    days: list[DailyStudyStat]


def utc_now() -> datetime:
    # MySQL DATETIME 不保存时区，因此统一写入不带时区信息的 UTC 时间。
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.post("/start", response_model=StudyStartResponse)
def start_study(current_user: CurrentUser, db: Session = Depends(get_db)) -> StudyStartResponse:
    study_session = StudySession(user_id=current_user.id, started_at=utc_now())

    try:
        # 开始时先创建记录，前端刷新后仍可以通过 session_id 停止它。
        db.add(study_session)
        db.commit()
        db.refresh(study_session)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="暂时无法记录学习开始时间") from exc

    return StudyStartResponse(
        session_id=study_session.id,
        started_at=study_session.started_at,
    )


@router.post("/stop", response_model=StudyStopResponse)
def stop_study(
    request: StudyStopRequest,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
) -> StudyStopResponse:
    study_session = db.scalar(
        select(StudySession).where(
            StudySession.id == request.session_id,
            StudySession.user_id == current_user.id,
        )
    )

    if study_session is None:
        raise HTTPException(status_code=404, detail="没有找到这次学习记录")

    if study_session.stopped_at is not None:
        raise HTTPException(status_code=409, detail="这次学习已经停止")

    stopped_at = utc_now()
    duration_seconds = max(
        0,
        int((stopped_at - study_session.started_at).total_seconds()),
    )

    # 停止时一次性补齐结束时间和学习秒数，保证两个字段同步提交。
    study_session.stopped_at = stopped_at
    study_session.duration_seconds = duration_seconds

    try:
        db.commit()
        db.refresh(study_session)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="暂时无法保存学习时间") from exc

    return StudyStopResponse(
        session_id=study_session.id,
        started_at=study_session.started_at,
        stopped_at=study_session.stopped_at,
        duration_seconds=study_session.duration_seconds,
    )


@router.get("/stats", response_model=StudyStatsResponse)
def get_study_stats(
    current_user: CurrentUser,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
) -> StudyStatsResponse:
    today = datetime.now(LOCAL_TIMEZONE).date()
    query_end = end_date or today
    query_start = start_date or query_end - timedelta(days=14)

    if query_start > query_end:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")

    if (query_end - query_start).days > 365:
        raise HTTPException(status_code=400, detail="单次最多查询 366 天")

    # 页面按中国时区展示日期，而数据库统一保存 UTC，需要先转换查询边界。
    range_start = datetime.combine(query_start, time.min, LOCAL_TIMEZONE)
    range_end = datetime.combine(query_end + timedelta(days=1), time.min, LOCAL_TIMEZONE)
    range_start_utc = range_start.astimezone(timezone.utc).replace(tzinfo=None)
    range_end_utc = range_end.astimezone(timezone.utc).replace(tzinfo=None)

    try:
        # 查询与目标日期范围有交集的已完成学习记录。
        sessions = db.scalars(
            select(StudySession).where(
                StudySession.user_id == current_user.id,
                StudySession.stopped_at.is_not(None),
                StudySession.started_at < range_end_utc,
                StudySession.stopped_at > range_start_utc,
            )
        ).all()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="暂时无法读取学习统计") from exc

    daily_periods: dict[date, list[StudyPeriod]] = {}
    current_date = query_start
    while current_date <= query_end:
        daily_periods[current_date] = []
        current_date += timedelta(days=1)

    # 跨越午夜的学习记录按每天实际覆盖的秒数拆分，避免全部算到开始日。
    for study_session in sessions:
        session_start = study_session.started_at.replace(tzinfo=timezone.utc)
        session_end = study_session.stopped_at.replace(tzinfo=timezone.utc)

        for day, periods in daily_periods.items():
            day_start = datetime.combine(day, time.min, LOCAL_TIMEZONE).astimezone(timezone.utc)
            day_end = day_start + timedelta(days=1)
            overlap_start = max(session_start, day_start)
            overlap_end = min(session_end, day_end)

            if overlap_end > overlap_start:
                duration_seconds = int((overlap_end - overlap_start).total_seconds())
                local_start = overlap_start.astimezone(LOCAL_TIMEZONE)
                local_end = overlap_end.astimezone(LOCAL_TIMEZONE)
                periods.append(
                    StudyPeriod(
                        start_time=local_start.strftime("%H:%M"),
                        end_time="24:00" if overlap_end == day_end else local_end.strftime("%H:%M"),
                        duration_seconds=duration_seconds,
                    )
                )

    days = [
        DailyStudyStat(
            date=day,
            duration_seconds=sum(period.duration_seconds for period in periods),
            periods=sorted(periods, key=lambda period: period.start_time),
        )
        for day, periods in daily_periods.items()
    ]

    return StudyStatsResponse(
        start_date=query_start,
        end_date=query_end,
        total_seconds=sum(item.duration_seconds for item in days),
        days=days,
    )
