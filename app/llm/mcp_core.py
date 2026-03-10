import logging
import asyncio
import time
import json
from typing import Dict, Optional, List
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.types import TextContent, ImageContent, EmbeddedResource
from app.core.config import settings
from app.llm.rag import rag_service

logger = logging.getLogger(__name__)

class MCPManager:
    """
    管理 MCP 服务器连接和工具。
    """
    def __init__(self):
        self.sessions: Dict[str, ClientSession] = {}
        self.exit_stack: Optional[AsyncExitStack] = None
        self.tool_to_server: Dict[str, str] = {}
        self.all_tools: List[Dict] = []

    async def connect_all(self):
        """初始化所有 MCP 服务器连接"""
        if self.exit_stack:
            await self.close_all()
            
        self.exit_stack = AsyncExitStack()
        self.sessions = {}
        self.all_tools = []
        
        mcp_configs = {
            "k8s": settings.MCP_K8S_COMMAND,
            "filesystem": settings.MCP_FS_COMMAND
        }
        
        for name, cmd_list in mcp_configs.items():
            if not cmd_list: continue
            try:
                server_params = StdioServerParameters(command=cmd_list[0], args=cmd_list[1:], env=None)
                async with asyncio.timeout(30):
                    read_stream, write_stream = await self.exit_stack.enter_async_context(stdio_client(server_params))
                    session = await self.exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
                    await session.initialize()
                    self.sessions[name] = session
                    
                    m_tools = await session.list_tools()
                    for t in m_tools.tools:
                        tool_def = {
                            "type": "function",
                            "function": {
                                "name": t.name,
                                "description": f"[{name}] {t.description}",
                                "parameters": t.inputSchema
                            },
                            "mcp_server": name
                        }
                        self.all_tools.append(tool_def)
                        self.tool_to_server[t.name] = name
                    logger.info(f"已加载 {len(m_tools.tools)} 个工具 (来自 {name})")
            except Exception as e:
                logger.error(f"连接 MCP 服务器 {name} 失败: {e}", exc_info=True)
                raise

        if settings.RAG_ENABLED:
            logger.info("启用 RAG 知识库搜索工具")
            self.all_tools.append({
                "type": "function",
                "function": {
                    "name": "search_rag_knowledge_base",
                    "description": "搜索知识库以获取相关信息、架构、排错指南等。",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string", "description": "搜索查询语句。"}},
                        "required": ["query"]
                    }
                }
            })
        logger.info(f"MCP Manager 初始化完成，共 {len(self.all_tools)} 个工具可用")

    async def close_all(self):
        """关闭所有会话"""
        if self.exit_stack:
            logger.info("正在关闭所有 MCP 会话...")
            try:
                async with asyncio.timeout(10):
                    await self.exit_stack.aclose()
                logger.info("MCP 会话已安全关闭")
            except Exception as e:
                logger.debug(f"MCP 关闭期间的非关键错误: {e}")
            finally:
                self.exit_stack = None
        self.sessions = {}
        self.tool_to_server = {}
        self.all_tools = []

    async def call_tool(self, tool_name: str, tool_args: Dict) -> str:
        """执行具体的工具调用"""
        # 记录工具调用的完整参数
        logger.info(f"调用工具: {tool_name}, 参数: {json.dumps(tool_args, ensure_ascii=False)}")
        start_time = time.time()
        
        try:
            if tool_name == "search_rag_knowledge_base":
                final_res = await rag_service.aquery(tool_args.get("query"))
            else:
                server_name = self.tool_to_server.get(tool_name)
                if not server_name or server_name not in self.sessions:
                    raise ConnectionError(f"工具 {tool_name} 没有活跃的 MCP 会话")
                
                session = self.sessions[server_name]
                result = await session.call_tool(tool_name, arguments=tool_args)
                
                content_parts = []
                if hasattr(result, 'content') and result.content:
                    for content in result.content:
                        if isinstance(content, TextContent):
                            content_parts.append(content.text)
                        elif isinstance(content, ImageContent):
                            content_parts.append(f"[图像: {content.mimeType}]")
                        elif isinstance(content, EmbeddedResource):
                            content_parts.append(f"[资源: {content.resource.uri}]")
                        else:
                            content_parts.append(str(content))
                    final_res = "\n".join(content_parts)
                else:
                    final_res = str(result)
            
            logger.info(f"工具 {tool_name} 执行成功，耗时 {time.time() - start_time:.2f}s")
            logger.info(f"工具 {tool_name} 输出结果: {final_res}")
            return final_res
            
        except Exception as e:
            logger.error(f"工具 {tool_name} 执行失败: {e}", exc_info=True)
            raise
