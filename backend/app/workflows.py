"""Runs agent workflows: one worker thread, saved progress, recovery after a crash, approval decisions.

All database writes about a run happen here, not inside graph nodes, because nodes may be re-run.
"""
import atexit
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import agents
from app import db as dbmod
from app.config import settings
from app.models import ApprovalRequest, User, WorkflowEvent, WorkflowRun
from app.security import audit, readable_collection_ids

log = logging.getLogger(__name__)

RECURSION_LIMIT = 25    # a final safety net on top of the retry counter
UNFINISHED = ("queued", "running")
FINISHED = ("approved", "rejected", "completed", "failed")

# One worker: the CPU can only usefully run one LLM call at a time.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workflow")
_graph = None


def get_graph():
    """Build the graph once, with progress saved in LangGraph's own PostgreSQL tables."""
    global _graph
    if _graph is None:
        url = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
        pool = ConnectionPool(url, max_size=4, open=True,
                              kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0})
        atexit.register(pool.close)
        saver = PostgresSaver(pool)
        saver.setup()   # creates the checkpoint tables the first time
        _graph = agents.build_graph(saver)
    return _graph


def prepare() -> None:
    """Set the graph up at server start. A failure here is logged, not fatal: chat and documents still
    work, and each run records the error if the problem is still there."""
    try:
        get_graph()
    except Exception:
        log.exception("Could not set up the agent graph")


def enqueue(fn, *args) -> None:
    _executor.submit(fn, *args)


def thread_config(run_id: int) -> dict:
    return {"configurable": {"thread_id": f"run-{run_id}"}, "recursion_limit": RECURSION_LIMIT}


def initial_state(run: WorkflowRun) -> dict:
    return {"run_id": run.id, "user_id": run.user_id, "request": run.request, "collection_ids": run.collection_ids}


# ---------- Turning graph updates into progress messages ----------

def describe(step: str, update: dict) -> str:
    if step == "orchestrator":
        return f"Task type: {update['task_type']}. Searching with {len(update['queries'])} queries."
    if step == "research":
        return f"Found {update['new_evidence']} new passage(s), {len(update['evidence'])} in total."
    if step == "analysis":
        pending = sum(1 for c in update["claims"] if c["status"] is None)
        return f"{pending} claim(s) to verify."
    if step == "verification":
        counts = {s: sum(1 for c in update["claims"] if c["status"] == s)
                  for s in ("supported", "unsupported", "conflicting")}
        return f"{counts['supported']} supported, {counts['unsupported']} unsupported, {counts['conflicting']} conflicting."
    if step == "report":
        return update["report_doc"]["summary"][:300]
    return "Decision received."


def add_event(db: Session, run_id: int, step: str, message: str) -> None:
    db.add(WorkflowEvent(run_id=run_id, step=step, message=message))


# ---------- Driving a run ----------

def drive(run_id: int, graph_input=None) -> None:
    """Run the graph until it finishes or pauses, saving progress after each step."""
    config = thread_config(run_id)
    with dbmod.SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        if run is None:
            return
        try:    # everything inside: an error in a worker thread would otherwise vanish silently
            db.commit()     # no open transaction during setup: CREATE INDEX CONCURRENTLY would wait on it forever
            graph = get_graph()
            run.status = "running"
            db.commit()
            for chunk in graph.stream(graph_input, config, stream_mode="updates"):
                for step, update in chunk.items():
                    if step == "__interrupt__" or not update:
                        continue
                    run.current_step = step
                    if step == "orchestrator":
                        run.task_type = update["task_type"]
                    if step == "report":
                        run.report = update["report_doc"]
                    add_event(db, run_id, step, describe(step, update))
                    db.commit()
            settle(db, run, graph.get_state(config))
        except Exception as e:
            log.exception("Workflow %s failed", run_id)
            db.rollback()
            run = db.get(WorkflowRun, run_id)
            run.status, run.error = "failed", f"{type(e).__name__}: {e}"[:500]
            run.finished_at = datetime.now(timezone.utc)
            add_event(db, run_id, "failed", "The workflow stopped because of an error.")
            db.commit()


def settle(db: Session, run: WorkflowRun, snapshot) -> None:
    """After the graph stops: is it paused for a person, or finished?"""
    if snapshot.next:
        run.status, run.current_step = "awaiting_approval", "approval"
        if run.approval is None:
            db.add(ApprovalRequest(run_id=run.id, requested_by=run.user_id))
        add_event(db, run.id, "approval", "Waiting for a reviewer to approve or reject.")
    else:
        decision = snapshot.values.get("decision")
        run.status = "completed" if decision is None else ("approved" if decision["approved"] else "rejected")
        run.finished_at = datetime.now(timezone.utc)
        add_event(db, run.id, "done", {"approved": "Report approved.", "rejected": "Report rejected."}.get(run.status, "No findings to approve."))
    db.commit()


def start_run(db: Session, user: User, request: str, collection_id: int | None = None) -> WorkflowRun:
    """Create a run. Which collections it may search is decided here, in code, once."""
    collections = readable_collection_ids(db, user)
    if collection_id is not None:
        collections = [c for c in collections if c == collection_id]
    run = WorkflowRun(user_id=user.id, request=request, collection_ids=collections)
    db.add(run)
    db.flush()
    audit(db, user.id, "workflow.start", "workflow_run", run.id, collections=collections)
    db.commit()
    enqueue(drive, run.id, initial_state(run))
    return run


def resume_unfinished() -> list[int]:
    """At server start, continue runs a previous process left unfinished, from their last saved step."""
    with dbmod.SessionLocal() as db:
        runs = db.scalars(select(WorkflowRun).where(WorkflowRun.status.in_(UNFINISHED))).all()
        if not runs:
            return []
        graph = get_graph()
        for run in runs:
            saved = graph.get_state(thread_config(run.id))
            add_event(db, run.id, "recovery", "Server restarted: continuing from the last saved step.")
            db.commit()
            enqueue(drive, run.id, recovery_input(run, saved))
        return [r.id for r in runs]


def recovery_input(run: WorkflowRun, saved):
    if not saved.values:
        return initial_state(run)   # crashed before the first step was saved
    if saved.next == ("approval",) and run.approval is not None and run.approval.decision:
        # The decision was recorded but the process died before the graph resumed.
        return decision_command(run.approval)
    return None                     # None = continue from the last checkpoint


# ---------- Approvals ----------

def can_review(db: Session, user: User, run: WorkflowRun) -> bool:
    """An analyst or admin who can read everything the run searched, and not the person who started it."""
    return (user.role in ("analyst", "admin") and user.id != run.user_id
            and set(run.collection_ids) <= set(readable_collection_ids(db, user)))


def can_view(db: Session, user: User, run: WorkflowRun) -> bool:
    return user.id == run.user_id or can_review(db, user, run)


def decision_command(approval: ApprovalRequest) -> Command:
    return Command(resume={"approved": approval.decision == "approved", "comment": approval.comment})


def decide(db: Session, approval: ApprovalRequest, user: User, approved: bool, comment: str | None) -> None:
    """Record the decision first, then let the paused graph continue."""
    approval.decision = "approved" if approved else "rejected"
    approval.comment = comment
    approval.decided_by = user.id
    approval.decided_at = datetime.now(timezone.utc)
    approval.run.status = "running"
    add_event(db, approval.run_id, "approval", f"{approval.decision.capitalize()} by {user.email}."
              + (f" Comment: {comment}" if comment else ""))
    audit(db, user.id, f"workflow.{approval.decision}", "workflow_run", approval.run_id, comment=comment)
    db.commit()
    enqueue(drive, approval.run_id, decision_command(approval))
