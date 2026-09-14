import httpx
from langchain_core.tools import tool
from pydantic import BaseModel

from ..config import get_settings
from .plan_tools import PlanToolContext, PlanToolOutput, RegisteredPlanTool


GEOCODING_URL = "https://restapi.amap.com/v3/geocode/geo"
FORECAST_URL = "https://restapi.amap.com/v3/weather/weatherInfo"
CHINA_TIMEZONE = "Asia/Shanghai"


class WeatherProviderError(RuntimeError):
    pass


class LocationNotFoundError(WeatherProviderError):
    pass


class ResolvedLocation(BaseModel):
    city_query: str
    city_name: str
    admin1: str
    adcode: str
    latitude: float
    longitude: float
    timezone: str = CHINA_TIMEZONE


class WeatherPeriod(BaseModel):
    period: str
    weather: str
    temperature: str
    wind_direction: str
    wind_power: str


class WeatherSnapshot(BaseModel):
    city: str
    province: str
    report_time: str
    date: str
    periods: list[WeatherPeriod]


def _require_amap_key() -> str:
    api_key = get_settings().amap_api_key
    if not api_key:
        raise WeatherProviderError("AMAP_API_KEY is not configured")
    return api_key


def _parse_amap_payload(response: httpx.Response, error_message: str) -> dict:
    """校验高德统一响应字段，避免把错误响应当作正常业务数据。"""

    try:
        payload = response.json()
    except ValueError as exc:
        raise WeatherProviderError(error_message) from exc
    if not isinstance(payload, dict) or payload.get("status") != "1":
        raise WeatherProviderError(error_message)
    return payload


def _text_value(value) -> str:
    """兼容高德空字段有时返回空数组的情况。"""

    return value if isinstance(value, str) else ""


async def geocode_china_city(city: str) -> ResolvedLocation:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                GEOCODING_URL,
                params={
                    "key": _require_amap_key(),
                    "address": city,
                    "city": city,
                    "output": "JSON",
                },
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise WeatherProviderError("暂时无法查询城市") from exc

    payload = _parse_amap_payload(response, "高德城市查询失败")
    geocodes = payload.get("geocodes", [])
    if not geocodes:
        raise LocationNotFoundError("没有找到这个国内城市")

    location = geocodes[0]
    adcode = _text_value(location.get("adcode"))
    coordinate = _text_value(location.get("location"))
    if len(adcode) != 6 or not adcode.isdigit() or "," not in coordinate:
        raise LocationNotFoundError("没有找到这个国内城市")

    try:
        longitude, latitude = map(float, coordinate.split(",", maxsplit=1))
    except ValueError as exc:
        raise LocationNotFoundError("没有找到这个国内城市") from exc
    city_name = _text_value(location.get("city"))
    district = _text_value(location.get("district"))
    province = _text_value(location.get("province"))
    return ResolvedLocation(
        city_query=city,
        city_name=city_name or district or province,
        admin1=province,
        adcode=adcode,
        latitude=latitude,
        longitude=longitude,
    )


@tool
async def get_today_weather(adcode: str, plan_date: str) -> dict:
    """使用高德行政区编码查询指定日期的白天和夜间天气。"""

    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(
                FORECAST_URL,
                params={
                    "key": _require_amap_key(),
                    "city": adcode,
                    "extensions": "all",
                    "output": "JSON",
                },
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise WeatherProviderError("暂时无法获取天气预报") from exc

    payload = _parse_amap_payload(response, "高德天气查询失败")
    forecasts = payload.get("forecasts", [])
    if not forecasts:
        raise WeatherProviderError("高德天气没有返回预报数据")

    forecast = forecasts[0]
    cast = next(
        (item for item in forecast.get("casts", []) if item.get("date") == plan_date),
        None,
    )
    if cast is None:
        raise WeatherProviderError("高德天气没有返回今天的预报")

    snapshot = WeatherSnapshot(
        city=_text_value(forecast.get("city")),
        province=_text_value(forecast.get("province")),
        report_time=_text_value(forecast.get("reporttime")),
        date=cast["date"],
        periods=[
            WeatherPeriod(
                period="白天",
                weather=_text_value(cast.get("dayweather")),
                temperature=_text_value(cast.get("daytemp")),
                wind_direction=_text_value(cast.get("daywind")),
                wind_power=_text_value(cast.get("daypower")),
            ),
            WeatherPeriod(
                period="夜间",
                weather=_text_value(cast.get("nightweather")),
                temperature=_text_value(cast.get("nighttemp")),
                wind_direction=_text_value(cast.get("nightwind")),
                wind_power=_text_value(cast.get("nightpower")),
            ),
        ],
    )
    return snapshot.model_dump()


async def execute_weather_tool(context: PlanToolContext, _query: str) -> PlanToolOutput:
    """把通用计划上下文转换为高德天气参数和统一工具结果。"""

    if not context.adcode:
        raise WeatherProviderError("天气工具需要重新设置城市")

    payload = await get_today_weather.ainvoke(
        {"adcode": context.adcode, "plan_date": context.plan_date}
    )
    snapshot = WeatherSnapshot.model_validate(payload)
    return PlanToolOutput(
        data=snapshot.model_dump(),
        allowed_periods=[period.period for period in snapshot.periods],
    )


def weather_cache_key(context: PlanToolContext, _query: str) -> str:
    """同一行政区和日期的所有爱好共享一次天气查询。"""

    return f"weather:{context.plan_date}:{context.adcode}"


WEATHER_PLAN_TOOL = RegisteredPlanTool(
    name="weather",
    description="查询指定中国城市当天白天和夜间的天气、温度、风向与风力",
    executor=execute_weather_tool,
    cache_key=weather_cache_key,
)
