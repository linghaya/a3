# -*- coding: utf-8 -*-
"""项目看板模块（Sprint 2 新增）：按状态分列展示全部任务。

对应 Sprint 2 用户故事：
- US-13 项目经理查看项目整体看板（任务分布与状态）（Must）

看板为只读视图，登录用户均可查看；状态变更入口仍受 task.assign 权限
与"负责人本人"规则约束（见 tasks.task_change_status）。

Sprint 3 偿还 TD-01：把 5 秒轮询升级为 SSE（Server-Sent Events）长连接推送。
- 浏览器用 EventSource 订阅 /board/stream，服务端每 5 秒推一次统计数据；
- 当任务 id 列表发生变化时，页面自动重载看板卡片（只更新计数不重排，
  会让人误以为"卡片没动"，Sprint 2 的演示缺陷正出在这里）；
- EventSource 不可用时自动降级为 10 秒轮询 /board/api/stats，保证可演示；
- 已知边界：SSE 事件源在单进程内广播，多 worker 部署需要 Redis pub/sub，
  登记为 TD-06，不影响单机教学部署。

路由（url_prefix=/board）：
- GET /board            看板页
- GET /board/api/stats  看板统计数据（JSON，轮询降级用）
- GET /board/stream     SSE 事件流（Sprint 3 新增）
"""
import json
import time
from datetime import datetime

from flask import (
    Blueprint, Response, current_app, flash, jsonify, redirect,
    render_template, session, stream_with_context, url_for,
)
from .models import STATUS_CHOICES, Task

board_bp = Blueprint("board", __name__)


def _column_stats():
    """按状态汇总任务：返回 [{key, label, count, task_ids}, ...]。

    页面渲染与 JSON 接口共用该函数，保证两边口径一致。
    """
    counts = {value: 0 for value, _label in STATUS_CHOICES}
    ids = {value: [] for value, _label in STATUS_CHOICES}
    for task in Task.query.order_by(Task.priority, Task.due_date, Task.id).all():
        key = task.status if task.status in counts else "pending"
        counts[key] += 1
        ids[key].append(task.id)
    return [
        {"key": value, "label": label, "count": counts[value], "task_ids": ids[value]}
        for value, label in STATUS_CHOICES
    ]


def _detail_endpoint():
    """返回"点击卡片进入任务详情"要用的端点名。

    US-10 的任务详情页在 feat/task-status 分支上开发，与本分支并行；
    合并前该端点不存在，此时退回任务列表，避免看板页因 BuildError 打不开。
    合并进集成分支后自动切换到 tasks.task_detail（无需再改代码）。
    """
    if "tasks.task_detail" in current_app.view_functions:
        return "tasks.task_detail"
    return "tasks.task_list"


@board_bp.route("", methods=["GET"])
def board_view():
    """看板页：三列（未开始/进行中/完成）+ 逾期高亮 + 轮询刷新。"""
    if "user_id" not in session:
        flash("请先登录", "error")
        return redirect(url_for("auth.login"))

    tasks = Task.query.order_by(Task.priority, Task.due_date, Task.id).all()
    grouped = {value: [] for value, _label in STATUS_CHOICES}
    for task in tasks:
        grouped[task.status if task.status in grouped else "pending"].append(task)

    columns = [
        {
            "key": value,
            "label": label,
            "tasks": grouped[value],
            "count": len(grouped[value]),
            "ids": ",".join(str(t.id) for t in grouped[value]),
        }
        for value, label in STATUS_CHOICES
    ]
    return render_template(
        "board.html",
        columns=columns,
        total=len(tasks),
        generated_at=datetime.now().strftime("%H:%M:%S"),
        detail_endpoint=_detail_endpoint(),
    )


@board_bp.route("/api/stats", methods=["GET"])
def board_stats():
    """看板统计接口：返回各状态任务数与任务 id 列表。

    未登录返回 401 JSON（而不是跳转登录页），避免前端轮询拿到整页 HTML。
    """
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "未登录"}), 401

    columns = _column_stats()
    return jsonify({
        "ok": True,
        "generated_at": datetime.now().strftime("%H:%M:%S"),
        "total": sum(c["count"] for c in columns),
        "columns": columns,
    })


def _stats_payload():
    """组装一次推送/轮询共用的数据结构，保证 SSE 与 JSON 接口口径一致。"""
    columns = _column_stats()
    return {
        "ok": True,
        "generated_at": datetime.now().strftime("%H:%M:%S"),
        "total": sum(c["count"] for c in columns),
        "columns": columns,
    }


@board_bp.route("/stream", methods=["GET"])
def board_stream():
    """SSE 实时推送看板统计（Sprint 3 偿还 TD-01）。

    实现要点：
    - 生成器每 5 秒查询一次数据库，必须显式进入 app_context，
      否则请求结束后 db.session 已被回收，第二次推送就会 500；
    - 首个事件立即发送，避免前端等 5 秒才显示"已连接"；
    - 客户端断开时 GeneratorExit 会终止循环，不留下僵尸线程；
    - 只推送统计数据（计数与 id 列表），任务卡片由前端在 id 变化时重载，
      这样不会在用户阅读卡片时突然重排。

    已知边界（TD-06）：单进程内存事件源，多 worker 部署需 Redis pub/sub。
    """
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "未登录"}), 401

    app = current_app._get_current_object()

    def generate():
        while True:
            with app.app_context():
                payload = json.dumps(_stats_payload(), ensure_ascii=False)
            yield f"event: stats\ndata: {payload}\n\n"
            time.sleep(5)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 反向代理（如 Nginx）不缓冲
            "Connection": "keep-alive",
        },
    )
