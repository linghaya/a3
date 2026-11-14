# -*- coding: utf-8 -*-
"""任务模块：任务列表与筛选、创建、详情、状态更新、评论与时间线。

对应 Sprint 1 用户故事：
- US-06 项目经理创建任务并分配负责人（Must）
- US-07 项目经理设置任务优先级与截止时间（Must）

对应 Sprint 2 用户故事：
- US-10 团队成员更新任务状态（未开始/进行中/完成）（Must）
- US-11 按状态筛选查看任务（Should）

对应 Sprint 3 用户故事：
- US-12 在任务下评论（Should，连续两轮顺延后本轮第一优先）
- US-14 任务状态变更记录（Should，Sprint 2 评审会上教师提出）

权限控制：
- 创建任务需要 task.assign 权限（项目经理及以上）；
- 更新状态需"任务负责人本人"或拥有 task.assign 权限；
- 评论：所有登录成员可发；删除仅作者本人或管理员（member.manage）。

路由（url_prefix=/tasks）：
- GET  /tasks                              任务列表（支持 ?status= 筛选）
- POST /tasks/create                       创建任务
- GET  /tasks/<id>                         任务详情（含时间线与评论区）
- POST /tasks/<id>/status                  更新任务状态（写 TaskActivity）
- POST /tasks/<id>/comments                发表评论（US-12）
- POST /tasks/<id>/comments/<cid>/delete   删除评论（作者或管理员）
"""
from datetime import datetime

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, session, url_for,
)
from .models import (
    STATUS_CHOICES, STATUS_VALUES, Comment, Task, TaskActivity, User, as_utc, db,
)

tasks_bp = Blueprint("tasks", __name__)


def _require_login():
    """登录校验：未登录跳转登录页。"""
    if "user_id" not in session:
        flash("请先登录", "error")
        return redirect(url_for("auth.login"))
    return None


def _current_user():
    """当前登录用户对象。"""
    return db.session.get(User, session.get("user_id"))


def _status_counts():
    """统计各状态任务数（用于筛选条上的数字）。"""
    counts = {value: Task.query.filter_by(status=value).count() for value in STATUS_VALUES}
    counts["all"] = Task.query.count()
    return counts


def _task_timeline(task):
    """把任务活动与评论合并成一条按时间正序的时间线（US-14 集成点）。

    两类事件来自不同的表，但用户看到的是"这件事什么时候发生了什么"，
    因此必须在查询层合并、统一排序，而不是在页面上分两块各排各的。
    """
    events = []
    for act in task.activities:
        events.append(
            {
                "kind": "activity",
                "at": as_utc(act.created_at),
                "actor": act.actor,
                "text": act.action_label,
                "activity": act,
            }
        )
    for comment in task.comments:
        events.append(
            {
                "kind": "comment",
                "at": as_utc(comment.created_at),
                "actor": comment.author,
                "text": comment.content,
                "comment": comment,
            }
        )
    events.sort(key=lambda e: (e["at"], 0 if e["kind"] == "activity" else 1))
    return events


@tasks_bp.route("", methods=["GET"])
def task_list():
    """任务列表页：展示任务，支持按状态筛选（?status=pending|in_progress|done|all）。

    非法筛选值不做报错，回退为"全部"，避免手改 URL 导致页面崩溃。
    """
    redir = _require_login()
    if redir:
        return redir

    current = request.args.get("status", "").strip()
    if current not in STATUS_VALUES:
        current = "all"

    query = Task.query if current == "all" else Task.query.filter_by(status=current)
    tasks = query.order_by(Task.priority, Task.id).all()
    users = User.query.order_by(User.id).all()
    return render_template(
        "tasks.html",
        tasks=tasks,
        users=users,
        current_status=current,
        counts=_status_counts(),
    )


@tasks_bp.route("/<int:task_id>", methods=["GET"])
def task_detail(task_id):
    """任务详情页：任务信息、状态更新、状态变更记录与评论时间线。"""
    redir = _require_login()
    if redir:
        return redir

    task = db.session.get(Task, task_id)
    if task is None:
        abort(404)
    user = _current_user()
    permissions = session.get("permissions", [])
    can_update = task.can_update_status(user, permissions)
    return render_template(
        "task_detail.html",
        task=task,
        can_update=can_update,
        status_choices=STATUS_CHOICES,
        timeline=_task_timeline(task),
        comment_max=Comment.MAX_LENGTH,
        is_admin="member.manage" in permissions,
    )


@tasks_bp.route("/<int:task_id>/status", methods=["POST"])
def task_change_status(task_id):
    """更新任务状态：负责人本人或拥有 task.assign 权限者可操作。

    Sprint 3 起每次变更都写一条 TaskActivity（谁、从什么状态、到什么状态、何时），
    供任务详情时间线追溯；这是 US-14 的核心验收点。
    """
    redir = _require_login()
    if redir:
        return redir

    task = db.session.get(Task, task_id)
    if task is None:
        abort(404)
    user = _current_user()
    if not task.can_update_status(user, session.get("permissions", [])):
        flash("只有任务负责人本人或拥有任务分配权限的成员可以更新任务状态", "error")
        abort(403)

    new_status = request.form.get("status", "").strip()
    if new_status not in STATUS_VALUES:
        flash("任务状态取值不合法", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id))

    old_status = task.status
    old_label = task.status_label
    task.status = new_status
    if old_status != new_status:
        db.session.add(
            TaskActivity(
                task=task,
                actor=user,
                action="status",
                from_status=old_status,
                to_status=new_status,
            )
        )
    db.session.commit()
    flash(f"任务「{task.title}」状态已由「{old_label}」更新为「{task.status_label}」", "success")

    # 仅允许站内相对路径回跳，防止开放重定向
    next_url = request.form.get("next", "").strip()
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect(url_for("tasks.task_detail", task_id=task.id))


@tasks_bp.route("/create", methods=["POST"])
def task_create():
    """创建任务：需 task.assign 权限。创建成功时写一条 created 活动。"""
    redir = _require_login()
    if redir:
        return redir
    if "task.assign" not in session.get("permissions", []):
        flash("当前角色无任务分配权限（需要 task.assign）", "error")
        abort(403)

    title = request.form.get("title", "").strip()
    if not title:
        flash("任务标题不能为空", "error")
        return redirect(url_for("tasks.task_list"))

    due_date = None
    due_text = request.form.get("due_date", "").strip()
    if due_text:
        try:
            due_date = datetime.strptime(due_text, "%Y-%m-%d").date()
        except ValueError:
            flash("截止时间格式应为 YYYY-MM-DD", "error")
            return redirect(url_for("tasks.task_list"))

    assignee = None
    assignee_id = request.form.get("assignee_id", "").strip()
    if assignee_id:
        assignee = db.session.get(User, int(assignee_id))

    try:
        priority = int(request.form.get("priority", 2))
    except ValueError:
        priority = 2
    if priority not in (1, 2, 3):
        priority = 2

    task = Task(
        title=title,
        description=request.form.get("description", "").strip(),
        priority=priority,
        due_date=due_date,
        assignee=assignee,
    )
    db.session.add(task)
    db.session.flush()
    db.session.add(
        TaskActivity(
            task=task,
            actor=_current_user(),
            action="created",
            to_status="pending",
        )
    )
    db.session.commit()

    who = assignee.username if assignee else "未指派"
    flash(f"任务「{title}」已创建，负责人：{who}", "success")
    return redirect(url_for("tasks.task_list"))


@tasks_bp.route("/<int:task_id>/comments", methods=["POST"])
def comment_create(task_id):
    """发表评论（US-12）：所有登录成员可参与任务讨论，内容不能为空且不超过 500 字。"""
    redir = _require_login()
    if redir:
        return redir

    task = db.session.get(Task, task_id)
    if task is None:
        abort(404)

    content = request.form.get("content", "").strip()
    if not content:
        flash("评论内容不能为空", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id) + "#timeline")
    if len(content) > Comment.MAX_LENGTH:
        flash(f"评论最多 {Comment.MAX_LENGTH} 字，请精简后再提交", "error")
        return redirect(url_for("tasks.task_detail", task_id=task.id) + "#timeline")

    comment = Comment(task=task, author=_current_user(), content=content)
    db.session.add(comment)
    db.session.commit()
    flash("评论已发布", "success")
    return redirect(url_for("tasks.task_detail", task_id=task.id) + "#timeline")


@tasks_bp.route("/<int:task_id>/comments/<int:comment_id>/delete", methods=["POST"])
def comment_delete(task_id, comment_id):
    """删除评论：仅作者本人或管理员（member.manage）可删除。

    不允许编辑，是为了保留讨论的原始语义（"说过什么"）。
    删除后保留一条 flash，但不保留墓碑记录——教学版本按此取舍，
    若用于真实审计场景应改为软删除（见技术债 TD-08）。
    """
    redir = _require_login()
    if redir:
        return redir

    task = db.session.get(Task, task_id)
    if task is None:
        abort(404)
    comment = db.session.get(Comment, comment_id)
    if comment is None or comment.task_id != task.id:
        abort(404)

    user = _current_user()
    is_admin = "member.manage" in session.get("permissions", [])
    if comment.author_id != user.id and not is_admin:
        flash("只能删除自己的评论，管理员可删除任意评论", "error")
        abort(403)

    db.session.delete(comment)
    db.session.commit()
    flash("评论已删除", "success")
    return redirect(url_for("tasks.task_detail", task_id=task.id) + "#timeline")
