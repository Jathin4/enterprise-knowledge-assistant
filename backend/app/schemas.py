"""Shapes of API responses. Kept separate from models so secrets (password_hash,
storage_path) can never leak into a response by accident."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    role: str


class CollectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    collection_id: int
    owner_id: int
    filename: str
    file_type: str
    size_bytes: int
    status: str
    error: str | None
    page_count: int | None
    chunk_count: int
    created_at: datetime
    updated_at: datetime


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: int | None = None   # omit to start a new conversation
    collection_id: int | None = None     # omit to search all of your collections


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    role: str
    content: str
    sources: list[dict]
    abstained: bool
    latency_ms: int | None
    created_at: datetime


class ChatResponse(BaseModel):
    conversation_id: int
    message: MessageOut


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    created_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]
