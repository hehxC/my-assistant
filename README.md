# 个人工作助手

一个使用 Vue、FastAPI、LangGraph 和 DeepSeek 实现的个人工作助手。账号之间的聊天与学习记录互相隔离，LangGraph 通过 `AsyncRedisSaver` 为每段对话维护短期记忆。

“当人了吗”页面的计时器会在开始时创建学习会话，在停止时把结束时间和学习秒数保存到 MySQL。

侧边栏的“我的爱好”页面支持按账号添加、编辑和删除爱好，并为后续生成今日计划提供结构化兴趣数据。

“今日计划”会读取当前账号的爱好，通过 LangGraph 判断外部影响因素并从运行时工具注册表选择能力；天气只是当前默认工具，后续增加赛事或电影工具不需要修改 Graph 主流程。天气与城市解析使用高德 Web 服务，建议时段依据标准预报展示为“白天”或“夜间”。计划按账号和日期保存到 MySQL。

桌面端侧边栏的“当人了吗”页面按天展示学习时长。默认查询最近 15 天，也可以指定开始日期和结束日期；点击柱状图日期可以查看当天的具体学习时间段，跨越午夜的会话会按每天实际覆盖的时间拆分。

## 项目结构

```text
backend/
├─ app/
│  ├─ agents/            LangGraph、模型调用与 Agent 提示词
│  ├─ routers/           认证、聊天、学习和爱好接口
│  ├─ config.py          环境配置
│  ├─ database.py        数据库连接与结构初始化
│  ├─ models.py          SQLAlchemy 数据模型
│  └─ main.py            FastAPI 应用装配入口
└─ tests/                后端自动化测试
frontend/                Vue 3 + Vite 前端
.env                     本地配置，不会被 Git 跟踪
```

## 启动后端

需要 Python 3.10 或更高版本。

先启动带有 RedisJSON 和 RediSearch 的 Redis Stack：

```powershell
docker run -d --name my-assistant-redis -p 6379:6379 redis/redis-stack-server:latest
```

如果容器已经创建但处于停止状态，运行 `docker start my-assistant-redis`。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn backend.app.main:app --reload
```

后端运行在 `http://127.0.0.1:8000`，接口文档位于 `http://127.0.0.1:8000/docs`。

## 启动前端

需要 Node.js 18 或更高版本。在另一个终端中运行：

```powershell
cd frontend
npm install
npm run dev
```

然后访问 `http://localhost:5173`。开发服务器会把 `/api` 请求代理到后端。

## 测试与构建

```powershell
pytest
cd frontend
npm run build
```

## 配置

后端从项目根目录的 `.env` 读取：

- `DEEPSEEK_API_KEY`：DeepSeek API Key
- `DEEPSEEK_MODEL`：模型名称，默认 `deepseek-chat`
- `AMAP_API_KEY`：高德开放平台的 Web 服务 API Key，用于国内城市解析和天气预报
- `MYSQL_HOST`：MySQL 地址，默认 `127.0.0.1`
- `MYSQL_PORT`：MySQL 端口，默认 `3306`
- `MYSQL_USER`：MySQL 用户名，默认 `root`
- `MYSQL_PASSWORD`：MySQL 密码
- `MYSQL_DATABASE`：数据库名，默认 `my-assistant`
- `REDIS_URL`：Redis 地址，默认 `redis://127.0.0.1:6379`
- `SESSION_COOKIE_SECURE`：本地 HTTP 开发使用 `false`，部署到 HTTPS 时设置为 `true`

不要把 `.env` 提交到版本控制。部署时请在服务端环境变量中配置密钥。

## 短期记忆说明

前端登录后向后端申请一个归属当前用户的 `thread_id`，后续请求只发送这个 ID 和用户最新的一条消息。后端校验线程所有权后，LangGraph 才会恢复消息。

点击“清空对话”会向后端申请新的线程，因此新旧对话不会混在一起。`AsyncRedisSaver` 会把会话状态保存到 Redis，重启 FastAPI 后仍可恢复。
