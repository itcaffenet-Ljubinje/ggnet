"""Signing in: the first admin, sessions, roles, users and the activity log."""

from __future__ import annotations

import pytest

from app.api.v1 import auth as auth_api
from app.services import auth

ADMIN = {"username": "owner", "password": "cafe-owner-1"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """The setup code in a temporary data dir, and a fresh wrong-password counter."""
    monkeypatch.setattr(auth, "setup_code_path", lambda cfg=None: tmp_path / "setup-code")
    monkeypatch.setattr(auth_api, "throttle", auth.LoginThrottle())


def _code(tmp_path) -> str:
    return (tmp_path / "setup-code").read_text().strip()


def _setup(c, tmp_path) -> dict:
    assert c.get("/api/v1/auth/state").json()["setup_required"] is True
    r = c.post("/api/v1/auth/setup", json={**ADMIN, "setup_code": _code(tmp_path)})
    assert r.status_code == 200, r.text
    return r.json()


def _login(c, username: str, password: str) -> None:
    r = c.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def test_passwords_are_hashed_with_a_salt():
    h = auth.hash_password("secret-pass")
    assert h.startswith("scrypt$") and "secret-pass" not in h
    assert h != auth.hash_password("secret-pass")
    assert auth.verify_password("secret-pass", h)
    assert not auth.verify_password("secret-pasS", h)
    assert not auth.verify_password("x", "not-a-hash")


def test_the_ui_api_needs_sign_in_but_the_pcs_do_not(anon_client):
    assert anon_client.get("/api/v1/machines").status_code == 401
    assert anon_client.post("/api/v1/images", json={"name": "x", "size_gb": 1}).status_code == 401
    assert anon_client.get("/api/v1/settings/boot").status_code == 401
    assert anon_client.get("/api/v1/users").status_code == 401
    # What the PCs and the sign-in screen use stays open.
    assert anon_client.get("/api/health").status_code == 200
    assert anon_client.get("/api/v1/boot/ipxe", params={"mac": "aa-bb-cc-dd-ee-01"}).status_code == 200
    assert anon_client.get("/api/v1/auth/state").status_code == 200
    r = anon_client.post("/api/v1/agent/heartbeat", json={"name": "pc01", "agent_version": "0.1.13"})
    assert r.status_code == 404          # unknown PC, but not refused for want of a sign-in


def test_the_first_admin_needs_the_setup_code(anon_client, tmp_path):
    state = anon_client.get("/api/v1/auth/state").json()
    assert state == {"setup_required": True, "user": None, "token": None}
    code = _code(tmp_path)
    assert len(code) == 14                                       # 3f9a-0c71-b2e4
    r = anon_client.post("/api/v1/auth/setup", json={**ADMIN, "setup_code": "0000-0000-0000"})
    assert r.status_code == 403
    r = anon_client.post("/api/v1/auth/setup", json={**ADMIN, "setup_code": code.upper()})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["role"] == "admin"
    assert not (tmp_path / "setup-code").exists()                # used up
    assert anon_client.get("/api/v1/machines").status_code == 200   # signed in by the cookie
    r = anon_client.post("/api/v1/auth/setup", json={**ADMIN, "setup_code": code})
    assert r.status_code == 409


@pytest.mark.parametrize("body", [
    {"username": "A", "password": "long-enough-1"},          # too short, upper case
    {"username": "owner", "password": "short"},
    {"username": "has space", "password": "long-enough-1"},
])
def test_bad_usernames_and_passwords_are_refused(anon_client, tmp_path, body):
    anon_client.get("/api/v1/auth/state")
    r = anon_client.post("/api/v1/auth/setup", json={**body, "setup_code": _code(tmp_path)})
    assert r.status_code == 422


def test_sign_in_out_and_bearer_tokens(anon_client, tmp_path):
    _setup(anon_client, tmp_path)
    anon_client.post("/api/v1/auth/logout")
    assert anon_client.get("/api/v1/machines").status_code == 401
    r = anon_client.post("/api/v1/auth/login", json={"username": "OWNER ", "password": ADMIN["password"]})
    assert r.status_code == 200
    token = r.json()["token"]
    assert anon_client.get("/api/v1/auth/state").json()["user"]["username"] == "owner"
    anon_client.cookies.clear()
    assert anon_client.get("/api/v1/machines").status_code == 401
    assert anon_client.get("/api/v1/machines", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_too_many_wrong_passwords_are_blocked(anon_client, tmp_path):
    _setup(anon_client, tmp_path)
    for _ in range(5):
        r = anon_client.post("/api/v1/auth/login", json={"username": "owner", "password": "wrong-pass"})
        assert r.status_code == 401
    r = anon_client.post("/api/v1/auth/login", json=ADMIN)
    assert r.status_code == 429                                  # even the right one, for a while


def test_staff_may_only_power_pcs(anon_client, tmp_path):
    _setup(anon_client, tmp_path)
    r = anon_client.post("/api/v1/users", json={"username": "ana", "password": "staff-pass-1"})
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "staff"
    m = anon_client.post("/api/v1/machines", json={"name": "pc01"}).json()
    anon_client.post("/api/v1/auth/logout")
    _login(anon_client, "ana", "staff-pass-1")

    assert anon_client.get("/api/v1/machines").status_code == 200
    assert anon_client.get("/api/v1/images").status_code == 200
    # Changing things is the admin's.
    assert anon_client.post("/api/v1/machines", json={"name": "pc02"}).status_code == 403
    assert anon_client.delete(f"/api/v1/machines/{m['id']}").status_code == 403
    assert anon_client.put("/api/v1/settings/boot", json={}).status_code == 403
    assert anon_client.get("/api/v1/users").status_code == 403
    assert anon_client.get("/api/v1/activity").status_code == 403
    r = anon_client.post("/api/v1/machines/bulk", json={"machine_ids": [m["id"]], "action": "assign",
                                                       "game_image_id": None})
    assert r.status_code == 403
    # Power is theirs (pc01 has no MAC, so turning it on fails, but not for want of rights).
    r = anon_client.post(f"/api/v1/machines/{m['id']}/power", json={"action": "on"})
    assert r.status_code not in (401, 403), r.text
    r = anon_client.post("/api/v1/machines/bulk", json={"machine_ids": [m["id"]], "action": "reboot"})
    assert r.status_code == 200, r.text


def test_admins_manage_users_but_never_lose_the_last_admin(anon_client, tmp_path):
    me = _setup(anon_client, tmp_path)["user"]
    assert anon_client.post("/api/v1/users", json={"username": "owner", "password": "x" * 8}).status_code == 409
    assert anon_client.delete(f"/api/v1/users/{me['id']}").status_code == 409        # not yourself
    assert anon_client.patch(f"/api/v1/users/{me['id']}", json={"role": "staff"}).status_code == 409
    assert anon_client.patch(f"/api/v1/users/{me['id']}", json={"disabled": True}).status_code == 409
    other = anon_client.post("/api/v1/users", json={"username": "boss", "password": "boss-pass-1",
                                                   "role": "admin"}).json()
    assert anon_client.patch(f"/api/v1/users/{me['id']}", json={"role": "staff"}).status_code == 200
    assert anon_client.get("/api/v1/users").status_code == 403                      # now staff
    _login(anon_client, "boss", "boss-pass-1")
    assert [u["username"] for u in anon_client.get("/api/v1/users").json()] == ["boss", "owner"]
    assert anon_client.delete(f"/api/v1/users/{me['id']}").status_code == 204
    assert anon_client.delete(f"/api/v1/users/{other['id']}").status_code == 409     # yourself


def test_disabling_or_a_new_password_signs_the_user_out(anon_client, tmp_path):
    _setup(anon_client, tmp_path)
    ana = anon_client.post("/api/v1/users", json={"username": "ana", "password": "staff-pass-1"}).json()
    admin_cookie = dict(anon_client.cookies)
    _login(anon_client, "ana", "staff-pass-1")
    ana_token = anon_client.cookies.get(auth.COOKIE)
    anon_client.cookies.clear()
    anon_client.cookies.update(admin_cookie)
    assert anon_client.patch(f"/api/v1/users/{ana['id']}", json={"password": "new-pass-12"}).status_code == 200
    r = anon_client.get("/api/v1/machines", headers={"Authorization": f"Bearer {ana_token}"})
    assert r.status_code == 401
    anon_client.patch(f"/api/v1/users/{ana['id']}", json={"disabled": True})
    r = anon_client.post("/api/v1/auth/login", json={"username": "ana", "password": "new-pass-12"})
    assert r.status_code == 401


def test_changing_ones_own_password(anon_client, tmp_path):
    _setup(anon_client, tmp_path)
    r = anon_client.post("/api/v1/auth/password", json={"current_password": "wrong-one",
                                                      "new_password": "brand-new-1"})
    assert r.status_code == 403
    r = anon_client.post("/api/v1/auth/password", json={"current_password": ADMIN["password"],
                                                      "new_password": "brand-new-1"})
    assert r.status_code == 204
    assert anon_client.get("/api/v1/machines").status_code == 401        # sign in again
    _login(anon_client, "owner", "brand-new-1")


def test_changes_are_in_the_activity_log(anon_client, tmp_path):
    _setup(anon_client, tmp_path)
    anon_client.post("/api/v1/machines", json={"name": "pc01"})
    anon_client.post("/api/v1/machines", json={"name": "pc01"})          # duplicate: 409
    anon_client.get("/api/v1/machines")                                  # reads are not logged
    anon_client.post("/api/v1/auth/login", json={"username": "owner", "password": "bad-guess"})
    log = anon_client.get("/api/v1/activity").json()
    got = [(a["username"], a["action"], a["status_code"]) for a in log]
    assert got[:3] == [
        (None, "POST /api/v1/auth/login (owner)", 401),
        ("owner", "POST /api/v1/machines", 409),
        ("owner", "POST /api/v1/machines", 200),
    ]
    assert got[-1] == ("owner", "POST /api/v1/auth/setup", 200)
    # A failed change leaves only its log entry behind.
    assert len(anon_client.get("/api/v1/machines").json()) == 1


def test_the_server_shell_can_print_the_code_and_rescue_an_admin(db_url, tmp_path, monkeypatch, capsys):
    from sqlalchemy.orm import sessionmaker

    from app import cli
    from app.db.session import make_engine

    engine = make_engine(db_url)
    monkeypatch.setattr(cli, "get_sessionmaker", lambda: sessionmaker(bind=engine, expire_on_commit=False))
    passwords = iter(["rescue-pass-1", "rescue-pass-1", "other-pass-12", "other-pass-12"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(passwords))

    assert cli.main(["setup-code"]) == 0
    assert capsys.readouterr().out.strip() == _code(tmp_path)
    assert cli.main(["create-admin", "Rescue"]) == 0
    assert not (tmp_path / "setup-code").exists()
    assert cli.main(["setup-code"]) == 1
    assert cli.main(["reset-password", "rescue"]) == 0
    with pytest.raises(SystemExit):
        cli.main(["reset-password", "nobody"])
    engine.dispose()
