import uvicorn

from .config import Settings


if __name__ == "__main__":
    settings = Settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level=settings.log_level.lower())
