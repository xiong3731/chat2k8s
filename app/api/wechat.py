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

async def ai_thinking_and_reply(user_id: str, user_input: str):
    """Call MCP client and send response to WeChat"""
    print(f">>> Processing message for {user_id}: {user_input}")
    
    try:
        # Call MCP client to process the message, passing user_id for context memory
        ai_response = await mcp_client.process_message(user_input, user_id)
    except Exception as e:
        ai_response = f"Error processing request: {str(e)}"
        print(f"!!! Error in MCP processing: {e}")

    # Send response to WeChat
    async with httpx.AsyncClient() as client:
        try:
            # Get Token
            # Note: In production, access_token should be cached and refreshed.
            token_url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={settings.WECHAT_CORP_ID}&corpsecret={settings.WECHAT_CORP_SECRET}"
            t_res = await client.get(token_url)
            t_data = t_res.json()
            token = t_data.get("access_token")
            
            if not token:
                print(f"!!! Failed to get access token: {t_data}")
                return

            # Send message
            payload = {
                "touser": user_id,
                "msgtype": "text",
                "agentid": settings.WECHAT_AGENT_ID,
                "text": {"content": ai_response}
            }
            send_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
            res = await client.post(send_url, json=payload)
            print(f">>> AI Response sent: {res.json()}")
        except Exception as e:
            print(f"!!! Error sending message: {e}")
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
