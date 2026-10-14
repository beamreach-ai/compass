from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt as pyjwt
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

router = APIRouter()

_COOKIE_NAME = "compass_session"
_SESSION_HOURS = 8


def _credentials() -> tuple[str, str]:
    return (
        os.environ.get("APP_USERNAME", "").strip(),
        os.environ.get("APP_PASSWORD", "").strip(),
    )


def _auth_enabled() -> bool:
    username, password = _credentials()
    return bool(username and password)


def _make_token(username: str, password: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=_SESSION_HOURS)
    return pyjwt.encode({"sub": username, "exp": exp}, password, algorithm="HS256")


def validate_session(token: Optional[str]) -> bool:
    if not token:
        return False
    _, password = _credentials()
    if not password:
        return False
    try:
        pyjwt.decode(token, password, algorithms=["HS256"])
        return True
    except pyjwt.PyJWTError:
        return False


class LoginPayload(BaseModel):
    username: str
    password: str


@router.post("/api/auth/login")
def login(payload: LoginPayload, response: Response):
    username, password = _credentials()
    if not _auth_enabled():
        return {"ok": True, "auth_enabled": False}
    if payload.username != username or payload.password != password:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = _make_token(username, password)
    response.set_cookie(
        _COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=_SESSION_HOURS * 3600,
        secure=False,
    )
    return {"ok": True, "auth_enabled": True}


@router.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(_COOKIE_NAME)
    return {"ok": True}


@router.get("/api/auth/me")
def me(request: Request):
    principal = getattr(request.state, "api_principal", None)
    if principal is not None:
        return {
            "authenticated": True,
            "auth_enabled": _auth_enabled(),
            "method": "api_token",
            **principal.to_public(),
        }
    if not _auth_enabled():
        return {"authenticated": True, "auth_enabled": False}
    token = request.cookies.get(_COOKIE_NAME)
    authenticated = validate_session(token)
    if not authenticated:
        raise HTTPException(status_code=401, detail="Not authenticated")
    username, _ = _credentials()
    return {"authenticated": True, "auth_enabled": True, "username": username}
