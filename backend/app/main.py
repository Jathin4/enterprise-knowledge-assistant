import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.ingestion import resume_unfinished
from app import workflows
from app.routers import auth, chat, documents, evaluations
from app.routers import workflows as workflow_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    resumed = resume_unfinished()
    if resumed:
        logging.getLogger(__name__).info("Resuming unfinished documents: %s", resumed)
    workflows.prepare()     # create LangGraph's tables now, before any request can hold a transaction open
    resumed_runs = workflows.resume_unfinished()
    if resumed_runs:
        logging.getLogger(__name__).info("Resuming unfinished workflows: %s", resumed_runs)
    yield


app = FastAPI(title="Enterprise AI Knowledge Assistant", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(chat.router)
app.include_router(evaluations.router)
app.include_router(workflow_routes.router)


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    try:
        ollama = httpx.get(f"{settings.ollama_url}/api/version", timeout=5).json()["version"]
    except httpx.HTTPError:
        ollama = None
    return {"database": "ok", "ollama": ollama or "unreachable"}
