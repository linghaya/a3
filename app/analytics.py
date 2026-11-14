# -*- coding: utf-8 -*-
"""数据与分析模块（Sprint 3 新增）：趋势监控、成员负载、项目报表导出。

对应 Sprint 3 用户故事：
- US-16 项目经理监控项目趋势（Should）：累计燃起、每日完成、完成率、逾期与风险清单
- US-15 管理员查看成员负载（Could）：按优先级加权的在办负载、逾期数、建议
- US-17 管理员/项目经理导出项目报表（Could）：CSV（带 BOM，Excel 直接打开不乱码）

设计取舍：
- 图表用服务端生成的 SVG，不引入 ECharts/Chart.js：演示环境可能没有外网 CDN，
  且静态 SVG 可以直接进 PPT/文档，不依赖浏览器脚本；
- 统计按请求实时聚合，不建物化表：教学项目任务量 < 100，实时聚合足够；
  任务量上来后需要预聚合（见技术债 TD-07）；
- 导出为同步 CSV：实现简单、可当场演示；大数据量应改为异步任务（TD-08）。

路由（url_prefix=/analytics）：
- GET /analytics                 趋势与报表首页
- GET /analytics/api/trend       趋势数据 JSON（供外部看板/演示脚本使用）
- GET /analytics/workload        成员负载视图（需 member.manage）
- GET /analytics/export.csv      任务报表导出（需 report.export）
"""
import csv
import io
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from flask import (
    Blueprint, Response, abort, flash, redirect, render_template, request, session, url_for,
)
from sqlalchemy import func

from .models import (
    STATUS_CHOICES, STATUS_VALUES, Comment, Task, TaskActivity, User, as_utc, db,
)

analytics_bp = Blueprint("analytics", __name__)

LOCAL_TZ = timezone(timedelta(hours=8))


def _require_login():
    """登录校验：未登录跳转登录页。"""
    if "user_id" not in session:
        flash("请先登录", "error")
        return redirect(url_for("auth.login"))
    return None


def _local_dt(value):
    """数据库 UTC 时间 → 东八区 naive 时间；None 原样返回。"""
    if value is None:
        return None
    return as_utc(value).astimezone(LOCAL_TZ).replace(tzinfo=None)


def _local_date(value):
    """UTC 时间戳 → 东八区日期。"""
    local = _local_dt(value)
    return local.date() if local else None


def _done_at_map():
    """每个任务"最后一次被标记完成"的时间（用于燃起与周期统计）。

    不能简单用 task.created_at 代替：Sprint 2 之前完成的任务没有活动记录，
    这里做兼容回退；而 Sprint 3 之后以 TaskActivity 的完成事件为准，
    这样"完成→重开→再完成"不会被重复计数。
    """
    done_at = {}
    activities = (
        TaskActivity.query.filter_by(action="status", to_status="done")
        .order_by(TaskActivity.created_at)
        .all()
    )
    for act in activities:
        done_at[act.task_id] = act.created_at

    # 兼容 Sprint 1/2 遗留数据：状态为 done 但没有活动记录
    for task in Task.query.filter_by(status="done").all():
        if task.id not in done_at:
            done_at[task.id] = task.created_at
    return done_at


def _daily_series(days=14):
    """生成最近 N 天的累计燃起数据（scope=累计创建，done=累计完成）。"""
    today = datetime.now(LOCAL_TZ).date()
    start = today - timedelta(days=days - 1)
    tasks = Task.query.all()
    done_at = _done_at_map()

    created_by_day = {}
    for task in tasks:
        day = _local_date(task.created_at)
        if day is not None:
            created_by_day[day] = created_by_day.get(day, 0) + 1
    done_by_day = {}
    for task in tasks:
        raw = done_at.get(task.id)
        if raw is None:
            continue
        day = _local_date(raw)
        if day is not None:
            done_by_day[day] = done_by_day.get(day, 0) + 1

    scope_before = sum(
        count for day, count in created_by_day.items() if day < start
    )
    done_before = sum(count for day, count in done_by_day.items() if day < start)

    series = []
    scope = scope_before
    done_total = done_before
    for offset in range(days):
        day = start + timedelta(days=offset)
        created_today = created_by_day.get(day, 0)
        done_today = done_by_day.get(day, 0)
        scope += created_today
        done_total += done_today
        series.append(
            {
                "date": day.isoformat(),
                "label": day.strftime("%m-%d"),
                "created": created_today,
                "completed": done_today,
                "scope": scope,
                "done_total": done_total,
                "open": scope - done_total,
            }
        )
    return series


def _kpis(series=None):
    """关键指标：总量、完成率、逾期、吞吐、平均周期、高风险任务。"""
    series = series if series is not None else _daily_series()
    tasks = Task.query.all()
    total = len(tasks)
    done = sum(1 for t in tasks if t.status == "done")
    open_tasks = [t for t in tasks if t.is_open]
    overdue = [t for t in open_tasks if t.is_overdue]
    high_open = [t for t in open_tasks if t.priority == 1]
    unassigned = [t for t in open_tasks if t.assignee_id is None]

    done_at = _done_at_map()
    cycles = []
    for task in tasks:
        if task.status != "done" or task.id not in done_at:
            continue
        created = _local_date(task.created_at)
        finished = _local_date(done_at[task.id])
        if created and finished:
            cycles.append(max(0, (finished - created).days))
    avg_cycle = round(sum(cycles) / len(cycles), 1) if cycles else 0.0

    today = datetime.now(LOCAL_TZ).date()
    week_ago = today - timedelta(days=6)
    throughput_7d = 0
    for task in tasks:
        raw = done_at.get(task.id)
        day = _local_date(raw) if raw else None
        if day is not None and week_ago <= day <= today:
            throughput_7d += 1

    return {
        "total": total,
        "done": done,
        "open": len(open_tasks),
        "overdue": len(overdue),
        "high_open": len(high_open),
        "unassigned": len(unassigned),
        "completion_rate": round(done * 100.0 / total, 1) if total else 0.0,
        "throughput_7d": throughput_7d,
        "avg_cycle_days": avg_cycle,
        "scope": series[-1]["scope"] if series else 0,
        "burned": series[-1]["done_total"] if series else 0,
    }


def _status_counts():
    """各状态任务数，口径与任务列表、看板一致（按 Task.status 聚合）。"""
    rows = db.session.query(Task.status, func.count(Task.id)).group_by(Task.status).all()
    counts = {value: 0 for value in STATUS_VALUES}
    for status, count in rows:
        if status in counts:
            counts[status] = count
    counts["all"] = sum(counts.values())
    return counts


def _priority_counts():
    """各优先级未完成任务数（风险视角：高优先级未完成最值得关注）。"""
    rows = (
        db.session.query(Task.priority, func.count(Task.id))
        .filter(Task.status != "done")
        .group_by(Task.priority)
        .all()
    )
    counts = {1: 0, 2: 0, 3: 0}
    for priority, count in rows:
        counts[priority] = count
    return counts


def _workload():
    """成员负载：按优先级加权统计在办任务，并给出可执行建议。

    weight：高=3 / 中=2 / 低=1。用加权值而不是条数，原因是"3 条低优先级"
    与"1 条高优先级 + 1 条低优先级"对交付的风险完全不同。
    """
    members = []
    for user in User.query.order_by(User.id).all():
        tasks = list(user.tasks)
        open_tasks = [t for t in tasks if t.is_open]
        overdue = [t for t in open_tasks if t.is_overdue]
        weight = sum(t.weight for t in open_tasks)
        if overdue:
            suggestion = f"有 {len(overdue)} 项逾期，建议优先处理或转派"
        elif weight >= 8:
            suggestion = "在办负载偏高，建议转派低优先级任务"
        elif weight == 0:
            suggestion = "暂无在办任务，可承接新任务"
        else:
            suggestion = "负载正常"
        members.append(
            {
                "user": user,
                "open": len(open_tasks),
                "done": sum(1 for t in tasks if t.status == "done"),
                "overdue": len(overdue),
                "weight": weight,
                "suggestion": suggestion,
            }
        )

    max_weight = max([m["weight"] for m in members] + [1])
    for member in members:
        member["percent"] = round(member["weight"] * 100.0 / max_weight)
        if member["weight"] >= 8:
            member["level"] = "高"
        elif member["weight"] >= 4:
            member["level"] = "中"
        else:
            member["level"] = "低"

    unassigned = [t for t in Task.query.filter(Task.status != "done").all()
                  if t.assignee_id is None]
    return {
        "members": members,
        "max_weight": max_weight,
        "unassigned_count": len(unassigned),
        "unassigned_weight": sum(t.weight for t in unassigned),
    }


def _risk_tasks(limit=6):
    """风险清单：逾期未完成优先，其次高优先级未完成，按截止时间升序。"""
    tasks = [t for t in Task.query.all() if t.is_open]
    tasks.sort(key=lambda t: (not t.is_overdue, t.priority, t.due_date or date.max, t.id))
    return tasks[:limit]


def _burnup_svg(series):
    """服务端生成累计燃起图 SVG（不依赖外部图表库）。

    两条线：scope（累计创建，灰色虚线）与 done（累计完成，蓝色实线）。
    线之间的垂直距离就是"还没做完的范围"，比单看完成率更直观。
    """
    width, height = 820, 270
    left, right, top, bottom = 52, 18, 26, 36
    plot_w = width - left - right
    plot_h = height - top - bottom
    max_value = max([p["scope"] for p in series] + [1])
    # 取整到 5 的倍数，避免 Y 轴出现 17、34 这种刻度
    step = 5 if max_value <= 25 else 10
    max_value = ((max_value + step - 1) // step) * step
    n = len(series)
    x_step = plot_w / max(1, n - 1)

    def y(value):
        return top + plot_h - (value / max_value) * plot_h

    def points(key):
        return " ".join(
            f"{left + i * x_step:.1f},{y(point[key]):.1f}" for i, point in enumerate(series)
        )

    grid_lines = []
    for tick in range(0, max_value + 1, step):
        gy = y(tick)
        grid_lines.append(
            f'<line x1="{left}" y1="{gy:.1f}" x2="{width - right}" y2="{gy:.1f}" '
            f'stroke="#e2e8f0" stroke-width="1"/>'
            f'<text x="{left - 10}" y="{gy + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#718096">{tick}</text>'
        )

    x_labels = []
    for i, point in enumerate(series):
        if i % 2 == 0 or i == n - 1:
            x_labels.append(
                f'<text x="{left + i * x_step:.1f}" y="{height - 12}" text-anchor="middle" '
                f'font-size="11" fill="#718096">{point["label"]}</text>'
            )

    area_points = f"{left},{top + plot_h} " + points("done_total") + f" {left + (n - 1) * x_step:.1f},{top + plot_h}"
    last = series[-1]
    return f'''<svg viewBox="0 0 {width} {height}" role="img" aria-label="项目累计燃起图"
     style="width:100%;height:auto;background:#fff;border-radius:6px">
  <defs>
    <linearGradient id="doneFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#3182ce" stop-opacity="0.22"/>
      <stop offset="100%" stop-color="#3182ce" stop-opacity="0.02"/>
    </linearGradient>
  </defs>
  {''.join(grid_lines)}
  <polygon points="{area_points}" fill="url(#doneFill)"/>
  <polyline points="{points('scope')}" fill="none" stroke="#a0aec0" stroke-width="2" stroke-dasharray="6 4"/>
  <polyline points="{points('done_total')}" fill="none" stroke="#2b6cb0" stroke-width="3"/>
  <circle cx="{left + (n - 1) * x_step:.1f}" cy="{y(last['done_total']):.1f}" r="4" fill="#2b6cb0"/>
  <text x="{left + (n - 1) * x_step - 6:.1f}" y="{y(last['done_total']) - 10:.1f}"
        text-anchor="end" font-size="12" font-weight="bold" fill="#2b6cb0">已完成 {last['done_total']}</text>
  {''.join(x_labels)}
  <text x="{left}" y="{top - 8}" font-size="11" fill="#4a5568">累计任务量</text>
  <line x1="{left + 150}" y1="{top - 12}" x2="{left + 180}" y2="{top - 12}" stroke="#a0aec0" stroke-width="2" stroke-dasharray="6 4"/>
  <text x="{left + 186}" y="{top - 8}" font-size="11" fill="#4a5568">总范围</text>
  <line x1="{left + 250}" y1="{top - 12}" x2="{left + 280}" y2="{top - 12}" stroke="#2b6cb0" stroke-width="3"/>
  <text x="{left + 286}" y="{top - 8}" font-size="11" fill="#4a5568">已完成</text>
</svg>'''


@analytics_bp.route("", methods=["GET"])
def dashboard():
    """趋势与报表首页：KPI + 累计燃起 + 状态/优先级分布 + 风险清单。"""
    redir = _require_login()
    if redir:
        return redir

    series = _daily_series()
    kpis = _kpis(series)
    workload = _workload()
    permissions = session.get("permissions", [])
    return render_template(
        "analytics.html",
        series=series,
        kpis=kpis,
        burnup_svg=_burnup_svg(series),
        status_counts=_status_counts(),
        priority_counts=_priority_counts(),
        workload=workload,
        risks=_risk_tasks(),
        can_export="report.export" in permissions,
        can_view_workload="member.manage" in permissions,
    )


@analytics_bp.route("/api/trend", methods=["GET"])
def api_trend():
    """趋势数据 JSON：给外部看板/演示脚本用，也是端到端测试的稳定接口。"""
    redir = _require_login()
    if redir:
        return {"ok": False, "error": "未登录"}, 401

    series = _daily_series()
    return {
        "ok": True,
        "generated_at": datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "kpis": _kpis(series),
        "series": series,
    }


@analytics_bp.route("/workload", methods=["GET"])
def workload():
    """成员负载视图（US-15）：需 member.manage 权限。"""
    redir = _require_login()
    if redir:
        return redir
    if "member.manage" not in session.get("permissions", []):
        flash("当前角色无成员管理权限（需要 member.manage）", "error")
        abort(403)

    data = _workload()
    return render_template(
        "workload.html",
        members=data["members"],
        unassigned_count=data["unassigned_count"],
        unassigned_weight=data["unassigned_weight"],
        max_weight=data["max_weight"],
    )


@analytics_bp.route("/export.csv", methods=["GET"])
def export_csv():
    """导出任务报表（US-17）：CSV + UTF-8 BOM，Excel 双击打开不乱码。

    权限：report.export（管理员与项目经理）。普通成员访问返回 403，
    这是"数据对外交付"与"页面浏览"的权限边界。
    """
    redir = _require_login()
    if redir:
        return redir
    if "report.export" not in session.get("permissions", []):
        flash("当前角色无报表导出权限（需要 report.export）", "error")
        abort(403)

    status = request.args.get("status", "").strip()
    query = Task.query if status not in STATUS_VALUES else Task.query.filter_by(status=status)
    tasks = query.order_by(Task.priority, Task.due_date, Task.id).all()
    done_at = _done_at_map()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "任务ID", "标题", "状态", "优先级", "负责人", "截止时间",
        "是否逾期", "创建时间", "完成时间", "评论数", "最后活动",
    ])
    for task in tasks:
        created = _local_dt(task.created_at)
        finished = _local_dt(done_at.get(task.id)) if task.status == "done" else None
        last = task.activities[-1] if task.activities else None
        writer.writerow([
            task.id,
            task.title,
            task.status_label,
            task.priority_label,
            task.assignee.username if task.assignee else "未指派",
            task.due_date.isoformat() if task.due_date else "",
            "是" if task.is_overdue else "否",
            created.strftime("%Y-%m-%d %H:%M") if created else "",
            finished.strftime("%Y-%m-%d %H:%M") if finished else "",
            len(task.comments),
            last.action_label if last else "",
        ])

    # BOM 是 Excel 识别 UTF-8 的关键；没有 BOM 时中文会变成乱码
    payload = "\ufeff" + buffer.getvalue()
    filename = "爱管理_任务报表_" + datetime.now(LOCAL_TZ).strftime("%Y%m%d_%H%M") + ".csv"
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                "attachment; filename=aimanage_tasks.csv; "
                f"filename*=UTF-8''{quote(filename)}"
            ),
            "Cache-Control": "no-store",
        },
    )
