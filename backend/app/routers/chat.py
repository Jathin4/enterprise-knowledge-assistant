import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db as dbmod
from app.answering import Answer, answer_question, answer_stream
from app.db import get_db
from app.models import Conversation, Message, User
from app.schemas import ChatRequest, ChatResponse, ConversationDetail, ConversationOut, MessageOut
from app.security import get_current_user

router = APIRouter(prefix="/api", tags=["chat"])
log = logging.getLogger(__name__)


def own_conversation(db: Session, user: User, conversation_id: int) -> Conversation:
    conv = db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user.id:  # 404 either way: no peeking at others' chats
        raise HTTPException(404, "Conversation not found")
    return conv


def start_turn(db: Session, user: User, req: ChatRequest) -> Conversation:
    """Find or create the conversation and save the user's question."""
    if req.conversation_id is None:
        conv = Conversation(user_id=user.id, title=req.question.strip()[:200])
        db.add(conv)
        db.flush()
    else:
        conv = own_conversation(db, user, req.conversation_id)
    db.add(Message(conversation_id=conv.id, role="user", content=req.question.strip()))
    return conv


def save_answer(db: Session, conv: Conversation, answer: Answer) -> Message:
    msg = Message(conversation_id=conv.id, role="assistant", content=answer.text, sources=answer.sources,
                  abstained=answer.abstained, latency_ms=answer.retrieval_ms + answer.generation_ms)
    db.add(msg)
    db.commit()
    return msg


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Ask a question; returns the whole cited answer at once (see /api/chat/stream for word-by-word)."""
    conv = start_turn(db, user, req)
    answer = answer_question(db, user, req.question.strip(), req.collection_id)
    return ChatResponse(conversation_id=conv.id, message=save_answer(db, conv, answer))


def sse(data: dict) -> str:
    """One Server-Sent Event: a 'data:' line followed by a blank line."""
    return f"data: {json.dumps(data, default=str)}\n\n"


def stream_events(user_id: int, req: ChatRequest):
    # Own session: the request's session may be closed before a long stream finishes.
    with dbmod.SessionLocal() as db:
        try:
            user = db.get(User, user_id)
            conv = start_turn(db, user, req)
            db.commit()
            yield sse({"type": "start", "conversation_id": conv.id})
            for event in answer_stream(db, user, req.question.strip(), req.collection_id):
                if event["type"] == "answer":
                    msg = save_answer(db, conv, event["answer"])
                    yield sse({"type": "done", "message": MessageOut.model_validate(msg).model_dump(mode="json")})
                else:
                    yield sse(event)
        except Exception:
            log.exception("Chat stream failed")
            db.rollback()
            yield sse({"type": "error", "detail": "The answer could not be generated. Is Ollama running?"})


@router.post("/chat/stream")
def chat_stream(req: ChatRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Ask a question; the answer arrives word by word as Server-Sent Events:
    start → sources → token… → done (the checked answer with its citations) or error."""
    if req.conversation_id is not None:
        own_conversation(db, user, req.conversation_id)  # clean 404 before streaming starts
    return StreamingResponse(stream_events(user.id, req), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.scalars(select(Conversation).where(Conversation.user_id == user.id)
                      .order_by(Conversation.id.desc())).all()


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return own_conversation(db, user, conversation_id)


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.delete(own_conversation(db, user, conversation_id))
    db.commit()
    return Response(status_code=204)
