import asyncio
import json

import httpx
import pytest

from backend.app.agents import weather
from backend.app.agents.plan_tools import PlanToolContext


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class FakeAsyncClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, params):
        self.requests.append((url, params))
        if self.error:
            raise self.error
        return FakeResponse(self.payload)


def install_fake_client(monkeypatch, payload=None, error=None):
    client = FakeAsyncClient(payload, error)
    monkeypatch.setattr(weather, "_require_amap_key", lambda: "test-secret-key")
    monkeypatch.setattr(weather.httpx, "AsyncClient", lambda **_kwargs: client)
    return client


def test_geocode_mianyang_uses_city_constraint_and_returns_sichuan(monkeypatch):
    client = install_fake_client(
        monkeypatch,
        {
            "status": "1",
            "geocodes": [
                {
                    "province": "四川省",
                    "city": "绵阳市",
                    "district": [],
                    "adcode": "510700",
                    "location": "104.679127,31.467673",
                }
            ],
        },
    )

    location = asyncio.run(weather.geocode_china_city("绵阳"))

    assert location.city_name == "绵阳市"
    assert location.admin1 == "四川省"
    assert location.adcode == "510700"
    assert client.requests[0][1]["address"] == "绵阳"
    assert client.requests[0][1]["city"] == "绵阳"
    assert "test-secret-key" not in location.model_dump_json()


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        ({"status": "1", "geocodes": []}, weather.LocationNotFoundError),
        (
            {
                "status": "1",
                "geocodes": [{"adcode": [], "location": "104.68,31.47"}],
            },
            weather.LocationNotFoundError,
        ),
        ({"status": "0", "info": "INVALID_USER_KEY"}, weather.WeatherProviderError),
    ],
)
def test_geocode_maps_empty_invalid_and_provider_errors(monkeypatch, payload, error_type):
    install_fake_client(monkeypatch, payload)

    with pytest.raises(error_type):
        asyncio.run(weather.geocode_china_city("绵阳"))


def test_geocode_maps_timeout_to_provider_error(monkeypatch):
    install_fake_client(monkeypatch, error=httpx.ReadTimeout("timeout"))

    with pytest.raises(weather.WeatherProviderError, match="暂时无法查询城市"):
        asyncio.run(weather.geocode_china_city("绵阳"))


def test_weather_selects_today_and_exposes_only_day_night(monkeypatch):
    client = install_fake_client(
        monkeypatch,
        {
            "status": "1",
            "forecasts": [
                {
                    "city": "绵阳市",
                    "province": "四川省",
                    "reporttime": "2026-09-14 08:00:00",
                    "casts": [
                        {"date": "2026-09-13", "dayweather": "多云"},
                        {
                            "date": "2026-09-14",
                            "dayweather": "晴",
                            "daytemp": "28",
                            "daywind": "东",
                            "daypower": "≤3",
                            "nightweather": "多云",
                            "nighttemp": "19",
                            "nightwind": "北",
                            "nightpower": "≤3",
                        },
                    ],
                }
            ],
        },
    )
    context = PlanToolContext(plan_date="2026-09-14", adcode="510700")

    output = asyncio.run(weather.execute_weather_tool(context, "骑车"))

    assert output.allowed_periods == ["白天", "夜间"]
    assert [item["period"] for item in output.data["periods"]] == ["白天", "夜间"]
    assert output.data["periods"][0]["weather"] == "晴"
    assert client.requests[0][1]["city"] == "510700"
    assert client.requests[0][1]["extensions"] == "all"
    assert "test-secret-key" not in json.dumps(output.model_dump(), ensure_ascii=False)


def test_weather_without_adcode_degrades_before_request():
    context = PlanToolContext(plan_date="2026-09-14")

    with pytest.raises(weather.WeatherProviderError, match="重新设置城市"):
        asyncio.run(weather.execute_weather_tool(context, "骑车"))
