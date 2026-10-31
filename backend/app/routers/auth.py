from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.schemas import Token, UserOut
from app.security import audit, authenticate, create_access_token, get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=Token)
def login(request: Request, form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """Log in with email (sent as `username`) and password; returns a bearer token."""
    ip = request.client.host if request.client else None
    user = authenticate(db, form.username, form.password)
    if user is None:
        audit(db, None, "auth.login_failed", ip=ip, email=form.username.strip().lower())
        db.commit()
        # Same message whether the email or the password was wrong.
        raise HTTPException(401, "Incorrect email or password", headers={"WWW-Authenticate": "Bearer"})
    audit(db, user.id, "auth.login", "user", user.id, ip=ip)
    db.commit()
    return Token(access_token=create_access_token(user.id))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user
