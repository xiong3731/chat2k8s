import uvicorn
import logging
import asyncio
from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.llm.client import mcp_client
from app.wechat.bot import wechat_bot

# Configure logging with timestamp
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    connect_task = asyncio.create_task(mcp_client.connect())
    wechat_task = asyncio.create_task(wechat_bot.connect())
    yield
    connect_task.cancel()
    wechat_task.cancel()
    try:
        await asyncio.gather(connect_task, wechat_task, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    await mcp_client.close()
    await wechat_bot.stop()
app = FastAPI(lifespan=lifespan)
@app.get("/health")
async def health_check():
    return {"status": "ok"}
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=6789)