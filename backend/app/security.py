"""Authentication (who are you?), authorisation (may you do this?) and the audit log."""
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import AuditEvent, Collection, CollectionMember, Document, User

password_hasher = PasswordHash.recommended()  # Argon2
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
# Checked when the email doesn't exist, so a failed login takes the same time either way
# and attackers can't discover which emails are registered.
_DUMMY_HASH = password_hasher.hash("not-a-real-password")


# ---------- Authentication ----------

def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        password_hasher.verify(password, _DUMMY_HASH)
        return None
    return user if password_hasher.verify(password, user.password_hash) else None


def create_access_token(user_id: int) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_minutes)
    return jwt.encode({"sub": str(user_id), "exp": expires}, settings.secret_key, algorithm="HS256")


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    unauthorized = HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    try:
        user_id = int(jwt.decode(token, settings.secret_key, algorithms=["HS256"])["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise unauthorized
    user = db.get(User, user_id)
    if user is None:
        raise unauthorized
    return user


# ---------- Authorisation ----------
# Rule: admins see every collection; everyone else sees only collections they are a member of.
# Every document check goes through these functions, never through the client or the LLM.

def readable_collection_ids(db: Session, user: User) -> list[int]:
    if user.role == "admin":
        return list(db.scalars(select(Collection.id)))
    return list(db.scalars(select(CollectionMember.collection_id).where(CollectionMember.user_id == user.id)))


def get_document_for_user(db: Session, user: User, doc_id: int) -> Document:
    doc = db.get(Document, doc_id)
    # 404 (not 403) so users can't even learn that a forbidden document exists.
    if doc is None or doc.collection_id not in readable_collection_ids(db, user):
        raise HTTPException(404, "Document not found")
    return doc


def can_delete(user: User, doc: Document) -> bool:
    return user.role == "admin" or doc.owner_id == user.id


# ---------- Audit log ----------

def audit(db: Session, actor_id: int | None, action: str, object_type: str | None = None,
          object_id: object = None, ip: str | None = None, **details) -> None:
    """Add an audit row to the current transaction; the caller commits it with the action."""
    db.add(AuditEvent(actor_id=actor_id, action=action, object_type=object_type,
                      object_id=None if object_id is None else str(object_id), ip=ip, details=details))
