# -*- coding: utf-8 -*-
"""Sprint 3 趋势、负载与报表测试（US-15 / US-16 / US-17）。

覆盖：
- 趋势页与 JSON 接口的统计口径（累计范围、累计完成、完成率、逾期）；
- "完成→重开→再完成"只计一次完成，不重复抬高燃起曲线；
- 成员负载按优先级加权，逾期优先提示；
- 报表导出权限边界、CSV BOM、字段内容、状态筛选；
- 项目经理默认拥有 report.export，普通成员没有。
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import create_app
from app.models import Role, Task, TaskActivity, User, db

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


def _make_task(app, title, status="pending", priority=2, assignee_id=None,
               created_days_ago=0, due_days_from_now=None, done_days_ago=None):
    with app.app_context():
        now = datetime.now(timezone.utc)
        task = Task(
            title=title,
            status=status,
            priority=priority,
            assignee_id=assignee_id,
            created_at=now - timedelta(days=created_days_ago),
            due_date=(now + timedelta(days=due_days_from_now)).date()
            if due_days_from_now is not None else None,
        )
        db.session.add(task)
        db.session.flush()
        if done_days_ago is not None:
            db.session.add(
                TaskActivity(
                    task=task,
                    actor_id=1,
                    action="status",
                    from_status="in_progress",
                    to_status="done",
                    created_at=now - timedelta(days=done_days_ago),
                )
            )
        db.session.commit()
        return task.id


def test_analytics_requires_login(client, app):
    resp = client.get("/analytics")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_dashboard_shows_kpis_and_burnup(client, app):
    _make_task(app, "已完成A", status="done", done_days_ago=2)
    _make_task(app, "已完成B", status="done", done_days_ago=1)
    _make_task(app, "逾期任务", status="pending", priority=1, due_days_from_now=-2)
    _make_task(app, "在办任务", status="in_progress", priority=2)
    _login(client)
    body = client.get("/analytics").get_data(as_text=True)
    assert "项目趋势与报表" in body
    assert "整体完成率" in body
    assert "50.0%" in body  # 4 项完成 2 项
    assert "累计燃起图" in body
    assert "svg" in body
    assert "逾期任务" in body


def test_trend_api_cumulative_numbers(client, app):
    _make_task(app, "old1", status="done", created_days_ago=8, done_days_ago=5)
    _make_task(app, "old2", status="pending", created_days_ago=7)
    _login(client)
    data = client.get("/analytics/api/trend").get_json()
    assert data["ok"] is True
    assert len(data["series"]) == 14
    last = data["series"][-1]
    assert last["scope"] == 2
    assert last["done_total"] == 1
    assert last["open"] == 1
    assert data["kpis"]["total"] == 2
    assert data["kpis"]["completion_rate"] == 50.0


def test_reopened_task_is_not_double_counted(client, app):
    tid = _make_task(app, "反复横跳", status="done", created_days_ago=6, done_days_ago=4)
    with app.app_context():
        db.session.add(
            TaskActivity(
                task_id=tid, actor_id=1, action="status",
                from_status="done", to_status="in_progress",
                created_at=datetime.now(timezone.utc) - timedelta(days=3),
            )
        )
        db.session.add(
            TaskActivity(
                task_id=tid, actor_id=1, action="status",
                from_status="in_progress", to_status="done",
                created_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
        )
        db.session.commit()
    _login(client)
    data = client.get("/analytics/api/trend").get_json()
    assert data["series"][-1]["done_total"] == 1
    assert data["kpis"]["done"] == 1


def test_workload_requires_member_manage(client, app):
    _make_user(app, "plain", "团队成员")
    _login(client, "plain", PWD)
    assert client.get("/analytics/workload").status_code == 403


def test_workload_is_weighted_and_flags_overdue(client, app):
    uid = _make_user(app, "busy", "团队成员")
    _make_task(app, "高优在办", priority=1, assignee_id=uid)
    _make_task(app, "中优在办", priority=2, assignee_id=uid)
    _make_task(app, "逾期任务", priority=3, assignee_id=uid, due_days_from_now=-1)
    _make_task(app, "已完成", status="done", priority=1, assignee_id=uid,
               done_days_ago=1)
    _login(client)
    body = client.get("/analytics/workload").get_data(as_text=True)
    assert "成员负载视图" in body
    assert "busy" in body
    # 3 项在办（高 3 + 中 2 + 低 1 = 6 点），另有 1 项逾期
    assert "6" in body
    assert "项逾期" in body


def test_member_cannot_export_report(client, app):
    _make_user(app, "plain2", "团队成员")
    _login(client, "plain2", PWD)
    resp = client.get("/analytics/export.csv")
    assert resp.status_code == 403


def test_admin_export_csv_has_bom_and_content(client, app):
    uid = _make_user(app, "worker", "团队成员")
    _make_task(app, "导出任务甲", status="done", assignee_id=uid,
               created_days_ago=3, done_days_ago=1)
    _make_task(app, "导出任务乙", status="pending", priority=1)
    _login(client)
    resp = client.get("/analytics/export.csv")
    assert resp.status_code == 200
    assert resp.data.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    text = resp.data.decode("utf-8-sig")
    assert "任务ID,标题,状态,优先级,负责人,截止时间" in text
    assert "导出任务甲" in text
    assert "导出任务乙" in text
    assert "完成" in text
    assert "worker" in text
    assert "filename*=UTF-8''" in resp.headers["Content-Disposition"]


def test_export_csv_status_filter(client, app):
    _make_task(app, "只导出完成", status="done", done_days_ago=1)
    _make_task(app, "不要导出", status="pending")
    _login(client)
    text = client.get("/analytics/export.csv?status=done").data.decode("utf-8-sig")
    assert "只导出完成" in text
    assert "不要导出" not in text


def test_project_manager_has_export_permission_by_default(client, app):
    _make_user(app, "pmx", "项目经理")
    _login(client, "pmx", PWD)
    resp = client.get("/analytics/export.csv")
    assert resp.status_code == 200
