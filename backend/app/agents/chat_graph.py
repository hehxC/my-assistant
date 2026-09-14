from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from ..config import get_settings
from .prompts.talk_agent_prompt import TALK_AGENT_PROMPT


class ChatState(TypedDict):
    """定义 LangGraph 在一次会话中需要保存的状态。"""

    # add_messages 是消息列表的 reducer，新消息会追加到历史消息之后。
    messages: Annotated[list[AnyMessage], add_messages]


def create_chat_graph(checkpointer: BaseCheckpointSaver):
    """使用指定的状态存储器创建聊天 Graph。"""

    settings = get_settings()
    if not settings.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")

    model = ChatOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        temperature=0.8,
        max_tokens=800,
    )

    # 聊天节点读取当前 thread 的历史消息，然后生成一条回复。
    async def talk(state: ChatState) -> ChatState:
        model_messages = [
            SystemMessage(content=TALK_AGENT_PROMPT),
            *state["messages"],
        ]

        reply = await model.ainvoke(model_messages)

        # 只返回新回复，add_messages 会把它追加进历史状态。
        return {"messages": [reply]}

    builder = StateGraph(ChatState)
    builder.add_node("talk", talk)
    builder.add_edge(START, "talk")
    builder.add_edge("talk", END)

    # 编译时传入 checkpointer，Graph 才会启用短期记忆。
    return builder.compile(checkpointer=checkpointer)
