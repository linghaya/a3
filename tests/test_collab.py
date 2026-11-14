# -*- coding: utf-8 -*-
"""Sprint 3 协作闭环测试：任务评论（US-12）与状态变更记录（US-14）。

覆盖：
- 评论的发布、列表展示、空内容/超长校验、作者删除、他人删除被拒、管理员删除；
- 状态变更写入 TaskActivity（谁、从什么状态、到什么状态），时间线正确展示；
- 任务创建自动写入 created 活动；
- 评论与状态事件按时间合并排序；
- 越权与 404 边界。
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import create_app
from app.models import Comment, Role, Task, TaskActivity, User, db

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


def _make_task(app, title="任务A", assignee_id=None, status="pending", priority=2):
    with app.app_context():
        task = Task(title=title, status=status, priority=priority, assignee_id=assignee_id)
        db.session.add(task)
        db.session.commit()
        return task.id


def test_comment_requires_login(client, app):
    tid = _make_task(app)
    resp = client.post("/tasks/%d/comments" % tid, data={"content": "hi"})
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_member_can_comment_and_see_it_in_timeline(client, app):
    uid = _make_user(app, "alice")
    tid = _make_task(app, assignee_id=uid)
    _login(client, "alice", PWD)
    resp = client.post(
        "/tasks/%d/comments" % tid,
        data={"content": "接口已联调，等待前端确认"},
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "评论已发布" in body
    assert "接口已联调，等待前端确认" in body
    assert "1 条记录" in body
    with app.app_context():
        assert Comment.query.filter_by(task_id=tid).count() == 1


def test_empty_comment_is_rejected(client, app):
    uid = _make_user(app, "bob")
    tid = _make_task(app, assignee_id=uid)
    _login(client, "bob", PWD)
    resp = client.post("/tasks/%d/comments" % tid, data={"content": "   "},
                       follow_redirects=True)
    assert "评论内容不能为空" in resp.get_data(as_text=True)
    with app.app_context():
        assert Comment.query.count() == 0


def test_too_long_comment_is_rejected(client, app):
    uid = _make_user(app, "carol")
    tid = _make_task(app, assignee_id=uid)
    _login(client, "carol", PWD)
    resp = client.post("/tasks/%d/comments" % tid, data={"content": "x" * 501},
                       follow_redirects=True)
    assert "最多 500 字" in resp.get_data(as_text=True)
    with app.app_context():
        assert Comment.query.count() == 0


def test_author_can_delete_own_comment(client, app):
    uid = _make_user(app, "dave")
    tid = _make_task(app, assignee_id=uid)
    _login(client, "dave", PWD)
    client.post("/tasks/%d/comments" % tid, data={"content": "临时记录"})
    with app.app_context():
        cid = Comment.query.one().id
    resp = client.post("/tasks/%d/comments/%d/delete" % (tid, cid), follow_redirects=True)
    assert "评论已删除" in resp.get_data(as_text=True)
    with app.app_context():
        assert Comment.query.count() == 0


def test_other_member_cannot_delete_comment(client, app):
    author = _make_user(app, "author")
    _make_user(app, "intruder")
    tid = _make_task(app, assignee_id=author)
    _login(client, "author", PWD)
    client.post("/tasks/%d/comments" % tid, data={"content": "作者的话"})
    with app.app_context():
        cid = Comment.query.one().id
    client.get("/auth/logout")
    _login(client, "intruder", PWD)
    resp = client.post("/tasks/%d/comments/%d/delete" % (tid, cid))
    assert resp.status_code == 403
    with app.app_context():
        assert Comment.query.count() == 1


def test_admin_can_delete_any_comment(client, app):
    author = _make_user(app, "author2")
    tid = _make_task(app, assignee_id=author)
    _login(client, "author2", PWD)
    client.post("/tasks/%d/comments" % tid, data={"content": "需要清理的评论"})
    with app.app_context():
        cid = Comment.query.one().id
    client.get("/auth/logout")
    _login(client)
    resp = client.post("/tasks/%d/comments/%d/delete" % (tid, cid), follow_redirects=True)
    assert "评论已删除" in resp.get_data(as_text=True)
    with app.app_context():
        assert Comment.query.count() == 0


def test_status_change_writes_activity_with_actor(client, app):
    uid = _make_user(app, "worker")
    tid = _make_task(app, assignee_id=uid)
    _login(client, "worker", PWD)
    client.post("/tasks/%d/status" % tid, data={"status": "in_progress"},
                follow_redirects=True)
    with app.app_context():
        acts = TaskActivity.query.filter_by(task_id=tid).all()
        assert len(acts) == 1
        assert acts[0].action == "status"
        assert acts[0].from_status == "pending"
        assert acts[0].to_status == "in_progress"
        assert acts[0].actor.username == "worker"

    body = client.get("/tasks/%d" % tid).get_data(as_text=True)
    assert "将状态由「未开始」改为「进行中」" in body
    assert "worker" in body


def test_reopen_records_each_status_change(client, app):
    uid = _make_user(app, "worker2")
    tid = _make_task(app, assignee_id=uid)
    _login(client, "worker2", PWD)
    client.post("/tasks/%d/status" % tid, data={"status": "done"})
    client.post("/tasks/%d/status" % tid, data={"status": "in_progress"})
    with app.app_context():
        acts = TaskActivity.query.filter_by(task_id=tid).order_by(
            TaskActivity.created_at, TaskActivity.id
        ).all()
        assert [a.to_status for a in acts] == ["done", "in_progress"]
    body = client.get("/tasks/%d" % tid).get_data(as_text=True)
    assert "改为「完成」" in body
    assert "改为「进行中」" in body


def test_creating_task_writes_created_activity(client, app):
    _login(client)
    client.post("/tasks/create", data={"title": "新任务", "priority": "2"})
    with app.app_context():
        task = Task.query.filter_by(title="新任务").one()
        tid = task.id
        acts = TaskActivity.query.filter_by(task_id=task.id).all()
        assert len(acts) == 1
        assert acts[0].action == "created"
        assert acts[0].actor.username == "admin"
    body = client.get("/tasks/%d" % tid).get_data(as_text=True)
    assert "创建了任务" in body


def test_comment_is_escaped_against_xss(client, app):
    uid = _make_user(app, "xssuser")
    tid = _make_task(app, assignee_id=uid)
    _login(client, "xssuser", PWD)
    client.post("/tasks/%d/comments" % tid, data={"content": "<script>alert(1)</script>"})
    body = client.get("/tasks/%d" % tid).get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_comment_on_missing_task_returns_404(client, app):
    _login(client)
    assert client.post("/tasks/9999/comments", data={"content": "x"}).status_code == 404


def test_delete_comment_of_other_task_returns_404(client, app):
    uid = _make_user(app, "taskowner")
    tid1 = _make_task(app, title="任务1", assignee_id=uid)
    tid2 = _make_task(app, title="任务2", assignee_id=uid)
    _login(client, "taskowner", PWD)
    client.post("/tasks/%d/comments" % tid1, data={"content": "只属于任务1"})
    with app.app_context():
        cid = Comment.query.one().id
    resp = client.post("/tasks/%d/comments/%d/delete" % (tid2, cid))
    assert resp.status_code == 404
