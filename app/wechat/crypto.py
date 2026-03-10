import base64
import logging
import httpx
from typing import Optional
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend

logger = logging.getLogger(__name__)

def decrypt_wechat_media(encrypted_data: bytes, aes_key: str) -> bytes:
    """
    针对企业微信媒体资源的专用解密工具，兼容 16 字节和 32 字节填充。
    """
    try:
        # --- 核心修复：Base64 填充补全 ---
        missing_padding = len(aes_key) % 4
        if missing_padding:
            aes_key += '=' * (4 - missing_padding)
        
        # 1. 解码 Key
        key = base64.b64decode(aes_key)
        # 2. IV 是 Key 的前 16 字节
        iv = key[:16]
        
        # 3. 初始化 AES-256-CBC
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        
        # 4. 执行解密
        decrypted_padded_data = decryptor.update(encrypted_data) + decryptor.finalize()
        
        # 5. 尝试移除填充
        # 优先尝试文档定义的 32 字节 (256 bits) 填充
        try:
            unpadder = padding.PKCS7(256).unpadder() 
            return unpadder.update(decrypted_padded_data) + unpadder.finalize()
        except ValueError:
            # 回退尝试标准的 16 字节 (128 bits) 填充
            unpadder = padding.PKCS7(128).unpadder()
            return unpadder.update(decrypted_padded_data) + unpadder.finalize()

    except Exception as e:
        logger.error(f"解密流程彻底失败: {e}, aes_key={aes_key}", exc_info=True)
        raise ValueError(f"企业微信资源解密失败: {e}")

async def process_wechat_media(url: str, aes_key: str) -> Optional[bytes]:
    """
    下载并解密资源
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                raw_data = decrypt_wechat_media(resp.content, aes_key)
                return raw_data
            else:
                logger.error(f"下载媒体失败: status_code={resp.status_code}, url={url}")
    except Exception as e:
        logger.error(f"处理媒体资源异常: {e}")
    return None
