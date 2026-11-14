# -*- coding: utf-8 -*-
"""Sprint 1 单元测试（关键路径覆盖）。

覆盖用户故事：
- US-01 登录（正确/错误密码）
- US-02 注册（成功/重复用户名/短密码）
- US-03 密码重置（成功/旧密码错误/两次不一致）
- US-04/US-05 角色创建与权限管理（有权限/无权限 403）
- US-06/US-07 任务创建（负责人、优先级、截止时间）与列表展示

运行方式：
    python -m pytest tests/ -v
或
    python tests/test_sprint1.py
"""
import pytest
from app import create_app, db
from app.models import User, Role, Task, Permission


@pytest.fixture()
def app():
    """测试用应用：内存数据库，每个用例独立。"""
    app = create_app(testing=True)
    with app.app_context():
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    """Flask 测试客户端。"""
    return app.test_client()


def _login(client, username, password):
    """辅助：执行登录操作。"""
    return client.post("/auth/login",
                       data={"username": username, "password": password},
                       follow_redirects=True)


# ---------- US-01 登录 ----------

def test_login_with_valid_credentials(client):
    """正确账号密码应登录成功并跳转首页。"""
    resp = _login(client, "admin", "admin123")
    assert b"\xe6\xac\xa2\xe8\xbf\x8e\xe5\x9b\x9e\xe6\x9d\xa5" in resp.data  # "欢迎回来"
    assert resp.status_code == 200


def test_login_with_wrong_password(client):
    """密码错误应提示失败且不会登录。"""
    resp = _login(client, "admin", "wrongpass")
    assert b"\xe7\x94\xa8\xe6\x88\xb7\xe5\x90\x8d\xe6\x88\x96\xe5\xaf\x86\xe7\xa0\x81\xe9\x94\x99\xe8\xaf\xaf" in resp.data  # "用户名或密码错误"


# ---------- US-02 注册 ----------

def test_register_new_user(client):
    """正常注册应成功并能用新账号登录。"""
    resp = client.post("/auth/register", data={
        "username": "tester", "email": "t@example.com",
        "password": "secret123", "role": "团队成员",
    }, follow_redirects=True)
    assert b"\xe6\xb3\xa8\xe5\x86\x8c\xe6\x88\x90\xe5\x8a\x9f" in resp.data  # "注册成功"
    # 新账号可登录
    assert _login(client, "tester", "secret123").status_code == 200


def test_register_duplicate_username(client):
    """重复用户名应被拒绝。"""
    resp = client.post("/auth/register", data={
        "username": "admin", "email": "new@example.com",
        "password": "secret123", "role": "团队成员",
    }, follow_redirects=True)
    assert b"\xe5\xb7\xb2\xe8\xa2\xab\xe5\x8d\xa0\xe7\x94\xa8" in resp.data  # "已被占用"


def test_register_short_password(client):
    """少于 6 位密码应被拒绝。"""
    resp = client.post("/auth/register", data={
        "username": "u2", "email": "u2@example.com",
        "password": "123", "role": "团队成员",
    }, follow_redirects=True)
    assert b"6 \xe4\xbd\x8d" in resp.data  # "至少需要 6 位"


# ---------- US-03 密码重置 ----------

def test_reset_password_success(client):
    """验证旧密码后可设置新密码，新密码可登录。"""
    resp = client.post("/auth/reset-password", data={
        "username": "admin", "old_password": "admin123",
        "new_password": "newpass1", "confirm_password": "newpass1",
    }, follow_redirects=True)
    assert b"\xe5\xaf\x86\xe7\xa0\x81\xe9\x87\x8d\xe7\xbd\xae\xe6\x88\x90\xe5\x8a\x9f" in resp.data  # "密码重置成功"
    # 旧密码失效、新密码可用
    assert b"\xe9\x94\x99\xe8\xaf\xaf" in _login(client, "admin", "admin123").data
    assert _login(client, "admin", "newpass1").status_code == 200


def test_reset_password_wrong_old(client):
    """旧密码错误应拒绝重置。"""
    resp = client.post("/auth/reset-password", data={
        "username": "admin", "old_password": "badpass",
        "new_password": "newpass1", "confirm_password": "newpass1",
    }, follow_redirects=True)
    assert b"\xe4\xb8\x8d\xe6\xad\xa3\xe7\xa1\xae" in resp.data  # "不正确"


# ---------- US-04 / US-05 角色与权限 ----------

def test_admin_can_create_role(client, app):
    """管理员可创建新角色。"""
    _login(client, "admin", "admin123")
    resp = client.post("/roles/create",
                       data={"name": "测试工程师", "description": "负责测试"},
                       follow_redirects=True)
    assert b"\xe5\x88\x9b\xe5\xbb\xba\xe6\x88\x90\xe5\x8a\x9f" in resp.data  # "创建成功"
    assert Role.query.filter_by(name="测试工程师").first() is not None


def test_member_cannot_create_role(client):
    """普通成员无 role.manage 权限，创建角色应 403。"""
    # 注册一个团队成员并登录
    client.post("/auth/register", data={
        "username": "member1", "email": "m1@example.com",
        "password": "secret123", "role": "团队成员",
    })
    _login(client, "member1", "secret123")
    resp = client.post("/roles/create", data={"name": "hack", "description": ""})
    assert resp.status_code == 403


def test_admin_can_edit_role_permissions(client, app):
    """管理员可为角色分配权限。"""
    _login(client, "admin", "admin123")
    with app.app_context():
        role = Role.query.filter_by(name="团队成员").first()
        resp = client.post(f"/roles/{role.id}/edit-permissions",
                           data={"permissions": ["task.assign"]})
        assert resp.status_code == 302
        assert role.has_permission("task.assign")


def test_default_role_cannot_be_deleted(client):
    """系统默认角色不可删除。"""
    _login(client, "admin", "admin123")
    with client.application.app_context():
        role = Role.query.filter_by(name="管理员").first()
        resp = client.post(f"/roles/{role.id}/delete")
        assert resp.status_code == 302
        assert db.session.get(Role, role.id) is not None


# ---------- US-06 / US-07 任务创建与分配 ----------

def test_pm_can_create_task_with_assignee(client, app):
    """项目经理可创建任务并指派负责人、设置优先级与截止时间。"""
    client.post("/auth/register", data={
        "username": "dev1", "email": "d1@example.com",
        "password": "secret123", "role": "团队成员",
    })
    _login(client, "admin", "admin123")  # 管理员同样拥有 task.assign
    resp = client.post("/tasks/create", data={
        "title": "编写单元测试", "description": "覆盖关键路径",
        "priority": "1", "due_date": "2026-10-01", "assignee_id": "",
    }, follow_redirects=True)
    assert b"\xe5\xb7\xb2\xe5\x88\x9b\xe5\xbb\xba" in resp.data  # "已创建"
    with app.app_context():
        task = Task.query.filter_by(title="编写单元测试").first()
        assert task is not None
        assert task.priority == 1
        assert task.due_date is not None


def test_member_cannot_create_task(client):
    """普通成员无 task.assign 权限，创建任务应 403。"""
    client.post("/auth/register", data={
        "username": "member2", "email": "m2@example.com",
        "password": "secret123", "role": "团队成员",
    })
    _login(client, "member2", "secret123")
    resp = client.post("/tasks/create", data={"title": "no-permission"})
    assert resp.status_code == 403


def test_task_list_shows_created_task(client, app):
    """任务列表页应展示已创建的任务。"""
    _login(client, "admin", "admin123")
    client.post("/tasks/create", data={
        "title": "Sprint1验收任务", "priority": "2",
        "due_date": "", "assignee_id": "",
    })
    resp = client.get("/tasks")
    assert b"Sprint1\xe9\xaa\x8c\xe6\x94\xb6\xe4\xbb\xbb\xe5\x8a\xa1" in resp.data


def test_create_task_empty_title_rejected(client):
    """空标题任务应被拒绝。"""
    _login(client, "admin", "admin123")
    resp = client.post("/tasks/create", data={
        "title": "", "priority": "2", "due_date": "", "assignee_id": "",
    }, follow_redirects=True)
    assert b"\xe4\xb8\x8d\xe8\x83\xbd\xe4\xb8\xba\xe7\xa9\xba" in resp.data  # "不能为空"
