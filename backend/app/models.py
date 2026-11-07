from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.db import Base

ROLES = ("employee", "analyst", "admin")
STATUSES = ("queued", "extracting", "embedding", "ready", "failed")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(f"role IN {ROLES}", name="ck_users_role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str]
    role: Mapped[str] = mapped_column(String(20), default="employee")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Collection(Base):
    """A group of documents, e.g. 'Engineering'. Members may read and upload."""
    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CollectionMember(Base):
    __tablename__ = "collection_members"

    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(f"status IN {STATUSES}", name="ck_documents_status"),
        UniqueConstraint("collection_id", "checksum", name="uq_documents_collection_checksum"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(10))
    size_bytes: Mapped[int]
    checksum: Mapped[str] = mapped_column(String(64))
    storage_path: Mapped[str]
    status: Mapped[str] = mapped_column(String(20), default="queued")
    error: Mapped[str | None]
    page_count: Mapped[int | None]
    chunk_count: Mapped[int] = mapped_column(default=0)
    quarantined_chunks: Mapped[int] = mapped_column(default=0, server_default="0")   # suspected prompt injection
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_chunks_document_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    chunk_index: Mapped[int]
    page_number: Mapped[int | None]
    heading: Mapped[str | None]
    content: Mapped[str]
    embedding = mapped_column(Vector(settings.embed_dim), nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    document: Mapped[Document] = relationship(back_populates="chunks")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    messages: Mapped[list["Message"]] = relationship(
        order_by="Message.id", cascade="all, delete-orphan", passive_deletes=True
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str]
    # A snapshot of each cited source (file, page, heading, snippet), so old answers still
    # show their evidence even after the document is deleted or re-indexed.
    sources: Mapped[list] = mapped_column(JSONB, default=list)
    abstained: Mapped[bool] = mapped_column(default=False)
    latency_ms: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EvaluationRun(Base):
    """One run of the benchmark: the settings used, summary metrics and every question's result."""
    __tablename__ = "evaluation_runs"
    __table_args__ = (CheckConstraint("status IN ('running', 'done', 'failed')", name="ck_evaluation_runs_status"),
                      CheckConstraint("mode IN ('retrieval', 'full')", name="ck_evaluation_runs_mode"))

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(100))
    mode: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="running")
    progress: Mapped[int] = mapped_column(default=0)
    total: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None]
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    results: Mapped[list] = mapped_column(JSONB, default=list)
    duration_s: Mapped[float | None]
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


RUN_STATUSES = ("queued", "running", "awaiting_approval", "approved", "rejected", "completed", "failed")


class WorkflowRun(Base):
    """One agent workflow. The detailed graph state lives in LangGraph's checkpoint tables;
    this row is what the API, permissions and UI use."""
    __tablename__ = "workflow_runs"
    __table_args__ = (CheckConstraint(f"status IN {RUN_STATUSES}", name="ck_workflow_runs_status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    request: Mapped[str]
    # Fixed in code when the run starts. Every search also re-checks the user's current membership.
    collection_ids: Mapped[list] = mapped_column(JSONB)
    task_type: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    current_step: Mapped[str | None] = mapped_column(String(30))
    report: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    finished_at: Mapped[datetime | None]

    events: Mapped[list["WorkflowEvent"]] = relationship(
        order_by="WorkflowEvent.id", cascade="all, delete-orphan", passive_deletes=True
    )
    approval: Mapped["ApprovalRequest | None"] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )


class WorkflowEvent(Base):
    """Progress log of a run. Saved so a reconnecting browser can replay what it missed."""
    __tablename__ = "workflow_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    step: Mapped[str] = mapped_column(String(30))
    message: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (CheckConstraint("decision IN ('approved', 'rejected')", name="ck_approval_requests_decision"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), unique=True)
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[str | None] = mapped_column(String(10))
    comment: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    decided_at: Mapped[datetime | None]

    run: Mapped[WorkflowRun] = relationship(back_populates="approval")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(50))
    object_type: Mapped[str | None] = mapped_column(String(50))
    object_id: Mapped[str | None] = mapped_column(String(50))
    ip: Mapped[str | None] = mapped_column(String(45))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
