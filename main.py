import uvicorn
from fastapi import FastAPI
from app.api.wechat import router as wechat_router

app = FastAPI()

app.include_router(wechat_router)

@app.get("/health")
async def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=6789)
