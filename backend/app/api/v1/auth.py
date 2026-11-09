"""
Sign-in (app.services.auth): the first admin, signing in and out, one's own
password, and, for admins, the users and the activity log.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import _token, require_user
from app.api.v1.errors import conflict, not_found
from app.api.v1.schemas import (
    ActivityOut,
    AuthState,
    LoginIn,
    PasswordChange,
    SetupIn,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.db.models import Activity, User, UserRole
from app.db.session import get_db
from app.services import auth

router = APIRouter(tags=["auth"])

throttle = auth.LoginThrottle()


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _sign_in(db: Session, user: User, request: Request, response: Response) -> AuthState:
    token = auth.start_session(db, user, _ip(request))
    response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_HOURS * 3600, httponly=True,
                        samesite="strict", secure=_secure(request), path="/")
    return AuthState(setup_required=False, user=UserOut.model_validate(user), token=token)


@router.get("/auth/state", response_model=AuthState)
def state(request: Request, db: Session = Depends(get_db)):
    """Who is signed in, or whether the first admin still has to be created."""
    if auth.user_count(db) == 0:
        auth.setup_code(db)          # make sure there is one to type
        return AuthState(setup_required=True, user=None)
    user = auth.user_for(db, _token(request))
    return AuthState(setup_required=False, user=UserOut.model_validate(user) if user else None)


@router.post("/auth/setup", response_model=AuthState)
def setup(body: SetupIn, request: Request, response: Response, db: Session = Depends(get_db)):
    """Create the first admin with the setup code from the server (only while there is no user)."""
    if auth.user_count(db) > 0:
        conflict("The first admin already exists; sign in")
    ip = _ip(request)
    if throttle.blocked(ip):
        raise HTTPException(429, detail={"error": "Too many attempts; wait 5 minutes"})
    code = auth.setup_code(db)
    if code is None or body.setup_code.strip().lower() != code:
        throttle.failed(ip)
        raise HTTPException(403, detail={"error": "Wrong setup code (see the installer output or the server log)"})
    user = User(username=body.username, password_hash=auth.hash_password(body.password), role=UserRole.ADMIN)
    db.add(user)
    db.commit()
    auth.setup_code(db)              # removes the code
    auth.record(db, user, "POST", "/api/v1/auth/setup", 200, ip)
    return _sign_in(db, user, request, response)


@router.post("/auth/login", response_model=AuthState)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = _ip(request)
    if throttle.blocked(ip):
        raise HTTPException(429, detail={"error": "Too many wrong passwords; wait 5 minutes"})
    user = db.scalar(select(User).where(User.username == body.username.strip().lower()))
    if user is None or user.disabled or not auth.verify_password(body.password, user.password_hash):
        throttle.failed(ip)
        auth.record(db, None, "POST", f"/api/v1/auth/login ({body.username[:32]})", 401, ip)
        raise HTTPException(401, detail={"error": "Wrong username or password"})
    throttle.succeeded(ip)
    return _sign_in(db, user, request, response)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, db: Session = Depends(get_db)):
    auth.end_session(db, _token(request))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(auth.COOKIE, path="/")
    return response


@router.post("/auth/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(body: PasswordChange, db: Session = Depends(get_db),
                    user: User = Depends(require_user)):
    """Change one's own password; every session of the user ends, so sign in again."""
    if not auth.verify_password(body.current_password, user.password_hash):
        raise HTTPException(403, detail={"error": "The current password is wrong"})
    user.password_hash = auth.hash_password(body.new_password)
    auth.end_sessions_of(db, user.id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Users (admin) ─────────────────────────────────────────────────────

users = APIRouter(prefix="/users", tags=["auth"], dependencies=[Depends(require_user)])


@users.get("", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)):
    return db.scalars(select(User).order_by(User.username)).all()


@users.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.username == body.username)):
        conflict(f"User '{body.username}' already exists")
    user = User(username=body.username, password_hash=auth.hash_password(body.password), role=body.role)
    db.add(user)
    db.commit()
    return user


def _user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        not_found("User", user_id)
    return user


def _keeps_an_admin(db: Session, user: User) -> None:
    if user.role is UserRole.ADMIN and not user.disabled and auth.admin_count(db) <= 1:
        conflict("This is the last admin; make another admin first")


@users.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: UserUpdate, db: Session = Depends(get_db)):
    """Role, a new password (the user is signed out) or disabling the account."""
    user = _user(db, user_id)
    changes = body.model_dump(exclude_unset=True)
    demoted = changes.get("role", user.role) is not UserRole.ADMIN or changes.get("disabled", False)
    if demoted:
        _keeps_an_admin(db, user)
    if "role" in changes:
        user.role = changes["role"]
    if "disabled" in changes:
        user.disabled = changes["disabled"]
    if changes.get("password"):
        user.password_hash = auth.hash_password(changes["password"])
    if changes.get("password") or changes.get("disabled"):
        auth.end_sessions_of(db, user.id)
    db.commit()
    return user


@users.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, me: User = Depends(require_user), db: Session = Depends(get_db)):
    user = _user(db, user_id)
    if user.id == me.id:
        conflict("You cannot delete yourself")
    _keeps_an_admin(db, user)
    db.delete(user)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


activity = APIRouter(prefix="/activity", tags=["auth"], dependencies=[Depends(require_user)])


@activity.get("", response_model=list[ActivityOut])
def list_activity(limit: int = 200, db: Session = Depends(get_db)):
    """The newest entries of the activity log (admin)."""
    limit = max(1, min(limit, 1000))
    return db.scalars(select(Activity).order_by(Activity.id.desc()).limit(limit)).all()


@activity.get("/export.csv")
def export_activity(limit: int = 10000, db: Session = Depends(get_db)):
    """The activity log as CSV (newest first), for a spreadsheet or the records."""
    import csv
    import io

    limit = max(1, min(limit, 100_000))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["time_utc", "user", "action", "status", "ip_address"])
    for a in db.scalars(select(Activity).order_by(Activity.id.desc()).limit(limit)):
        # A cell starting with = + - @ is a formula in a spreadsheet: keep it text.
        action = "'" + a.action if a.action[:1] in "=+-@" else a.action
        w.writerow([a.at.astimezone(timezone.utc).isoformat(timespec="seconds"), a.username or "",
                    action, a.status_code, a.ip_address or ""])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                         headers={"Content-Disposition": f'attachment; filename="ggnet-activity-{stamp}.csv"'})


def _secure(request: Request) -> bool:
    """The session cookie never goes over HTTP once HTTPS is set up (app.serve)."""
    from app import serve

    return request.url.scheme == "https" or serve.tls() is not None

