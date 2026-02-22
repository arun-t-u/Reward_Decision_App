from fastapi import FastAPI
from app.api.routes import router as api_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(title="Reward Decision App")


@app.get("/health")
def health_check():
    return {"status": "ok"}

app.include_router(api_router)


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )

# uvicorn app.main:app --reload
