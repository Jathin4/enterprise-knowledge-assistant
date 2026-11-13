from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AuditEvent, User
from app.schemas import Token, UserOut
from app.security import audit, authenticate, create_access_token, get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Brute-force protection, counted from the audit log (no extra state, survives restarts).
WINDOW = timedelta(minutes=15)
MAX_FAILS_PER_EMAIL = 5     # guessing one account's password
MAX_FAILS_PER_IP = 20       # trying many accounts from one machine


def too_many_failures(db: Session, email: str, ip: str | None) -> bool:
    since = func.now() - WINDOW      # the database's clock, same as the stored timestamps
    recent = (select(AuditEvent).where(AuditEvent.action == "auth.login_failed", AuditEvent.created_at >= since))
    by_email = db.scalar(select(func.count()).select_from(
        recent.where(AuditEvent.details["email"].astext == email).subquery()))
    by_ip = db.scalar(select(func.count()).select_from(recent.where(AuditEvent.ip == ip).subquery())) if ip else 0
    return by_email >= MAX_FAILS_PER_EMAIL or by_ip >= MAX_FAILS_PER_IP


@router.post("/login", response_model=Token)
def login(request: Request, form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """Log in with email (sent as `username`) and password; returns a bearer token."""
    ip = request.client.host if request.client else None
    email = form.username.strip().lower()
    if too_many_failures(db, email, ip):   # checked first: a blocked attempt doesn't even test the password
        audit(db, None, "auth.login_blocked", ip=ip, email=email)
        db.commit()
        raise HTTPException(429, "Too many failed logins. Try again in 15 minutes.", headers={"Retry-After": "900"})
    user = authenticate(db, form.username, form.password)
    if user is None:
        audit(db, None, "auth.login_failed", ip=ip, email=email)
        db.commit()
        # Same message whether the email or the password was wrong.
        raise HTTPException(401, "Incorrect email or password", headers={"WWW-Authenticate": "Bearer"})
    audit(db, user.id, "auth.login", "user", user.id, ip=ip)
    db.commit()
    return Token(access_token=create_access_token(user.id))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user
