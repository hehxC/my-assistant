from functools import lru_cache
import os

from dotenv import load_dotenv
from pydantic import BaseModel


# 启动时读取项目根目录的 .env 文件。
load_dotenv()


class Settings(BaseModel):
    """集中描述后端运行所需的模型和数据库配置。"""

    # DeepSeek API 的认证密钥。
    deepseek_api_key: str

    # 默认使用适合普通对话的 deepseek-chat。
    deepseek_model: str = "deepseek-chat"

    # DeepSeek 的 OpenAI 兼容 API 地址。
    deepseek_base_url: str = "https://api.deepseek.com"

    # 高德 Web 服务 API 的认证密钥，仅在后端调用地理编码和天气接口。
    amap_api_key: str = ""

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""
    mysql_database: str = "my-assistant"

    redis_url: str = "redis://127.0.0.1:6379"
    session_cookie_secure: bool = False


@lru_cache
def get_settings() -> Settings:
    """读取并缓存配置，避免每个请求都重复访问环境变量。"""

    return Settings(
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        amap_api_key=os.getenv("AMAP_API_KEY", ""),
        mysql_host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        mysql_port=int(os.getenv("MYSQL_PORT", "3306")),
        mysql_user=os.getenv("MYSQL_USER", "root"),
        mysql_password=os.getenv("MYSQL_PASSWORD", ""),
        mysql_database=os.getenv("MYSQL_DATABASE", "my-assistant"),
        redis_url=os.getenv(
            "REDIS_URL",
            "redis://127.0.0.1:6379",
        ),
        session_cookie_secure=os.getenv("SESSION_COOKIE_SECURE", "false").lower()
        in {"1", "true", "yes"},
    )
