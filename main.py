import uvicorn
import logging
import asyncio
from fastapi import FastAPI
from contextlib import asynccontextmanager
from app.llm.client import mcp_client
from app.core.wechat_bot import wechat_bot

# Configure logging with timestamp
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize MCP Client
    # Run connection in background task to not block the main loop
    # and allow signals to be processed
    connect_task = asyncio.create_task(mcp_client.connect())
    
    # Also start WeChat Bot in background
    wechat_task = asyncio.create_task(wechat_bot.connect())

    yield
    
    # Shutdown
    # Cancel background tasks if they are still running
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
