import logging
import os
import asyncio
import time
import json
from typing import List, Dict, Any, TypedDict, Annotated, Union
import operator

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage, BaseMessage, trim_messages
from langgraph.graph import StateGraph, END, START
from langgraph.checkpoint.memory import MemorySaver

from app.core.config import settings
from app.llm.mcp_core import MCPManager
from app.llm.utils import count_tokens

logger = logging.getLogger(__name__)

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], operator.add]

class K8sAgent:
    """
    LangGraph Agent，负责 K8s SRE 对话逻辑。
    """
    def __init__(self, mcp_manager: MCPManager):
        self.mcp_manager = mcp_manager
        self.checkpointer = MemorySaver()
        self.app = None
        self.llm_with_tools = None
        self.system_content = ""

    def _load_system_prompt(self):
        guide_path = settings.SYSTEM_PROMPT_PATH
        if guide_path and os.path.exists(guide_path):
            try:
                with open(guide_path, 'r', encoding='utf-8') as f:
                    self.system_content = f.read()
            except Exception as e:
                logger.warning(f"加载系统提示词失败: {e}")
        if not self.system_content:
            self.system_content = "你是一个专业的 K8s 助手。"

    def compile(self):
        """编译 LangGraph 应用"""
        self._load_system_prompt()
        
        llm = ChatOpenAI(
            model=settings.OPENAI_MODEL,
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
            temperature=0,
            streaming=False,
            timeout=300
        )
        
        langchain_tools = [
            {"type": t["type"], "function": t["function"]} for t in self.mcp_manager.all_tools
        ]
        self.llm_with_tools = llm.bind_tools(langchain_tools)

        workflow = StateGraph(AgentState)
        workflow.add_node("agent", self._call_model)
        workflow.add_node("tools", self._call_tools)
        workflow.add_edge(START, "agent")
        workflow.add_conditional_edges("agent", self._should_continue, {"tools": "tools", END: END})
        workflow.add_edge("tools", "agent")
        
        self.app = workflow.compile(checkpointer=self.checkpointer)

    async def _call_model(self, state: AgentState):
        messages = state["messages"]
        logger.info(f"LLM 输入消息数: {len(messages)}")
        
        # 压缩旧的长 ToolMessage
        processed_messages = []
        safe_zone = 8 
        for i, msg in enumerate(messages):
            if (len(messages) - i) > safe_zone and isinstance(msg, ToolMessage):
                if isinstance(msg.content, str) and len(msg.content) > 8000:
                    new_content = f"{msg.content[:1000]}\n\n... [精简] ...\n\n{msg.content[-1000:]}"
                    msg = ToolMessage(content=new_content, tool_call_id=msg.tool_call_id, name=msg.name)
            processed_messages.append(msg)
        
        max_tokens_limit = 100000 if settings.MAX_HISTORY_ROUNDS == -1 else 20000
        try:
            messages = trim_messages(
                processed_messages,
                max_tokens=max_tokens_limit,
                strategy="last",
                token_counter=count_tokens,
                include_system=True,
                allow_partial=False
            )
        except Exception:
            if settings.MAX_HISTORY_ROUNDS != -1 and len(messages) > settings.MAX_HISTORY_ROUNDS * 2:
                messages = messages[-(settings.MAX_HISTORY_ROUNDS * 2):]

        try:
            async with asyncio.timeout(600):
                response = await self.llm_with_tools.ainvoke(messages)
                logger.debug(f"LLM 响应: {str(response)[:200]}...")
                return {"messages": [response]}
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}", exc_info=True)
            raise

    async def _call_tools(self, state: AgentState):
        last_message = state["messages"][-1]
        if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
            return {"messages": []}

        results = []
        for tool_call in last_message.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            tool_call_id = tool_call["id"]
            
            try:
                async with asyncio.timeout(60):
                    content_str = await self.mcp_manager.call_tool(tool_name, tool_args)
                    
                    max_output_len = 18000
                    if len(content_str) > max_output_len:
                        logger.warning(f"工具 {tool_name} 输出过长 ({len(content_str)} chars)，进行截断")
                        half_len = max_output_len // 2
                        content_str = f"{content_str[:half_len]}\n\n... [精简] ...\n\n{content_str[-half_len:]}"

                    results.append(ToolMessage(tool_call_id=tool_call_id, content=content_str, name=tool_name))
            except Exception as e:
                logger.error(f"工具 {tool_name} 执行异常: {e}", exc_info=True)
                results.append(ToolMessage(tool_call_id=tool_call_id, content=f"错误: {str(e)}", name=tool_name, status="error"))
        return {"messages": results}

    def _should_continue(self, state: AgentState):
        last_message = state["messages"][-1]
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            return "tools"
        return END
