import asyncio
import json
import logging
import time
import uuid
import websockets
from typing import Any, Dict, Optional, List, Union

from app.core.config import settings
from app.llm.client import mcp_client
from app.wechat.handlers import handle_wechat_message

logger = logging.getLogger(__name__)

class WeChatBot:
    """
    WeChat Bot 客户端，用于处理与企业微信智能机器人 OpenWS 服务器的 WebSocket 长连接。
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
        self._send_lock = asyncio.Lock()  
        
        self._system_handlers = {
            "/clear": self._handle_cmd_clean,
        }

    async def _handle_cmd_clean(self, session_id: str, req_id: str, content: str):
        """处理 clean 指令：清理上下文"""
        logger.info(f"[{session_id}] 执行系统指令: clean")
        await mcp_client.clear_context(session_id)
        await self._stream_respond("✅ 已清理您的对话上下文，我们可以开始新的对话了。", session_id, req_id, skip_logic=True)
        return True

    async def _dispatch_system_cmd(self, content: str, session_id: str, req_id: str) -> bool:
        """分发系统指令"""
        if not content or not isinstance(content, str):
            return False
            
        cmd_key = content.strip().lower()
        handler = self._system_handlers.get(cmd_key)
        
        if handler:
            return await handler(session_id, req_id, content)
        return False

    async def send_cmd(self, cmd: str, body: Dict[str, Any], req_id: str = None):
        """通用命令发送包装器"""
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
                    logger.warning(f"连接已关闭，命令 {cmd} 被跳过")
            except websockets.exceptions.ConnectionClosed:
                logger.warning(f"连接已关闭，命令 {cmd} 发送失败")
            except Exception as e:
                logger.error(f"发送命令 {cmd} 失败: {e}")

    async def connect(self):
        """建立 WebSocket 连接并进入订阅循环"""
        self._is_running = True
        while self._is_running:
            try:
                logger.info(f"正在建立长连接: {self.wss_url}")
                async with websockets.connect(self.wss_url, ping_interval=None) as ws:
                    self.ws = ws
                    if await self.subscribe():
                        await asyncio.gather(
                            self.keep_alive(), 
                            self.receive_messages()
                        )
            except asyncio.CancelledError:
                logger.info("微信连接任务被取消，正在停止...")
                break
            except Exception as e:
                if self._is_running:
                    logger.error(f"网络异常: {e}, 5秒后尝试重连...")
                    try:
                        await asyncio.sleep(5)
                    except asyncio.CancelledError:
                        break
                else:
                    break

    async def stop(self):
        """停止机器人并关闭 WebSocket 连接"""
        self._is_running = False
        if self.ws:
            await self.ws.close()
            logger.info("微信机器人已停止，连接已关闭。")

    async def subscribe(self) -> bool:
        """身份验证与订阅"""
        if not self.bot_id or not self.secret:
            logger.error("未配置 WECHAT_BOT_ID 或 WECHAT_BOT_SECRET！")
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
                logger.info("微信长连接订阅成功！")
                return True
            logger.error(f"订阅失败，详情: {data}")
            return False
        except Exception as e:
            logger.error(f"接收订阅响应超时或异常: {e}")
            return False

    async def keep_alive(self):
        """业务级心跳任务（PING）"""
        while self._is_running:
            try:
                await asyncio.sleep(30) 
                await self.send_cmd("ping", {}, req_id=f"ping_{int(time.time())}")
            except Exception:
                logger.warning("心跳发送失败，停止心跳循环。")
                break

    async def receive_messages(self):
        """监听并分发收到的 WebSocket 消息"""
        async for message in self.ws:
            if not self._is_running:
                break
            try:
                data = json.loads(message)
                cmd = data.get("cmd")
                if cmd == "aibot_msg_callback":
                    asyncio.create_task(self.handle_msg(data))
                elif cmd == "pong":
                    logger.debug("收到 PONG 响应")
                elif cmd == "error":
                    logger.error(f"收到服务器错误通知: {data}")
            except Exception as e:
                logger.error(f"解析消息异常: {e}")

    async def handle_msg(self, req_data: Dict[str, Any]):
        """处理来自微信的消息"""
        headers = req_data.get("headers", {})
        body = req_data.get("body", {})
        req_id = headers.get("req_id")
        
        userid = (
            body.get("from", {}).get("userid") or 
            body.get("sender") or 
            headers.get("from_user_id") or 
            "default_user"
        )
        session_id = userid
        
        logger.info(f"收到来自 {session_id} 的消息 (req_id: {req_id})")

        try:
            processed_content = await handle_wechat_message(body)
            if not processed_content:
                logger.info(f"[{session_id}] 消息内容为空或无法处理，跳过。")
                return

            if isinstance(processed_content, str) and await self._dispatch_system_cmd(processed_content, session_id, req_id):
                return

            await self._stream_respond(processed_content, session_id, req_id)

        except Exception as e:
            logger.error(f"处理消息异常: {e}", exc_info=True)
            await self._stream_respond(f"抱歉，处理您的消息时遇到错误: {str(e)}", session_id, req_id, skip_logic=True)

    async def _stream_respond(self, content: Any, session_id: str, req_id: str, skip_logic: bool = False):
        """流式回复逻辑"""
        stream_id = str(uuid.uuid4())
        last_content = "正在思考..."
        stream_started = False
        try:
            logger.info(f"[{session_id}] 开始回复 (req_id: {req_id})")
            await self.send_cmd("aibot_respond_msg", {
                "msgtype": "stream",
                "stream": {"id": stream_id, "finish": False, "content": last_content}
            }, req_id=req_id)
            stream_started = True

            if skip_logic:
                last_content = content
            else:
                logger.info(f"[{session_id}] 调用 LLM 处理消息...")
                async for chunk in mcp_client.process_message(content, user_id=session_id):
                    last_content = chunk
                    logger.debug(f"[{session_id}] 发送流式分片: {len(chunk)} chars")
                    await self.send_cmd("aibot_respond_msg", {
                        "msgtype": "stream",
                        "stream": {"id": stream_id, "finish": False, "content": last_content}
                    }, req_id=req_id)
            
            logger.info(f"[{session_id}] 回复完成 (stream_id: {stream_id})")
            await self.send_cmd("aibot_respond_msg", {
                "msgtype": "stream",
                "stream": {"id": stream_id, "finish": True, "content": last_content}
            }, req_id=req_id)
        except Exception as e:
            logger.error(f"响应失败: {e}", exc_info=True)
            error_msg = f"抱歉，处理消息时发生错误: {str(e)}"
            if stream_started:
                await self.send_cmd("aibot_respond_msg", {
                    "msgtype": "stream",
                    "stream": {"id": stream_id, "finish": True, "content": error_msg}
                }, req_id=req_id)
            else:
                await self.send_cmd("aibot_respond_msg", {
                    "msgtype": "text",
                    "text": {"content": error_msg}
                }, req_id=req_id)

wechat_bot = WeChatBot()
