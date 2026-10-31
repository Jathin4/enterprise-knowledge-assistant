import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db as dbmod
from app.db import get_db
from app.evaluation import run_evaluation
from app.models import EvaluationRun, User
from app.security import audit, require_admin

router = APIRouter(prefix="/api/evaluations", tags=["evaluations"])
log = logging.getLogger(__name__)


class EvaluationRequest(BaseModel):
    mode: Literal["retrieval", "full"] = "retrieval"
    label: str = Field(min_length=1, max_length=100)


class EvaluationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    label: str
    mode: str
    status: str
    progress: int
    total: int
    error: str | None
    config: dict
    metrics: dict
    duration_s: float | None
    created_at: datetime


class EvaluationDetail(EvaluationSummary):
    results: list[dict]


def _run_in_background(run_id: int) -> None:
    with dbmod.SessionLocal() as db:
        try:
            run_evaluation(db, db.get(EvaluationRun, run_id))
        except Exception:
            log.exception("Evaluation %s failed", run_id)


@router.post("", response_model=EvaluationSummary, status_code=202)
def start_evaluation(req: EvaluationRequest, tasks: BackgroundTasks,
                     user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Start a benchmark run (admin only). 'retrieval' takes seconds; 'full' takes tens of minutes."""
    if db.scalar(select(EvaluationRun.id).where(EvaluationRun.status == "running")):
        raise HTTPException(409, "An evaluation is already running")
    run = EvaluationRun(label=req.label, mode=req.mode, created_by=user.id)
    db.add(run)
    db.flush()
    audit(db, user.id, "evaluation.start", "evaluation_run", run.id, mode=req.mode)
    db.commit()
    tasks.add_task(_run_in_background, run.id)
    return run


@router.get("", response_model=list[EvaluationSummary])
def list_evaluations(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return db.scalars(select(EvaluationRun).order_by(EvaluationRun.id.desc())).all()


@router.get("/{run_id}", response_model=EvaluationDetail)
def get_evaluation(run_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    run = db.get(EvaluationRun, run_id)
    if run is None:
        raise HTTPException(404, "Evaluation not found")
    return run
