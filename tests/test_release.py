# -*- coding: utf-8 -*-
"""Sprint 3 发布硬化测试：邀请码有效期、权限实时生效、SSE、旧库迁移。

对应本轮技术债清偿：
- TD-02：权限在 before_request 中按 user_id 重新读取，收回权限无需重新登录；
- TD-05：邀请码 7 天有效期，过期注册被拒；
- TD-01：看板 SSE 推送可用；
- 集成点 I-1：Sprint 2 遗留 project.db 的 invite 表缺 expires_at，启动时自动迁移。
"""
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect

from app import create_app
from app.models import Invite, Role, Task, User, db

PWD = "secret123"


@pytest.fixture()
def app():
    application = create_app(testing=True)
    with application.app_context():
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def _login(client, username="admin", password="admin123"):
    return client.post(
        "/auth/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )


def _make_user(app, username, role_name="团队成员"):
    with app.app_context():
        role = Role.query.filter_by(name=role_name).one()
        user = User(username=username, email=username + "@example.com", role_id=role.id)
        user.set_password(PWD)
        db.session.add(user)
        db.session.commit()
        return user.id


def _admin_id(app):
    with app.app_context():
        return User.query.filter_by(username="admin").one().id


def test_invite_has_seven_day_expiry(client, app):
    _login(client)
    client.post("/members/invite", data={"role": "团队成员"}, follow_redirects=True)
    with app.app_context():
        invite = Invite.query.one()
        assert invite.expires_at is not None
        assert invite.days_left == 7
        assert invite.is_usable is True
        assert invite.status_label == "待使用"


def test_expired_invite_cannot_register(client, app):
    admin_id = _admin_id(app)
    with app.app_context():
        role = Role.query.filter_by(name="团队成员").one()
        db.session.add(
            Invite(
                code="EXPIRED1",
                role=role,
                invited_by_id=admin_id,
                status="pending",
                expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
        )
        db.session.commit()
        invite = Invite.query.filter_by(code="EXPIRED1").one()
        assert invite.is_usable is False
        assert invite.status_label == "已过期"

    resp = client.post(
        "/auth/register",
        data={
            "username": "late", "email": "late@example.com",
            "password": PWD, "confirm_password": PWD,
            "invite_code": "EXPIRED1",
        },
        follow_redirects=True,
    )
    assert "可能已过期" in resp.get_data(as_text=True)
    with app.app_context():
        assert User.query.filter_by(username="late").first() is None


def test_expired_invite_is_shown_as_expired_in_members_page(client, app):
    admin_id = _admin_id(app)
    with app.app_context():
        role = Role.query.filter_by(name="团队成员").one()
        db.session.add(
            Invite(
                code="OLDC0DE1", role=role, invited_by_id=admin_id, status="pending",
                expires_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
        )
        db.session.commit()
    _login(client)
    body = client.get("/members").get_data(as_text=True)
    assert "已过期" in body


def test_legacy_invite_without_expiry_still_usable(app):
    """迁移兼容：Sprint 2 的历史邀请没有 expires_at，应视为不过期。"""
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        role = Role.query.filter_by(name="团队成员").one()
        invite = Invite(code="LEGACY01", role=role, invited_by=admin, status="pending")
        db.session.add(invite)
        db.session.commit()
        assert invite.expires_at is None
        assert invite.is_usable is True


def test_permission_revocation_takes_effect_without_relogin(app, client):
    """TD-02 清偿验证：管理员收回 task.assign 后，已登录的项目经理下一请求即 403。"""
    _make_user(app, "pm_live", "项目经理")
    _login(client, "pm_live", PWD)
    resp = client.post("/tasks/create", data={"title": "收回前创建", "priority": "2"},
                       follow_redirects=True)
    assert "已创建" in resp.get_data(as_text=True)

    # 直接在测试夹具的 app context 内改权限（不要再嵌套 app_context，
    # 否则会产生一个新 session，外层请求仍读到旧缓存）
    pm = Role.query.filter_by(name="项目经理").one()
    pm.permissions = []
    db.session.commit()
    # 生产环境每个请求一个 session；测试夹具全程共用一个 session，
    # 这里手动过期缓存，模拟"下一个请求重新查库"的真实场景。
    db.session.expire_all()

    # 不重新登录，直接再发请求：before_request 已把 session 权限刷新为空
    resp = client.post("/tasks/create", data={"title": "收回后创建", "priority": "2"})
    assert resp.status_code == 403
    with app.app_context():
        assert Task.query.filter_by(title="收回后创建").first() is None


def test_removed_member_session_invalidated(app):
    """成员被移除后，其旧会话立即失效，不能继续访问。"""
    _make_user(app, "ghost", "团队成员")
    member_client = app.test_client()
    _login(member_client, "ghost", PWD)
    assert member_client.get("/tasks").status_code == 200

    admin_client = app.test_client()
    _login(admin_client)
    with app.app_context():
        uid = User.query.filter_by(username="ghost").one().id
    admin_client.post("/members/%d/delete" % uid, follow_redirects=True)

    resp = member_client.get("/tasks")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_board_sse_requires_login(client, app):
    resp = client.get("/board/stream")
    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False


def test_board_sse_pushes_first_event(client, app):
    _login(client)
    resp = client.get("/board/stream", buffered=False)
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    first = next(resp.response)
    resp.close()
    assert b"event: stats" in first
    assert b'"ok": true' in first


def test_board_stats_api_still_works(client, app):
    _login(client)
    resp = client.get("/board/api/stats")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_legacy_database_migration_adds_expires_at():
    """集成点 I-1：旧库 invite 表没有 expires_at，启动后应自动补齐。"""
    path = os.path.join(os.getcwd(), "tests", "_legacy_%s.db" % uuid.uuid4().hex[:8])
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE invite ("
        "id INTEGER PRIMARY KEY, code VARCHAR(20), role_id INTEGER, "
        "invited_by_id INTEGER, status VARCHAR(20), created_at DATETIME, "
        "accepted_at DATETIME, accepted_by_id INTEGER)"
    )
    conn.execute(
        "INSERT INTO invite (code, role_id, invited_by_id, status, created_at) "
        "VALUES ('LEGACY99', 1, 1, 'pending', '2026-10-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    application = create_app(database_uri="sqlite:///" + path.replace("\\", "/"))
    try:
        with application.app_context():
            columns = {c["name"] for c in inspect(db.engine).get_columns("invite")}
            assert "expires_at" in columns
            legacy = Invite.query.filter_by(code="LEGACY99").one()
            assert legacy.expires_at is None
            assert legacy.is_usable is True
    finally:
        with application.app_context():
            db.session.remove()
            db.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass
