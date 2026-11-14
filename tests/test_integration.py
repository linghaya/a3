# -*- coding: utf-8 -*-
"""集成与回归测试：验证 Sprint 2 增量与 Sprint 1 基线拼在一起仍然正确。

与 test_sprint1.py / test_sprint2.py 的分工：
- test_sprint1.py         Sprint 1 基线的回归用例（本轮不修改、必须保持全绿）
- test_sprint2.py         单个 Sprint 2 功能点的用例
- test_integration.py     跨模块的端到端业务流程 + 模块之间的数据一致性

运行：python -m pytest tests/test_integration.py -v
"""
import pytest

from app import create_app
from app.models import db, User, Role, Task, Invite

INV_PWD = "secret123"


@pytest.fixture
def app():
    application = create_app(testing=True)
    with application.app_context():
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _login(client, username="admin", password="admin123"):
    return client.post(
        "/auth/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )


def _admin_id(app):
    with app.app_context():
        return User.query.filter_by(username="admin").one().id


# ---------------------------------------------------------------------------
# 端到端主流程：邀请 -> 注册 -> 建任务 -> 改状态 -> 筛选 -> 看板 -> 移除
# ---------------------------------------------------------------------------
def test_end_to_end_member_and_task_lifecycle(client, app):
    """一条完整业务链路，跨 4 个模块（成员/认证/任务/看板）验证集成结果。"""
    # 1. 管理员登录并生成邀请码
    _login(client)
    client.post("/members/invite", data={"role": "团队成员"}, follow_redirects=True)
    with app.app_context():
        code = Invite.query.one().code

    # 2. 新人不带会话，凭邀请码注册并通过邀请加入团队
    client.get("/auth/logout")
    client.post(
        "/auth/register",
        data={
            "username": "alice",
            "email": "alice@example.com",
            "password": INV_PWD,
            "confirm_password": INV_PWD,
            "invite_code": code,
        },
        follow_redirects=True,
    )
    with app.app_context():
        alice = User.query.filter_by(username="alice").one()
        alice_id = alice.id
        assert alice.role.name == "团队成员"

    # 3. 管理员给新人指派一个任务
    _login(client)
    client.post(
        "/tasks/create",
        data={
            "title": "集成测试任务",
            "description": "跨模块链路",
            "priority": "1",
            "due_date": "2026-12-31",
            "assignee_id": str(alice_id),
        },
        follow_redirects=True,
    )
    with app.app_context():
        task = Task.query.filter_by(title="集成测试任务").one()
        task_id = task.id
        assert task.assignee_id == alice_id
        assert task.status == "pending"

    # 4. 新人登录后能在列表里看到任务，并把它改为进行中
    client.get("/auth/logout")
    _login(client, "alice", INV_PWD)
    body = client.get("/tasks").get_data(as_text=True)
    assert "集成测试任务" in body

    client.post(
        "/tasks/%d/status" % task_id,
        data={"status": "in_progress"},
        follow_redirects=True,
    )
    with app.app_context():
        assert db.session.get(Task, task_id).status == "in_progress"

    # 5. 管理员按状态筛选能看到这条任务
    client.get("/auth/logout")
    _login(client)
    body = client.get("/tasks?status=in_progress").get_data(as_text=True)
    assert "集成测试任务" in body

    # 6. 看板与筛选口径一致：进行中列应有 1 条
    stats = client.get("/board/api/stats").get_json()
    counts = {c["key"]: c["count"] for c in stats["columns"]}
    assert counts["in_progress"] == 1
    assert stats["total"] == 1

    # 7. 名下有进行中任务，移除成员被拒
    resp = client.post("/members/%d/delete" % alice_id, follow_redirects=True)
    assert "未完成任务" in resp.get_data(as_text=True)

    # 8. 新人把任务标记完成，之后即可被移除
    client.get("/auth/logout")
    _login(client, "alice", INV_PWD)
    client.post(
        "/tasks/%d/status" % task_id, data={"status": "done"}, follow_redirects=True
    )
    client.get("/auth/logout")
    _login(client)
    resp = client.post("/members/%d/delete" % alice_id, follow_redirects=True)
    assert "已移除" in resp.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(User, alice_id) is None
        assert db.session.get(Task, task_id).assignee_id is None


# ---------------------------------------------------------------------------
# 集成一致性：任务列表筛选 与 看板统计 必须同口径
# ---------------------------------------------------------------------------
def test_task_list_and_board_report_the_same_counts(client, app):
    """同一批任务，列表筛选计数与看板统计接口完全一致。"""
    with app.app_context():
        for i, status in enumerate(["pending", "pending", "in_progress", "done", "done"]):
            db.session.add(Task(title="t%d" % i, priority=2, status=status))
        db.session.commit()

    _login(client)
    stats = client.get("/board/api/stats").get_json()
    board_counts = {c["key"]: c["count"] for c in stats["columns"]}

    for key in ("pending", "in_progress", "done"):
        body = client.get("/tasks?status=%s" % key).get_data(as_text=True)
        assert "当前显示 %d 项" % board_counts[key] in body


# ---------------------------------------------------------------------------
# 权限体系回归：新权限与 Sprint 1 权限体系共存
# ---------------------------------------------------------------------------
def test_member_manage_permission_gates_navigation_and_route(client, app):
    """有 member.manage 才看到"成员管理"入口，也只有他们能访问 /members。"""
    with app.app_context():
        member_role = Role.query.filter_by(name="团队成员").one()
        user = User(email="m@example.com", role_id=member_role.id)
        user.username = "plain"
        user.set_password(INV_PWD)
        db.session.add(user)
        db.session.commit()

    # 管理员：能看到导航入口，能访问页面
    _login(client)
    body = client.get("/").get_data(as_text=True)
    assert 'href="/members">成员管理' in body
    assert client.get("/members").status_code == 200
    client.get("/auth/logout")

    # 团队成员：没有导航入口，直接访问被 403
    _login(client, "plain", INV_PWD)
    body = client.get("/").get_data(as_text=True)
    assert 'href="/members">成员管理' not in body
    assert client.get("/members").status_code == 403


def test_role_permission_edit_still_controls_task_creation(client, app):
    """Sprint 1 的角色—权限联动在 Sprint 2 仍然生效（改权限后立即影响新请求）。"""
    _login(client)
    with app.app_context():
        pm = Role.query.filter_by(name="项目经理").one()
        pm_id = pm.id
        user = User(email="pm@example.com")
        user.username = "pmx"
        user.set_password(INV_PWD)
        user.role_id = pm_id
        db.session.add(user)
        db.session.commit()

    # 项目经理本来可以创建任务
    client.get("/auth/logout")
    _login(client, "pmx", INV_PWD)
    resp = client.post(
        "/tasks/create",
        data={"title": "项目经理建的任务", "priority": "2"},
        follow_redirects=True,
    )
    assert "已创建" in resp.get_data(as_text=True)
    client.get("/auth/logout")

    # 管理员收回项目经理的 task.assign
    _login(client)
    client.post(
        "/roles/%d/edit-permissions" % pm_id,
        data={"permissions": []},
        follow_redirects=True,
    )
    client.get("/auth/logout")

    # 重新登录后（权限在会话中刷新）再创建任务被 403
    _login(client, "pmx", INV_PWD)
    resp = client.post(
        "/tasks/create", data={"title": "应该被拒绝的任务", "priority": "2"}
    )
    assert resp.status_code == 403
    with app.app_context():
        assert Task.query.filter_by(title="应该被拒绝的任务").first() is None


# ---------------------------------------------------------------------------
# Sprint 1 关键行为回归（跨模块拼装后仍成立）
# ---------------------------------------------------------------------------
def test_sprint1_login_failure_message_unchanged(client, app):
    """Sprint 1 的登录失败提示未被 Sprint 2 改动。"""
    resp = client.post(
        "/auth/login",
        data={"username": "admin", "password": "wrong"},
        follow_redirects=True,
    )
    assert "用户名或密码错误" in resp.get_data(as_text=True)


def test_sprint1_duplicate_username_still_rejected(client, app):
    """Sprint 1 的重复用户名校验未被邀请码逻辑破坏。"""
    resp = client.post(
        "/auth/register",
        data={
            "username": "admin",
            "email": "another@example.com",
            "password": INV_PWD,
            "confirm_password": INV_PWD,
            "role": "团队成员",
        },
        follow_redirects=True,
    )
    assert "已被占用" in resp.get_data(as_text=True)


def test_password_reset_and_relogin_still_work(client, app):
    """Sprint 1 的密码重置链路在 Sprint 2 仍可用。"""
    client.post(
        "/auth/reset-password",
        data={
            "username": "admin",
            "old_password": "admin123",
            "new_password": "newpass123",
            "confirm_password": "newpass123",
        },
        follow_redirects=True,
    )
    client.get("/auth/logout")
    resp = _login(client, "admin", "newpass123")
    assert "欢迎回来" in resp.get_data(as_text=True)
