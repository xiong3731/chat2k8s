import json
import logging
from typing import List, Dict, Any, Optional

import openai
from mcp import ClientSession
from mcp.client.sse import sse_client
from app.core.config import settings

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MCPClient:
    def __init__(self):
        self.sse_url = settings.MCP_SERVER_URL
        self.api_key = settings.OPENAI_API_KEY
        self.base_url = settings.OPENAI_BASE_URL
        self.model = settings.OPENAI_MODEL
        # In-memory storage for conversation history: {user_id: [messages]}
        self.conversations: Dict[str, List[Dict[str, Any]]] = {}
        # Max conversation history rounds (1 round = user + ai)
        self.max_history_rounds = settings.MAX_HISTORY_ROUNDS

    async def process_message(self, user_input: str, user_id: str = "default_user") -> str:
        """
        Process a user message using MCP tools and OpenAI, with context memory.
        """
        logger.info(f"Processing message for {user_id}: {user_input}")
        
        try:
            # Establish SSE connection and initialize MCP session
            async with sse_client(self.sse_url) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as sess:
                    await sess.initialize()
                    
                    # 1. Get and convert tool definitions
                    m_tools = await sess.list_tools()
                    tools = [
                        {
                            "type": "function",
                            "function": {
                                "name": t.name,
                                "description": t.description,
                                "parameters": t.inputSchema
                            }
                        }
                        for t in m_tools.tools
                    ]
                    
                    # Initialize OpenAI client
                    client = openai.AsyncOpenAI(
                        api_key=self.api_key,
                        base_url=self.base_url
                    )
                    
                    # Retrieve history
                    if user_id not in self.conversations:
                        self.conversations[user_id] = []
                    
                    history = self.conversations[user_id]
                    
                    # Construct current messages list: History + Current User Input
                    # Note: We need to copy history to avoid modifying it during tool execution loop if we don't want to save tool calls
                    # However, usually we want to keep context. For simplicity, let's append user input to history now.
                    
                    current_turn_messages = [{"role": "user", "content": user_input}]
                    
                    # Full context for AI
                    messages = history + current_turn_messages
                    
                    # 2. Inner loop: Handle tool calls automatically (Agent pattern)
                    while True:
                        response = await client.chat.completions.create(
                            model=self.model,
                            messages=messages,
                            tools=tools if tools else None
                        )
                        
                        message = response.choices[0].message
                        messages.append(message)
                        
                        # If no tool calls, return the content
                        if not message.tool_calls:
                            logger.info(f"AI Response: {message.content}")
                            
                            # Update history:
                            # We only want to save the final turn (User + AI Response) to keep history clean?
                            # Or do we save the whole chain including tool calls?
                            # Saving tool calls is better for context but consumes more tokens.
                            # For now, let's save the user input and the final AI response.
                            
                            # Append user input and final response to persistent history
                            self.conversations[user_id].append({"role": "user", "content": user_input})
                            self.conversations[user_id].append({"role": "assistant", "content": message.content or ""})
                            
                            # Trim history if needed (keep last N rounds * 2 messages)
                            if len(self.conversations[user_id]) > self.max_history_rounds * 2:
                                self.conversations[user_id] = self.conversations[user_id][-self.max_history_rounds * 2:]
                                
                            return message.content or ""
                        
                        # Execute tool calls
                        for tool_call in message.tool_calls:
                            try:
                                args = json.loads(tool_call.function.arguments)
                                tool_name = tool_call.function.name
                                logger.info(f"Calling tool: {tool_name} with args: {args}")
                                
                                result = await sess.call_tool(tool_name, args)
                                
                                # Extract text content from result
                                content = "".join(
                                    content.text 
                                    for content in result.content 
                                    if hasattr(content, 'text')
                                )
                                
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "name": tool_name,
                                    "content": content
                                })
                                
                            except Exception as e:
                                logger.error(f"Error executing tool {tool_call.function.name}: {e}")
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "name": tool_call.function.name,
                                    "content": f"Error: {str(e)}"
                                })

        except Exception as e:
            logger.error(f"Error in MCP processing: {e}")
            return f"Error processing request: {str(e)}"

# Global instance
mcp_client = MCPClient()
