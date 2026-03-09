import json
import logging
import asyncio
import time
import httpx
import tiktoken
import os
from typing import List, Dict, Any, Optional, Annotated, TypedDict
from contextlib import AsyncExitStack
import operator

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.types import TextContent, ImageContent, EmbeddedResource
from app.core.config import settings
from app.llm.rag import rag_service

# LangChain imports
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage, BaseMessage, trim_messages
from langgraph.graph import StateGraph, END, START
from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger(__name__)

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], operator.add]

class MCPClient:
    def __init__(self):
        self.api_key = settings.OPENAI_API_KEY
        self.base_url = settings.OPENAI_BASE_URL
        self.model_name = settings.OPENAI_MODEL
        self.max_history_rounds = settings.MAX_HISTORY_ROUNDS
        
        self.checkpointer = MemorySaver()
        self.sessions: Dict[str, ClientSession] = {}
        self.exit_stack: Optional[AsyncExitStack] = None
        self.app = None
        self.llm_with_tools = None
        self.system_content = ""

    def _load_system_prompt(self):
        """Load system prompt from guide.md file."""
        guide_path = "./doc_path/guide_doc/guide.md"
        if os.path.exists(guide_path):
            try:
                with open(guide_path, 'r', encoding='utf-8') as f:
                    self.system_content = f.read()
                logger.info("System prompt loaded/reloaded from guide.md")
            except Exception as e:
                logger.warning(f"Failed to load guide.md: {e}")
                if not self.system_content:
                    self.system_content = "你是一个专业的 K8s SRE 助手。"
        else:
            if not self.system_content:
                self.system_content = "你是一个专业的 K8s SRE 助手。"

    async def connect(self):
        """Initialize connections to multiple MCP servers, fetch tools, and build the agent graph."""
        # 0. Load system prompt
        self._load_system_prompt()

        # Clean up existing session if any
        if self.exit_stack:
            await self.close()
            
        self.exit_stack = AsyncExitStack()
        self.sessions = {}
        all_tools = []
        
        mcp_configs = {
            "k8s": settings.MCP_K8S_COMMAND,
            "filesystem": settings.MCP_FS_COMMAND
        }
        
        try:
            for name, cmd_list in mcp_configs.items():
                logger.info(f"Connecting to MCP server: {name}")
                command = cmd_list[0]
                args = cmd_list[1:]
                
                server_params = StdioServerParameters(
                    command=command,
                    args=args,
                    env=None
                )
                
                try:
                    # Use a shorter timeout for stdio_client to avoid blocking signal handling
                    async with asyncio.timeout(30):
                        read_stream, write_stream = await self.exit_stack.enter_async_context(
                            stdio_client(server_params)
                        )
                        
                        session = await self.exit_stack.enter_async_context(
                            ClientSession(read_stream, write_stream)
                        )
                        await session.initialize()
                        self.sessions[name] = session
                        logger.info(f"MCP Session {name} connected.")

                        # Fetch Tools from this session
                        m_tools = await session.list_tools()
                        for t in m_tools.tools:
                            all_tools.append({
                                "type": "function",
                                "function": {
                                    "name": t.name,
                                    "description": f"[{name}] {t.description}",
                                    "parameters": t.inputSchema
                                },
                                "mcp_server": name  # Tag the tool with its server name
                            })
                except TimeoutError:
                    logger.error(f"Timeout connecting to MCP server: {name}")
                    raise
                except Exception as e:
                    logger.error(f"Failed to connect to MCP server {name}: {e}")
                    raise
            
            # Add RAG tool if enabled
            if settings.RAG_ENABLED:
                all_tools.append({
                    "type": "function",
                    "function": {
                        "name": "search_knowledge_base",
                        "description": "Search the knowledge base for SRE related information, architecture, troubleshooting guides, etc.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "The search query."
                                }
                            },
                            "required": ["query"]
                        }
                    }
                })
            
            # 3. Initialize LLM
            llm = ChatOpenAI(
                model=self.model_name,
                api_key=self.api_key,
                base_url=self.base_url,
                temperature=0,
                streaming=False,
                timeout=300,  # Increased timeout
                max_retries=3
            )
            # Bind tools to LLM
            # We need to strip our custom 'mcp_server' key before binding
            langchain_tools = [
                {"type": t["type"], "function": t["function"]} for t in all_tools
            ]
            self.llm_with_tools = llm.bind_tools(langchain_tools)
            
            # Store tool to server mapping for execution
            self.tool_to_server = {
                t["function"]["name"]: t.get("mcp_server") for t in all_tools
            }

            # 4. Build Graph
            workflow = StateGraph(AgentState)
            workflow.add_node("agent", self._call_model)
            workflow.add_node("tools", self._call_tools)
            workflow.add_edge(START, "agent")
            workflow.add_conditional_edges(
                "agent",
                self._should_continue,
                {"tools": "tools", END: END}
            )
            workflow.add_edge("tools", "agent")
            
            self.app = workflow.compile(checkpointer=self.checkpointer)
            logger.info("LangGraph Agent compiled successfully.")
            
        except Exception as e:
            logger.error(f"Failed to connect MCP Client: {e}")
            await self.close()
            raise

    async def close(self):
        if self.exit_stack:
            logger.info("Closing MCP sessions...")
            try:
                # 显式逐个关闭 session 可能会更优雅，但 aclose 应该处理这些
                # 针对 MCP SDK 的 asyncio 特性，增加超时保护
                async with asyncio.timeout(10):
                    await self.exit_stack.aclose()
            except asyncio.TimeoutError:
                logger.warning("Timeout closing MCP sessions, forcing exit.")
            except Exception as e:
                # 捕获日志中出现的 RuntimeError 和 ExceptionGroup
                # 避免退出时的异常导致 Application shutdown failed
                logger.debug(f"Non-critical error during MCP shutdown: {e}")
            finally:
                self.exit_stack = None
        self.sessions = {}
        self.app = None
        self.tool_to_server = {}

    def _token_counter(self, messages: List[BaseMessage]) -> int:
        """Custom token counter using tiktoken."""
        try:
            encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            # Fallback if encoding name is invalid, though cl100k_base is standard
            encoding = tiktoken.get_encoding("gpt2") 
            
        num_tokens = 0
        for msg in messages:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            # Approximate calculation: 4 tokens per message overhead + content tokens
            num_tokens += 4 + len(encoding.encode(content))
        return num_tokens

    async def _call_model(self, state: AgentState):
        messages = state["messages"]
        
        # Token Trimming
        try:
            messages = trim_messages(
                messages,
                max_tokens=12000,
                strategy="last",
                token_counter=self._token_counter,
                include_system=True,
                allow_partial=False
            )
        except Exception as e:
            logger.warning(f"Token trimming failed: {e}")
            # Fallback
            if len(messages) > self.max_history_rounds * 2:
                messages = messages[-(self.max_history_rounds * 2):]

        logger.info(f"Calling LLM with {len(messages)} messages")
        try:
            # Add an explicit timeout for the individual LLM call to prevent infinite hanging
            async with asyncio.timeout(600):
                response = await self.llm_with_tools.ainvoke(messages)
                logger.info("LLM response received")
                return {"messages": [response]}
        except asyncio.TimeoutError:
            logger.error("LLM invoke timed out after 600s")
            return {"messages": [AIMessage(content="抱歉，模型响应超时，请稍后重试。")]}
        except Exception as e:
            logger.error(f"LLM invoke failed: {e}")
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
            
            logger.info(f"Calling tool {tool_name}")
            tool_start_time = time.time()
            try:
                # Add a timeout to each individual tool call
                async with asyncio.timeout(60):
                    if tool_name == "search_knowledge_base":
                        content_str = await rag_service.aquery(tool_args.get("query"))
                    else:
                        server_name = self.tool_to_server.get(tool_name)
                        if not server_name or server_name not in self.sessions:
                            raise ConnectionError(f"No active MCP Session for tool {tool_name}")
                        
                        session = self.sessions[server_name]
                        result = await session.call_tool(tool_name, arguments=tool_args)
                        
                        # Format result
                        content_parts = []
                        if hasattr(result, 'content') and result.content:
                            for content in result.content:
                                if isinstance(content, TextContent):
                                    content_parts.append(content.text)
                                elif isinstance(content, ImageContent):
                                    content_parts.append(f"[Image: {content.mimeType}]")
                                elif isinstance(content, EmbeddedResource):
                                    content_parts.append(f"[Resource: {content.resource.uri}]")
                                else:
                                    content_parts.append(str(content))
                            content_str = "\n".join(content_parts)
                        else:
                            content_str = str(result)

                    # Limit tool output size to prevent overwhelming the LLM
                    max_output_len = 15000
                    if len(content_str) > max_output_len:
                        content_str = content_str[:max_output_len] + f"\n\n[Output truncated from {len(content_str)} to {max_output_len} chars]"

                    tool_duration = time.time() - tool_start_time
                    logger.info(f"Tool {tool_name} finished in {tool_duration:.2f}s, result size: {len(content_str)} chars")

                    results.append(ToolMessage(
                        tool_call_id=tool_call_id,
                        content=content_str,
                        name=tool_name
                    ))
            except asyncio.TimeoutError:
                logger.error(f"Tool {tool_name} timed out after 60s")
                results.append(ToolMessage(
                    tool_call_id=tool_call_id,
                    content=f"Error: Tool {tool_name} timed out after 60s",
                    name=tool_name,
                    status="error"
                ))
            except Exception as e:
                logger.error(f"Tool {tool_name} error: {e}")
                results.append(ToolMessage(
                    tool_call_id=tool_call_id,
                    content=f"Error: {str(e)}",
                    name=tool_name,
                    status="error"
                ))
        return {"messages": results}

    def _should_continue(self, state: AgentState):
        last_message = state["messages"][-1]
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            return "tools"
        return END

    async def process_message(self, user_input: str, user_id: str = "default_user"):
        start_time = time.time()
        logger.info(f"[{user_id}] Processing message")

        try:
            # Auto-reconnect logic
            if not self.app:
                try:
                    await self.connect()
                except Exception as e:
                    logger.error(f"Failed to connect to MCP during processing: {e}")
                    yield f"❌ MCP 连接失败: {str(e)}\n请检查配置或稍后重试。"
                    return

            config = {"configurable": {"thread_id": user_id}}
            
            try:
                current_state = await self.app.aget_state(config)
            except Exception:
                current_state = None

            input_messages = []
            if not current_state or not current_state.values:
                # Reload system prompt for new threads to support hot-updates
                self._load_system_prompt()
                input_messages.append(SystemMessage(content=self.system_content))
            
            input_messages.append(HumanMessage(content=user_input))

            final_content = ""
            current_status = "正在思考..."
            # Initial status
            yield current_status
            
            async for event in self.app.astream({"messages": input_messages}, config=config, stream_mode="updates"):
                for node_name, output in event.items():
                    if node_name == "agent":
                        last_msg = output["messages"][-1]
                        if last_msg.tool_calls:
                            tool_info_list = []
                            for tc in last_msg.tool_calls:
                                tool_name = tc["name"].split(']')[-1].strip() if ']' in tc["name"] else tc["name"]
                                tool_args = json.dumps(tc["args"], ensure_ascii=False)
                                tool_info_list.append(f"{tool_name}({tool_args})")
                            
                            current_status += f"\n正在调用工具: {', '.join(tool_info_list)}..."
                            yield current_status
                        else:
                            final_content = last_msg.content
                    elif node_name == "tools":
                        current_status += "\n正在思考..."
                        yield current_status
            
            if not final_content:
                final_content = "处理完成，但未生成具体回复内容。"
                
            logger.info(f"[{user_id}] Response generated in {time.time() - start_time:.2f}s")
            # The final answer will replace the entire status block in the UI
            yield final_content
            
        except (httpx.ReadTimeout, httpx.ConnectError, ConnectionError) as e:
            logger.warning(f"Connection error during process_message: {e}. Reconnecting...")
            try:
                await self.connect()
                # Retry once
                async for event in self.app.astream({"messages": input_messages}, config=config, stream_mode="updates"):
                    if "agent" in event:
                        last_msg = event["agent"]["messages"][-1]
                        if not last_msg.tool_calls:
                            yield last_msg.content
            except Exception as retry_err:
                yield f"❌ 连接异常且重试失败: {str(retry_err)}"
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            yield f"❌ 处理消息时发生错误: {str(e)}"

mcp_client = MCPClient()
