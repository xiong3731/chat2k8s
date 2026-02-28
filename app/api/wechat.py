import asyncio
import httpx
from fastapi import APIRouter, Request, HTTPException, Response, BackgroundTasks
from wechatpy.enterprise.crypto import WeChatCrypto
from wechatpy.exceptions import InvalidSignatureException
from wechatpy.enterprise import parse_message
from app.core.config import settings
from app.mcp.client import mcp_client

router = APIRouter()

# Initialize WeChatCrypto
# Ensure that we catch initialization errors if settings are missing
try:
    crypto = WeChatCrypto(
        settings.WECHAT_TOKEN,
        settings.WECHAT_ENCODING_AES_KEY,
        settings.WECHAT_CORP_ID
    )
except Exception as e:
    print(f"Warning: WeChatCrypto initialization failed: {e}")
    crypto = None

async def send_wechat_message(user_id: str, content: str):
    """Helper function to send message to WeChat"""
    # Split response if too long (WeChat limit: 2048 bytes)
    MAX_BYTES = 2000 # Safety margin
    chunks = []
    current_chunk = ""
    current_length = 0
    
    # Split by lines to preserve formatting
    lines = content.splitlines(keepends=True)
    if not lines and content:
        lines = [content]
    
    for line in lines:
        line_bytes = line.encode('utf-8')
        line_len = len(line_bytes)
        
        if current_length + line_len > MAX_BYTES:
            # If buffer has content, flush it
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = ""
                current_length = 0
            
            # If the single line is too long, split it by characters
            if line_len > MAX_BYTES:
                for char in line:
                    char_len = len(char.encode('utf-8'))
                    if current_length + char_len > MAX_BYTES:
                        chunks.append(current_chunk)
                        current_chunk = char
                        current_length = char_len
                    else:
                        current_chunk += char
                        current_length += char_len
            else:
                # Line fits in empty buffer
                current_chunk = line
                current_length = line_len
        else:
            current_chunk += line
            current_length += line_len
            
    if current_chunk:
        chunks.append(current_chunk)

    async with httpx.AsyncClient() as client:
        try:
            # Get Token
            token_url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={settings.WECHAT_CORP_ID}&corpsecret={settings.WECHAT_CORP_SECRET}"
            t_res = await client.get(token_url)
            t_data = t_res.json()
            token = t_data.get("access_token")
            
            if not token:
                print(f"!!! Failed to get access token: {t_data}")
                return

            # Send messages
            send_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
            
            for i, chunk in enumerate(chunks):
                payload = {
                    "touser": user_id,
                    "msgtype": "text",
                    "agentid": settings.WECHAT_AGENT_ID,
                    "text": {"content": chunk}
                }
                res = await client.post(send_url, json=payload)
                print(f">>> AI Response part {i+1}/{len(chunks)} sent: {res.json()}")
                print(f">>> Content: {chunk}")
                
        except Exception as e:
            print(f"!!! Error sending message: {e}")

async def ai_thinking_and_reply(user_id: str, user_input: str):
    """Call MCP client and send response to WeChat"""
    print(f">>> Processing message for {user_id}: {user_input}")
    
    try:
        # Call MCP client to process the message, passing user_id for context memory
        ai_response = await mcp_client.process_message(user_input, user_id)
    except Exception as e:
        error_msg = f"Error processing request: {str(e)}"
        print(f"!!! Error in MCP processing: {e}")
        # Send error message to user
        await send_wechat_message(user_id, f"⚠️ **系统错误**\n\n{error_msg}")
        return

    # Send success response
    await send_wechat_message(user_id, ai_response)
@router.get("/wechat")
async def verify_wechat(msg_signature: str, timestamp: str, nonce: str, echostr: str):
    """
    Handle WeChat callback verification (GET request).
    """
    if not crypto:
        raise HTTPException(status_code=500, detail="WeChat configuration error")
        
    try:
        decrypted_echostr = crypto.check_signature(
            msg_signature,
            timestamp,
            nonce,
            echostr
        )
        return Response(content=decrypted_echostr)
    except InvalidSignatureException:
        raise HTTPException(status_code=403, detail="Invalid signature")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/wechat")
async def receive_wechat_message(
    request: Request,
    background_tasks: BackgroundTasks,
    msg_signature: str,
    timestamp: str,
    nonce: str
):
    """
    Handle incoming WeChat messages (POST request).
    """
    if not crypto:
        return Response(content="")
        
    body = await request.body()
    try:
        msg_xml = crypto.decrypt_message(
            body,
            msg_signature,
            timestamp,
            nonce
        )
        msg = parse_message(msg_xml)
        
        if msg.type == 'text':
            background_tasks.add_task(ai_thinking_and_reply, msg.source, msg.content)
            
        return Response(content="")
    except InvalidSignatureException:
        raise HTTPException(status_code=403, detail="Invalid signature")
    except Exception as e:
        print(f"Error processing message: {e}")
        # Return success to WeChat to avoid retries even on error
        return Response(content="")
