import asyncio
import json
import logging
import time
import uuid
import websockets
from typing import Any, Dict, Optional, Callable, Awaitable

from app.core.config import settings
from app.llm.client import mcp_client

logger = logging.getLogger(__name__)

class WeChatBot:
    """
    WeChat Bot client for handling WebSocket connections with the WeChat OpenWS server.
    """
    def __init__(
        self, 
        bot_id: str = settings.WECHAT_BOT_ID, 
        secret: str = settings.WECHAT_BOT_SECRET, 
        wss_url: str = settings.WECHAT_WSS_URL
    ):
        self.bot_id = bot_id
        self.secret = secret
        self.wss_url = wss_url
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._is_running = False
        self._send_lock = asyncio.Lock()  # Prevent concurrent write issues in the protocol
        self._message_handler: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None

    def set_message_handler(self, handler: Callable[[Dict[str, Any]], Awaitable[None]]):
        """
        Set a custom handler for incoming messages.
        """
        self._message_handler = handler

    async def send_cmd(self, cmd: str, body: Dict[str, Any], req_id: str = None):
        """
        Universal command sending wrapper.
        """
        data = {
            "cmd": cmd,
            "headers": {"req_id": req_id or f"req_{uuid.uuid4().hex[:8]}"},
            "body": body
        }
        async with self._send_lock:
            try:
                if self.ws:
                    await self.ws.send(json.dumps(data))
                else:
                    logger.warning(f"Connection closed, command {cmd} skipped")
            except websockets.exceptions.ConnectionClosed:
                logger.warning(f"Connection closed, command {cmd} send failed")
            except Exception as e:
                logger.error(f"Failed to send command {cmd}: {e}")

    async def connect(self):
        """
        Establish connection and execute the subscription loop.
        """
        self._is_running = True
        while self._is_running:
            try:
                logger.info(f"Establishing long connection: {self.wss_url}")
                async with websockets.connect(self.wss_url, ping_interval=None) as ws:
                    self.ws = ws
                    if await self.subscribe():
                        # Concurrent tasks: Keep-alive + Message receiving
                        await asyncio.gather(
                            self.keep_alive(), 
                            self.receive_messages()
                        )
            except asyncio.CancelledError:
                logger.info("WeChat connection task cancelled, stopping...")
                break
            except Exception as e:
                if self._is_running:
                    logger.error(f"Network exception: {e}, reconnecting in 5 seconds...")
                    try:
                        await asyncio.sleep(5)
                    except asyncio.CancelledError:
                        break
                else:
                    break

    async def stop(self):
        """
        Stop the bot and close the connection.
        """
        self._is_running = False
        if self.ws:
            await self.ws.close()
            logger.info("WeChat bot stopped and connection closed.")

    async def subscribe(self) -> bool:
        """
        Identity verification and subscription (must be the first step after connection).
        """
        if not self.bot_id or not self.secret:
            logger.error("WECHAT_BOT_ID or WECHAT_BOT_SECRET not configured!")
            return False

        sub_id = f"sub_{uuid.uuid4().hex[:8]}"
        await self.send_cmd("aibot_subscribe", {
            "bot_id": self.bot_id, 
            "secret": self.secret
        }, req_id=sub_id)
        
        try:
            resp = await self.ws.recv()
            data = json.loads(resp)
            if data.get("errcode") == 0:
                logger.info("WeChat long connection subscribed successfully!")
                return True
            logger.error(f"Subscription failed, details: {data}")
            return False
        except Exception as e:
            logger.error(f"Timeout or exception receiving subscription response: {e}")
            return False

    async def keep_alive(self):
        """
        Business-level heartbeat (PING).
        """
        while self._is_running:
            try:
                await asyncio.sleep(30)
                await self.send_cmd("ping", {}, req_id=f"ping_{int(time.time())}")
            except Exception:
                logger.warning("Heartbeat failed, stopping keep_alive loop.")
                break

    async def receive_messages(self):
        """
        Listen for and dispatch messages.
        """
        async for message in self.ws:
            if not self._is_running:
                break
            try:
                data = json.loads(message)
                cmd = data.get("cmd")
                
                if cmd == "aibot_msg_callback":
                    # Handle message asynchronously to avoid blocking the receive loop
                    if self._message_handler:
                        asyncio.create_task(self._message_handler(data))
                    else:
                        asyncio.create_task(self.handle_msg(data))
                elif cmd == "pong":
                    logger.debug("Received PONG")
                elif cmd == "error":
                    logger.error(f"Received error from server: {data}")
            except Exception as e:
                logger.error(f"Exception parsing message: {e}")

    async def handle_msg(self, req_data: Dict[str, Any]):
        """
        Default handler for received user messages. Calls LLM and streams response.
        """
        headers = req_data.get("headers", {})
        body = req_data.get("body", {})
        req_id = headers.get("req_id")
        content = body.get("text", {}).get("content", "")
        
        # 提取会话 ID，确保不同用户/会话的上下文隔离
        # 1. 优先使用 userid (确保用户级别的上下文隔离)
        # 2. 如果在群聊中且需要群组级别的隔离，可以使用 chatid
        # 这里遵循用户要求“不同用户隔离”，故优先使用 userid
        userid = (
            body.get("from", {}).get("userid") or 
            body.get("sender") or 
            headers.get("from_user_id") or 
            "default_user"
        )
        
        # 如果是群聊，可以考虑将 chatid 加入 context key 以实现更细粒度的隔离
        # 但目前先满足用户最核心的“用户间隔离”需求
        session_id = userid
        
        logger.info(f"Received user message from {session_id} (req_id: {req_id}, type: {body.get('chattype', 'single')}): {content}")
        
        # Start streaming response from LLM
        stream_id = str(uuid.uuid4())
        last_content = "正在思考..."
        stream_started = False
        try:
            # 1. 发送初始状态
            logger.info(f"Sending initial 'Thinking' frame for req_id: {req_id}")
            await self.send_cmd("aibot_respond_msg", {
                "msgtype": "stream",
                "stream": {"id": stream_id, "finish": False, "content": last_content}
            }, req_id=req_id)
            stream_started = True
            logger.info("Initial 'Thinking' frame sent.")

            # 2. 随后流式输出 LLM 的中间过程 and 最终答案
            logger.info("Starting LLM stream processing...")
            async for chunk in mcp_client.process_message(content, user_id=session_id):
                last_content = chunk
                logger.info(f"Sending stream chunk: {last_content}")
                await self.send_cmd("aibot_respond_msg", {
                    "msgtype": "stream",
                    "stream": {"id": stream_id, "finish": False, "content": last_content}
                }, req_id=req_id)
            
            # 3. 结束流式输出，最后一帧必须包含最终内容且 finish 为 True
            logger.info(f"Sending final frame for stream_id: {stream_id}")
            await self.send_cmd("aibot_respond_msg", {
                "msgtype": "stream",
                "stream": {"id": stream_id, "finish": True, "content": last_content}
            }, req_id=req_id)
            
            logger.info(f"Streaming response {stream_id} completed.")
        except Exception as e:
            logger.error(f"Error in LLM processing or streaming: {e}")
            error_msg = f"抱歉，处理消息时发生错误: {str(e)}"
            
            if stream_started:
                # 如果已经开始了流式输出，必须发送一个 finish=True 的帧来结束“正在思考”状态
                await self.send_cmd("aibot_respond_msg", {
                    "msgtype": "stream",
                    "stream": {"id": stream_id, "finish": True, "content": error_msg}
                }, req_id=req_id)
            else:
                # 如果还没开始流式（极少见），发送普通文本
                await self.send_cmd("aibot_respond_msg", {
                    "msgtype": "text",
                    "text": {"content": error_msg}
                }, req_id=req_id)

# Create default instance for easy import
wechat_bot = WeChatBot()

if __name__ == "__main__":
    # Basic logging setup for direct execution
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    try:
        asyncio.run(wechat_bot.connect())
    except KeyboardInterrupt:
        logger.info("Stopped by user")
