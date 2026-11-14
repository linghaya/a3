# -*- coding: utf-8 -*-
"""端到端验收测试（发布就绪 DoD 的核心证据）。

两条旅程：
1. 主旅程：邀请 → 注册入队 → 建任务并指派 → 成员评论 → 更新状态 → 趋势/报表 → 看板 → 结项；
2. 异常/权限旅程：越权导出、越权访问负载、越权改他人任务、越权删他人评论、
   过期邀请码注册、未登录 SSE。
另有一条"发布门禁"冒烟：管理员可打开全部发布页面，导出返回附件。
"""
import csv
import io
from datetime import datetime, timedelta, timezone

import pytest

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


def _login(client, username="admin", password="admin123"):
    return client.post(
        "/auth/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )


def test_end_to_end_main_journey(app):
    admin = app.test_client()
    member = app.test_client()
    _login(admin)

    # 1. 管理员生成邀请码
    admin.post("/members/invite", data={"role": "团队成员"}, follow_redirects=True)
    with app.app_context():
        code = Invite.query.one().code
        assert Invite.query.one().days_left >= 6

    # 2. 新成员凭码注册并登录（角色以邀请码为准）
    member.post(
        "/auth/register",
        data={
            "username": "e2e_alice", "email": "e2e_alice@example.com",
            "password": PWD, "confirm_password": PWD, "invite_code": code,
        },
        follow_redirects=True,
    )
    _login(member, "e2e_alice", PWD)
    with app.app_context():
        alice = User.query.filter_by(username="e2e_alice").one()
        alice_id = alice.id
        assert alice.role.name == "团队成员"

    # 3. 管理员建任务并指派给新成员
    admin.post(
        "/tasks/create",
        data={
            "title": "E2E：完成发布说明", "description": "端到端主旅程",
            "priority": "1", "due_date": "2026-12-31", "assignee_id": str(alice_id),
        },
        follow_redirects=True,
    )
    with app.app_context():
        task = Task.query.filter_by(title="E2E：完成发布说明").one()
        tid = task.id

    # 4. 成员能看到任务，发表评论并把状态改为进行中
    assert "E2E：完成发布说明" in member.get("/tasks").get_data(as_text=True)
    member.post("/tasks/%d/comments" % tid, data={"content": "初稿今天下班前给出"},
                follow_redirects=True)
    member.post("/tasks/%d/status" % tid, data={"status": "in_progress"},
                follow_redirects=True)
    detail = member.get("/tasks/%d" % tid).get_data(as_text=True)
    assert "初稿今天下班前给出" in detail
    assert "将状态由「未开始」改为「进行中」" in detail

    # 5. 趋势页反映"1 项在办、0 项完成"，风险清单含该任务
    trend = admin.get("/analytics/api/trend").get_json()
    assert trend["kpis"]["total"] == 1
    assert trend["kpis"]["open"] == 1
    assert trend["kpis"]["done"] == 0
    assert trend["kpis"]["high_open"] == 1

    # 6. 报表导出包含该任务、评论数为 1、状态为进行中
    export = admin.get("/analytics/export.csv")
    assert export.status_code == 200
    assert export.data.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(export.data.decode("utf-8-sig"))))
    row = [r for r in rows if r and r[0] == str(tid)][0]
    assert row[1] == "E2E：完成发布说明"
    assert row[2] == "进行中"
    assert row[9] == "1"

    # 7. 看板口径与任务状态一致
    stats = admin.get("/board/api/stats").get_json()
    counts = {c["key"]: c["count"] for c in stats["columns"]}
    assert counts["in_progress"] == 1

    # 8. 成员完成任务，趋势与报表同步为 100%
    member.post("/tasks/%d/status" % tid, data={"status": "done"}, follow_redirects=True)
    trend = admin.get("/analytics/api/trend").get_json()
    assert trend["kpis"]["done"] == 1
    assert trend["kpis"]["completion_rate"] == 100.0
    rows = list(csv.reader(io.StringIO(
        admin.get("/analytics/export.csv").data.decode("utf-8-sig"))))
    row = [r for r in rows if r and r[0] == str(tid)][0]
    assert row[2] == "完成"
    assert row[8] != ""  # 完成时间非空

    # 9. 管理员工作负载页可打开，成员被拒
    assert admin.get("/analytics/workload").status_code == 200
    assert member.get("/analytics/workload").status_code == 403

    # 10. 结项：任务已完成，成员可被移除，历史任务负责人置空
    resp = admin.post("/members/%d/delete" % alice_id, follow_redirects=True)
    assert "已移除" in resp.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(User, alice_id) is None
        assert db.session.get(Task, tid).assignee_id is None


def test_exception_and_permission_journey(app):
    admin = app.test_client()
    member = app.test_client()
    _login(admin)

    with app.app_context():
        role = Role.query.filter_by(name="团队成员").one()
        other = User(username="e2e_bob", email="bob@example.com", role_id=role.id)
        other.set_password(PWD)
        db.session.add(other)
        db.session.commit()
        bob_id = other.id
        task = Task(title="他人的任务", priority=2, assignee_id=bob_id)
        db.session.add(task)
        db.session.commit()
        tid = task.id

    _login(member, "e2e_bob", PWD)
    # 普通成员没有导出与负载权限
    assert member.get("/analytics/export.csv").status_code == 403
    assert member.get("/analytics/workload").status_code == 403

    # 未登录不能订阅 SSE
    anon = app.test_client()
    assert anon.get("/board/stream").status_code == 401

    # 普通成员不能改他人任务
    with app.app_context():
        third_role = Role.query.filter_by(name="团队成员").one()
        third = User(username="e2e_carol", email="carol@example.com",
                     role_id=third_role.id)
        third.set_password(PWD)
        db.session.add(third)
        db.session.commit()
        carol_id = third.id
    carol = app.test_client()
    _login(carol, "e2e_carol", PWD)
    assert carol.post("/tasks/%d/status" % tid, data={"status": "done"}).status_code == 403

    # 普通成员不能删除他人评论
    member.post("/tasks/%d/comments" % tid, data={"content": "bob 的评论"})
    with app.app_context():
        from app.models import Comment
        cid = Comment.query.one().id
    assert carol.post("/tasks/%d/comments/%d/delete" % (tid, cid)).status_code == 403

    # 过期邀请码注册被拒
    with app.app_context():
        admin_user = User.query.filter_by(username="admin").one()
        db.session.add(
            Invite(
                code="E2EEXPIRED", role=third_role, invited_by_id=admin_user.id,
                status="pending",
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        db.session.commit()
    resp = app.test_client().post(
        "/auth/register",
        data={
            "username": "e2e_late", "email": "late@example.com",
            "password": PWD, "confirm_password": PWD, "invite_code": "E2EEXPIRED",
        },
        follow_redirects=True,
    )
    assert "可能已过期" in resp.get_data(as_text=True)


def test_release_gate_smoke(app):
    """发布门禁冒烟：管理员可打开全部页面，导出为附件且含 BOM。"""
    admin = app.test_client()
    _login(admin)
    pages = [
        "/", "/roles", "/tasks", "/members", "/board", "/analytics",
        "/analytics/workload", "/analytics/api/trend",
    ]
    for page in pages:
        resp = admin.get(page)
        assert resp.status_code == 200, page

    export = admin.get("/analytics/export.csv")
    assert export.status_code == 200
    assert export.mimetype == "text/csv"
    assert "attachment" in export.headers["Content-Disposition"]
    assert export.data.startswith(b"\xef\xbb\xbf")
