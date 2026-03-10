import logging
import asyncio
import time
import json
from typing import List, Dict, Any, Union

from app.llm.mcp_core import MCPManager
from app.llm.agent import K8sAgent

logger = logging.getLogger(__name__)

class MCPClient:
    """
    MCP 客户端 facade，协调 MCPManager 和 K8sAgent。
    """
    def __init__(self):
        self.mcp_manager = MCPManager()
        self.agent = K8sAgent(self.mcp_manager)
        self._connected = False

    async def connect(self):
        """初始化连接"""
        if self._connected:
            return
            
        await self.mcp_manager.connect_all()
        self.agent.compile()
        self._connected = True
        logger.info("MCP Client 已连接并初始化 Agent")

    async def close(self):
        """关闭连接"""
        await self.mcp_manager.close_all()
        self._connected = False

    async def clear_context(self, user_id: str):
        """清理上下文"""
        try:
            await self.agent.checkpointer.adelete_thread(user_id)
            logger.info(f"[{user_id}] 对话上下文已清理")
        except Exception as e:
            logger.error(f"[{user_id}] 清理对话上下文失败: {e}")

    async def process_message(self, user_input: Union[str, List[Dict[str, Any]]], user_id: str = "default_user"):
        """主处理接口"""
        start_time = time.time()
        # 记录用户输入内容
        if isinstance(user_input, str):
            input_preview = user_input
        else:
            input_preview = json.dumps(user_input, ensure_ascii=False)
        logger.info(f"[{user_id}] 收到用户消息: {input_preview}")

        try:
            if not self._connected:
                logger.info(f"[{user_id}] MCP 客户端未连接，正在初始化...")
                await self.connect()

            config = {"configurable": {"thread_id": user_id}}
            
            # 检查是否有历史状态，如果没有则添加系统提示词
            current_state = None
            try:
                current_state = await self.agent.app.aget_state(config)
            except Exception as e:
                logger.debug(f"[{user_id}] 获取状态失败 (可能是新会话): {e}")

            from langchain_core.messages import SystemMessage, HumanMessage
            
            input_messages = []
            if not current_state or not current_state.values:
                logger.info(f"[{user_id}] 新会话，加载系统提示词")
                self.agent._load_system_prompt()
                input_messages.append(SystemMessage(content=self.agent.system_content))
            
            input_messages.append(HumanMessage(content=user_input))

            # --- 状态追踪 ---
            stages = [{"name": "正在思考", "start": time.time(), "done": False, "content": ""}]
            
            def render_status():
                lines = []
                for s in stages:
                    if s.get("done"):
                        elapsed = s["end"] - s["start"]
                        lines.append(f"✅ {s['name']} ({elapsed:.1f}s){s.get('content', '')}")
                    else:
                        # 进行中状态不显示时间，避免需要实时刷新
                        lines.append(f"⏳ {s['name']}...{s.get('content', '')}")
                return "\n".join(lines)

            # 初始状态
            yield render_status()
            
            final_content = ""
            logger.info(f"[{user_id}] 进入 LangGraph 处理流")
            async for event in self.agent.app.astream({"messages": input_messages}, config=config, stream_mode="updates"):
                for node_name, output in event.items():
                    if node_name == "agent":
                        last_msg = output["messages"][-1]
                        
                        # 结束当前思考阶段
                        stages[-1]["done"] = True
                        stages[-1]["end"] = time.time()
                        
                        if last_msg.tool_calls:
                            tool_info_list = []
                            status_info_list = []
                            for tc in last_msg.tool_calls:
                                tool_name = tc["name"]
                                short_name = tool_name.split(']')[-1].strip() if ']' in tool_name else tool_name
                                tool_args = json.dumps(tc["args"], ensure_ascii=False)
                                
                                tool_info_list.append(f"{tool_name}({tool_args})")
                                status_info_list.append(f"{short_name}({tool_args})")
                            
                            logger.info(f"[{user_id}] Agent 决定调用工具: {tool_info_list}")
                            
                            # 开启工具执行阶段
                            stages.append({
                                "name": "正在执行工具", 
                                "start": time.time(), 
                                "done": False, 
                                "content": f": {', '.join(status_info_list)}"
                            })
                        else:
                            final_content = last_msg.content
                            logger.info(f"[{user_id}] Agent 生成最终回复: {final_content}")
                    
                    elif node_name == "tools":
                        logger.info(f"[{user_id}] 工具执行完成，返回结果给 Agent")
                        # 结束工具执行阶段
                        stages[-1]["done"] = True
                        stages[-1]["end"] = time.time()
                        
                        # 开启新一轮思考
                        stages.append({
                            "name": "正在思考", 
                            "start": time.time(), 
                            "done": False, 
                            "content": ""
                        })

                if not final_content:
                    yield render_status()
            
            if not final_content:
                logger.warning(f"[{user_id}] 流程结束但未生成内容")
                final_content = "处理完成。"
            
            total_elapsed = time.time() - start_time
            logger.info(f"[{user_id}] 响应生成完毕，总耗时 {total_elapsed:.2f}s")
            
            # 附加总耗时到回复末尾
            final_content += f"\n\n---\n⏱️ 总耗时: {total_elapsed:.2f}s"
            
            yield final_content
            
        except Exception as e:
            logger.error(f"[{user_id}] 处理消息异常: {e}", exc_info=True)
            yield f"❌ 处理消息时发生错误: {str(e)}"

mcp_client = MCPClient()
