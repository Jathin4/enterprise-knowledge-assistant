import json
import time
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db as dbmod
from app import workflows
from app.db import get_db
from app.models import ApprovalRequest, User, WorkflowEvent, WorkflowRun
from app.security import get_current_user, readable_collection_ids

router = APIRouter(prefix="/api", tags=["agents"])

STREAM_LIMIT_S = 3600   # a stream ends after an hour; the browser can simply reconnect


class ReportRequest(BaseModel):
    request: str = Field(min_length=5, max_length=2000)
    collection_id: int | None = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    step: str
    message: str
    created_at: datetime


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    run_id: int
    requested_by: int
    decided_by: int | None
    decision: str | None
    comment: str | None
    decided_at: datetime | None


class RunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    request: str
    task_type: str | None
    status: str
    current_step: str | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class RunDetail(RunSummary):
    collection_ids: list[int]
    report: dict | None
    events: list[EventOut]
    approval: ApprovalOut | None
    can_review: bool = False


class Decision(BaseModel):
    decision: Literal["approved", "rejected"]
    comment: str | None = Field(default=None, max_length=2000)


def viewable_run(db: Session, user: User, run_id: int) -> WorkflowRun:
    run = db.get(WorkflowRun, run_id)
    if run is None or not workflows.can_view(db, user, run):
        raise HTTPException(404, "Workflow not found")   # 404, so others can't learn it exists
    return run


@router.post("/reports", response_model=RunSummary, status_code=202)
def start_report(req: ReportRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Start a research/report workflow over your collections. Runs in the background."""
    readable = readable_collection_ids(db, user)
    if req.collection_id is not None and req.collection_id not in readable:
        raise HTTPException(404, "Collection not found")
    if not readable:
        raise HTTPException(400, "You are not a member of any collection")
    return workflows.start_run(db, user, req.request.strip(), req.collection_id)


@router.get("/workflows", response_model=list[RunSummary])
def list_runs(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Your own runs, plus runs you are allowed to review."""
    runs = db.scalars(select(WorkflowRun).order_by(WorkflowRun.id.desc()).limit(100)).all()
    return [r for r in runs if workflows.can_view(db, user, r)]


@router.get("/workflows/{run_id}", response_model=RunDetail)
def get_run(run_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    run = viewable_run(db, user, run_id)
    detail = RunDetail.model_validate(run)
    detail.can_review = workflows.can_review(db, user, run)
    return detail


@router.get("/workflows/{run_id}/events")
def stream_events(run_id: int, after: int = 0, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Progress as Server-Sent Events. Saved events are replayed first, so reconnecting loses nothing:
    pass ?after=<last event id>. The stream ends when the run pauses for approval or finishes."""
    viewable_run(db, user, run_id)
    # End the request's transaction now: it would otherwise stay open for the whole stream, and
    # block anything that waits for open transactions (LangGraph's CREATE INDEX CONCURRENTLY deadlocked on it).
    db.close()

    def events():
        last, deadline = after, time.monotonic() + STREAM_LIMIT_S
        while time.monotonic() < deadline:
            with dbmod.SessionLocal() as s:   # own session: the request's may close before the stream ends
                rows = s.scalars(select(WorkflowEvent).where(WorkflowEvent.run_id == run_id, WorkflowEvent.id > last)
                                 .order_by(WorkflowEvent.id)).all()
                run = s.get(WorkflowRun, run_id)
                status = run.status if run else "failed"
            for e in rows:
                last = e.id
                yield f"data: {json.dumps({'type': 'event', 'id': e.id, 'step': e.step, 'message': e.message})}\n\n"
            if status not in workflows.UNFINISHED:
                yield f"data: {json.dumps({'type': 'status', 'status': status})}\n\n"
                return
            yield ": keep-alive\n\n"
            time.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/workflows/{run_id}/retry", response_model=RunSummary, status_code=202)
def retry_run(run_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Continue a failed run from its last saved step (the owner only)."""
    run = viewable_run(db, user, run_id)
    if run.user_id != user.id:
        raise HTTPException(403, "Only the person who started this run can retry it")
    if run.status != "failed":
        raise HTTPException(409, "Only a failed run can be retried")
    run.status, run.error, run.finished_at = "queued", None, None
    db.commit()
    workflows.enqueue(workflows.drive, run.id, workflows.recovery_input(run, workflows.get_graph().get_state(workflows.thread_config(run.id))))
    return run


@router.get("/approvals", response_model=list[RunSummary])
def list_approvals(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Runs waiting for a decision that you are allowed to make."""
    runs = db.scalars(select(WorkflowRun).where(WorkflowRun.status == "awaiting_approval")
                      .order_by(WorkflowRun.id)).all()
    return [r for r in runs if workflows.can_review(db, user, r)]


@router.post("/approvals/{run_id}", response_model=RunSummary)
def decide_approval(run_id: int, body: Decision, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Approve or reject a paused run. Locks the row so two reviewers can't both decide."""
    approval = db.scalar(select(ApprovalRequest).where(ApprovalRequest.run_id == run_id).with_for_update())
    run = viewable_run(db, user, run_id)
    if not workflows.can_review(db, user, run):
        raise HTTPException(403, "You can't review this report: reviewers must be an analyst or admin who can "
                                 "read its collections, and not the person who started it")
    if approval is None or approval.decision or run.status != "awaiting_approval":
        raise HTTPException(409, "This report is not waiting for a decision")
    workflows.decide(db, approval, user, body.decision == "approved", body.comment)
    return run
