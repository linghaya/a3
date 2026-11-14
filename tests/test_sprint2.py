# -*- coding: utf-8 -*-
"""Sprint 2 新增功能的测试用例。

覆盖范围（对应 Sprint 2 的 5 个已交付故事）：
- US-08 邀请成员加入项目：邀请码生成、凭码注册、一次性校验、撤销
- US-09 查看并管理成员列表：权限控制、调整角色、移除成员及其保护规则
- US-10 更新任务状态：负责人本人 / 有 task.assign 权限者、非法状态、越权
- US-11 按状态筛选查看任务：合法/非法参数、各状态计数
- US-13 查看项目整体看板：看板页三列、统计接口 JSON、未登录行为

运行：python -m pytest tests/test_sprint2.py -v
"""
import pytest

from app import create_app
from app.models import db, User, Role, Task, Invite

INV_PWD = "secret123"


@pytest.fixture
def app():
    """每个用例一个全新的内存数据库。"""
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


def _make_member(app, username="member1", role_name="团队成员", password=INV_PWD):
    """直接写库创建一个成员（用于绕过注册页，专注被测逻辑）。"""
    with app.app_context():
        role = Role.query.filter_by(name=role_name).first()
        user = User(username=username, email=username + "@example.com", role_id=role.id)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user.id


def _make_task(app, assignee_id=None, status="pending", title="任务A"):
    with app.app_context():
        task = Task(title=title, priority=2, status=status, assignee_id=assignee_id)
        db.session.add(task)
        db.session.commit()
        return task.id


# ---------------------------------------------------------------- US-08 邀请
def test_admin_can_generate_invite_code(client, app):
    """管理员生成邀请码后，邀请记录出现在成员页，状态为待使用。"""
    _login(client)
    resp = client.post(
        "/members/invite", data={"role": "项目经理"}, follow_redirects=True
    )
    assert resp.status_code == 200
    assert "邀请码已生成" in resp.get_data(as_text=True)

    with app.app_context():
        invite = Invite.query.one()
        assert invite.status == "pending"
        assert invite.role.name == "项目经理"
        assert len(invite.code) == 8


def test_register_with_invite_code_joins_team_with_preset_role(client, app):
    """凭邀请码注册：角色取邀请码预设角色，邀请码变为已使用。"""
    _login(client)
    client.post("/members/invite", data={"role": "项目经理"}, follow_redirects=True)
    with app.app_context():
        code = Invite.query.one().code

    client.get("/auth/logout")
    resp = client.post(
        "/auth/register",
        data={
            "username": "newbie",
            "email": "newbie@example.com",
            "password": INV_PWD,
            "confirm_password": INV_PWD,
            "invite_code": code.lower(),  # 大小写不敏感
        },
        follow_redirects=True,
    )
    assert "注册成功" in resp.get_data(as_text=True)

    with app.app_context():
        user = User.query.filter_by(username="newbie").one()
        assert user.role.name == "项目经理"  # 以邀请码为准
        invite = Invite.query.one()
        assert invite.status == "accepted"
        assert invite.accepted_by_id == user.id


def test_invite_code_is_single_use(client, app):
    """同一个邀请码不能用第二次。"""
    _login(client)
    client.post("/members/invite", data={"role": "团队成员"}, follow_redirects=True)
    with app.app_context():
        code = Invite.query.one().code
    client.get("/auth/logout")

    payload = {
        "username": "u1",
        "email": "u1@example.com",
        "password": INV_PWD,
        "confirm_password": INV_PWD,
        "invite_code": code,
    }
    client.post("/auth/register", data=payload, follow_redirects=True)
    payload.update(username="u2", email="u2@example.com")
    resp = client.post("/auth/register", data=payload, follow_redirects=True)
    assert "邀请码无效或已被使用" in resp.get_data(as_text=True)

    with app.app_context():
        assert User.query.filter_by(username="u2").first() is None


def test_invalid_invite_code_is_rejected(client, app):
    """不存在的邀请码注册失败，且不创建用户。"""
    resp = client.post(
        "/auth/register",
        data={
            "username": "ghost",
            "email": "ghost@example.com",
            "password": INV_PWD,
            "confirm_password": INV_PWD,
            "invite_code": "DEADBEEF",
        },
        follow_redirects=True,
    )
    assert "邀请码无效或已被使用" in resp.get_data(as_text=True)
    with app.app_context():
        assert User.query.filter_by(username="ghost").first() is None


def test_revoked_invite_code_cannot_be_used(client, app):
    """撤销后的邀请码不可用于注册。"""
    _login(client)
    client.post("/members/invite", data={"role": "团队成员"}, follow_redirects=True)
    with app.app_context():
        invite_id = Invite.query.one().id
        code = Invite.query.one().code
    client.post("/members/invites/%d/revoke" % invite_id, follow_redirects=True)
    client.get("/auth/logout")

    resp = client.post(
        "/auth/register",
        data={
            "username": "late",
            "email": "late@example.com",
            "password": INV_PWD,
            "confirm_password": INV_PWD,
            "invite_code": code,
        },
        follow_redirects=True,
    )
    assert "邀请码无效或已被使用" in resp.get_data(as_text=True)


def test_register_without_code_keeps_sprint1_behaviour(client, app):
    """不带邀请码的注册仍按 Sprint 1 逻辑走表单里的角色。"""
    resp = client.post(
        "/auth/register",
        data={
            "username": "plain",
            "email": "plain@example.com",
            "password": INV_PWD,
            "confirm_password": INV_PWD,
            "role": "团队成员",
        },
        follow_redirects=True,
    )
    assert "注册成功" in resp.get_data(as_text=True)
    with app.app_context():
        assert User.query.filter_by(username="plain").one().role.name == "团队成员"


# ---------------------------------------------------------------- US-09 成员管理
def test_member_page_requires_permission(client, app):
    """无 member.manage 权限的成员访问成员页被拦截（403）。"""
    _make_member(app, "plainman", "团队成员")
    _login(client, "plainman", INV_PWD)
    resp = client.get("/members")
    assert resp.status_code == 403


def test_admin_can_change_member_role(client, app):
    """管理员可以把成员从团队成员调整为项目经理。"""
    uid = _make_member(app, "promote_me", "团队成员")
    _login(client)
    resp = client.post(
        "/members/%d/role" % uid, data={"role": "项目经理"}, follow_redirects=True
    )
    assert "角色已由" in resp.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(User, uid).role.name == "项目经理"


def test_cannot_change_own_role(client, app):
    """管理员不能修改自己的角色，避免误操作把自己降权。"""
    _login(client)
    with app.app_context():
        admin_id = User.query.filter_by(username="admin").one().id
    resp = client.post(
        "/members/%d/role" % admin_id, data={"role": "团队成员"}, follow_redirects=True
    )
    assert "不能修改自己的角色" in resp.get_data(as_text=True)
    with app.app_context():
        assert User.query.filter_by(username="admin").one().role.name == "管理员"


def test_remove_member_reassigns_completed_tasks(client, app):
    """移除成员成功：其已完成任务的负责人被置空，账号删除。"""
    uid = _make_member(app, "leaver", "团队成员")
    _make_task(app, assignee_id=uid, status="done", title="已完成的任务")
    _login(client)
    resp = client.post("/members/%d/delete" % uid, follow_redirects=True)
    assert "已移除" in resp.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(User, uid) is None
        task = Task.query.filter_by(title="已完成的任务").one()
        assert task.assignee_id is None


def test_cannot_remove_member_with_open_tasks(client, app):
    """成员名下还有未完成任务时，拒绝移除（提示先转派或完成）。"""
    uid = _make_member(app, "busy", "团队成员")
    _make_task(app, assignee_id=uid, status="in_progress", title="进行中的任务")
    _login(client)
    resp = client.post("/members/%d/delete" % uid, follow_redirects=True)
    assert "未完成任务" in resp.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(User, uid) is not None


# ---------------------------------------------------------------- US-10 状态更新
def test_assignee_can_update_own_task_status(client, app):
    """任务负责人可以把自己任务的状态改为进行中。"""
    uid = _make_member(app, "worker", "团队成员")
    tid = _make_task(app, assignee_id=uid)
    _login(client, "worker", INV_PWD)
    resp = client.post(
        "/tasks/%d/status" % tid, data={"status": "in_progress"}, follow_redirects=True
    )
    assert "状态已由" in resp.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Task, tid).status == "in_progress"


def test_other_member_cannot_update_task_status(client, app):
    """既不是负责人、也没有 task.assign 权限的成员不能改状态。"""
    owner = _make_member(app, "owner1", "团队成员")
    _make_member(app, "other1", "团队成员")
    tid = _make_task(app, assignee_id=owner)
    _login(client, "other1", INV_PWD)
    resp = client.post(
        "/tasks/%d/status" % tid, data={"status": "done"}, follow_redirects=False
    )
    assert resp.status_code == 403
    with app.app_context():
        assert db.session.get(Task, tid).status == "pending"


def test_manager_can_update_any_task_status(client, app):
    """拥有 task.assign 权限的项目经理可以更新他人任务。"""
    owner = _make_member(app, "owner2", "团队成员")
    _make_member(app, "pm1", "项目经理")
    tid = _make_task(app, assignee_id=owner)
    _login(client, "pm1", INV_PWD)
    client.post("/tasks/%d/status" % tid, data={"status": "done"}, follow_redirects=True)
    with app.app_context():
        assert db.session.get(Task, tid).status == "done"


def test_invalid_status_value_is_rejected(client, app):
    """非法状态值不会写入数据库。"""
    uid = _make_member(app, "worker2", "团队成员")
    tid = _make_task(app, assignee_id=uid)
    _login(client, "worker2", INV_PWD)
    resp = client.post(
        "/tasks/%d/status" % tid, data={"status": "archived"}, follow_redirects=True
    )
    assert "不合法" in resp.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Task, tid).status == "pending"


# ---------------------------------------------------------------- US-11 状态筛选
def test_task_list_filters_by_status(client, app):
    """按状态筛选时只显示该状态的任务。"""
    _make_task(app, status="pending", title="待办任务")
    _make_task(app, status="in_progress", title="进行中任务")
    _make_task(app, status="done", title="已完成任务")
    _login(client)

    body = client.get("/tasks?status=in_progress").get_data(as_text=True)
    assert "进行中任务" in body
    assert "待办任务" not in body

    body = client.get("/tasks?status=done").get_data(as_text=True)
    assert "已完成任务" in body
    assert "进行中任务" not in body


def test_task_list_invalid_status_falls_back_to_all(client, app):
    """非法 status 参数回退为显示全部任务。"""
    _make_task(app, status="pending", title="待办任务")
    _login(client)
    body = client.get("/tasks?status=whatever").get_data(as_text=True)
    assert "待办任务" in body


def test_task_list_shows_status_counts(client, app):
    """列表顶部各状态计数与实际任务数一致。"""
    _make_task(app, status="pending", title="t1")
    _make_task(app, status="pending", title="t2")
    _make_task(app, status="done", title="t3")
    _login(client)
    body = client.get("/tasks").get_data(as_text=True)
    assert "全部 3" in body
    assert "未开始 2" in body
    assert "完成 1" in body


# ---------------------------------------------------------------- US-13 看板
def test_board_page_requires_login(client, app):
    """未登录访问看板跳转到登录页。"""
    resp = client.get("/board", follow_redirects=True)
    assert "登录" in resp.get_data(as_text=True)


def test_board_page_groups_tasks_by_status(client, app):
    """看板页按状态分三列展示任务。"""
    _make_task(app, status="pending", title="看板待办")
    _make_task(app, status="in_progress", title="看板进行中")
    _make_task(app, status="done", title="看板完成")
    _login(client)
    resp = client.get("/board")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    for text in ("未开始", "进行中", "完成", "看板待办", "看板进行中", "看板完成"):
        assert text in body


def test_board_stats_api_returns_counts(client, app):
    """统计接口返回各状态任务数与总数，供前端轮询。"""
    _make_task(app, status="pending", title="a")
    _make_task(app, status="done", title="b")
    _make_task(app, status="done", title="c")
    _login(client)
    resp = client.get("/board/api/stats")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    counts = {c["key"]: c["count"] for c in data["columns"]}
    assert counts == {"pending": 1, "in_progress": 0, "done": 2}
    assert data["total"] == 3


def test_board_stats_api_unauthenticated_returns_401_json(client, app):
    """未登录轮询接口返回 401 JSON，而不是跳转登录页的 HTML。"""
    resp = client.get("/board/api/stats")
    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False
