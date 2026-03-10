import base64
import logging
from typing import Any, Dict, List, Union, Optional
from app.wechat.crypto import process_wechat_media

logger = logging.getLogger(__name__)

async def handle_wechat_message(body: Dict[str, Any]) -> Optional[Union[str, List[Dict[str, Any]]]]:
    """
    解析企业微信消息体，支持多媒体（图片、语音、文件、图文混排）。
    """
    msg_type = body.get("msgtype", "text")
    processed_content: Union[str, List[Dict[str, Any]]] = ""

    try:
        if msg_type == "text":
            processed_content = body.get("text", {}).get("content", "")

        elif msg_type == "voice":
            voice_text = body.get("voice", {}).get("content", "")
            processed_content = f"[语音转文字]: {voice_text}"

        elif msg_type == "image":
            img_data = body.get("image", {})
            raw_bytes = await process_wechat_media(img_data.get("url"), img_data.get("aeskey"))
            if raw_bytes:
                base64_img = base64.b64encode(raw_bytes).decode("utf-8")
                processed_content = [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"}
                    },
                    {"type": "text", "text": "请分析图片中的内容，尤其是系统报错或 K8s 相关信息。"}
                ]
            else:
                processed_content = "[收到图片，但解密失败]"

        elif msg_type == "mixed":
            items = body.get("mixed", {}).get("msg_item", [])
            parts = []
            for item in items:
                it_type = item.get("msgtype")
                if it_type == "text":
                    parts.append({"type": "text", "text": item.get("text", {}).get("content", "")})
                elif it_type == "image":
                    img_data = item.get("image", {})
                    raw_bytes = await process_wechat_media(img_data.get("url"), img_data.get("aeskey"))
                    if raw_bytes:
                        base64_img = base64.b64encode(raw_bytes).decode("utf-8")
                        parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"}
                        })
                    else:
                        parts.append({"type": "text", "text": "[图片解密失败]"})
            processed_content = parts

        elif msg_type == "file":
            file_data = body.get("file", {})
            file_name = file_data.get("name", "unknown_file")
            logger.info(f"收到文件: {file_name}")
            raw_bytes = await process_wechat_media(file_data.get("url"), file_data.get("aeskey"))
            
            if raw_bytes:
                try:
                    file_text = raw_bytes.decode("utf-8")
                    processed_content = f"用户上传了文件 [{file_name}]，内容如下：\n```\n{file_text}\n```\n请根据文件内容回答问题。"
                except UnicodeDecodeError:
                    logger.warning(f"文件 {file_name} 解码失败，可能为二进制文件")
                    processed_content = f"收到二进制文件 [{file_name}]，目前仅支持直接分析文本类文件（如日志、YAML 等）。"
            else:
                logger.error(f"文件 {file_name} 下载或解密失败")
                processed_content = f"收到文件 [{file_name}]，但下载或解密失败。"

        return processed_content

    except Exception as e:
        logger.error(f"解析消息异常: {e}", exc_info=True)
        return f"抱歉，解析您的消息时遇到错误: {str(e)}"
