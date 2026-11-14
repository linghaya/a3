# -*- coding: utf-8 -*-
"""成员管理模块（Sprint 2 新增）：邀请成员、查看与管理成员列表。

对应 Sprint 2 用户故事：
- US-08 管理员邀请成员加入项目（Should）
- US-09 管理员查看并管理成员列表（Should）

权限控制：需要 member.manage 权限（默认仅授予管理员角色）。
权限在每次请求由 app.before_request 刷新（Sprint 3 清偿 TD-02），
管理员调整角色权限后，已登录成员的下一次请求即生效，无需重新登录。

Sprint 3：邀请码增加 7 天有效期（expires_at），到期后 is_usable 为假，
页面显示"已过期"；这是对 TD-05 的清偿。

路由（url_prefix=/members）：
- GET  /members                       成员列表 + 待使用邀请
- POST /members/invite                生成邀请码（默认 7 天有效）
- POST /members/invites/<id>/revoke   撤销邀请
- POST /members/<id>/role             调整成员角色
- POST /members/<id>/delete           移除成员
"""
import secrets
from datetime import timedelta

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, session, abort,
)
from .models import db, User, Role, Invite, utcnow

members_bp = Blueprint("members", __name__)

# 不能通过邀请码获得的管理员角色名：管理员只能由现有管理员手动调整角色产生
ADMIN_ROLE_NAME = "管理员"

# 邀请码默认有效期（天）。教学场景 7 天足够覆盖"课上生成、课下注册"的间隔；
# 真实产品应按团队策略可配置，见技术债 TD-05 的后续演进。
INVITE_TTL_DAYS = 7


def _require_login():
    """登录校验：未登录跳转登录页。"""
    if "user_id" not in session:
        flash("请先登录", "error")
        return redirect(url_for("auth.login"))
    return None


def _require_permission(perm_name):
    """权限校验：session 中不含该权限时返回 403。"""
    if perm_name not in session.get("permissions", []):
        flash("当前角色无此操作权限（需要 " + perm_name + "）", "error")
        abort(403)


def _current_user():
    """当前登录用户对象；会话失效时返回 None。"""
    return db.session.get(User, session.get("user_id"))


def _is_last_admin(user):
    """判断该成员是否为最后一个管理员（防止把系统管理员清空）。"""
    if user is None or user.role.name != ADMIN_ROLE_NAME:
        return False
    return User.query.join(Role).filter(Role.name == ADMIN_ROLE_NAME).count() <= 1


def _generate_code():
    """生成 8 位十六进制邀请码，并保证与库中已有邀请码不重复。"""
    while True:
        code = secrets.token_hex(4).upper()  # 形如 A1B2C3D4
        if Invite.query.filter_by(code=code).first() is None:
            return code


def _invitable_roles():
    """可被邀请的角色：除管理员外的所有角色（含管理员自建的角色）。"""
    return [r for r in Role.query.order_by(Role.id).all() if r.name != ADMIN_ROLE_NAME]


@members_bp.route("", methods=["GET"])
def member_list():
    """成员列表页：展示成员、所属角色、名下任务数与待使用邀请。"""
    redir = _require_login()
    if redir:
        return redir
    _require_permission("member.manage")

    users = User.query.order_by(User.id).all()
    invites = Invite.query.order_by(Invite.id.desc()).all()
    return render_template(
        "members.html",
        users=users,
        invites=invites,
        roles=_invitable_roles(),
    )


@members_bp.route("/invite", methods=["POST"])
def invite_create():
    """生成邀请码：需 member.manage 权限。

    表单字段：role（加入后的角色名，默认"团队成员"）。
    邀请码一次性使用，撤销后失效；管理员可在成员页查看邀请状态。
    """
    redir = _require_login()
    if redir:
        return redir
    _require_permission("member.manage")

    operator = _current_user()
    role_name = request.form.get("role", "团队成员").strip()
    role = Role.query.filter_by(name=role_name).first()
    if role is None:
        flash(f"角色「{role_name}」不存在，请先在角色权限页创建", "error")
        return redirect(url_for("members.member_list"))
    if role.name == ADMIN_ROLE_NAME:
        # 安全约束：不允许通过邀请码直接发放管理员角色
        flash("不能邀请管理员角色，请先按普通角色邀请后再调整角色", "error")
        return redirect(url_for("members.member_list"))

    invite = Invite(
        code=_generate_code(),
        role=role,
        invited_by=operator,
        status="pending",
        # Sprint 3 / TD-05：默认 7 天有效，过期后注册页直接拒绝
        expires_at=utcnow() + timedelta(days=INVITE_TTL_DAYS),
    )
    db.session.add(invite)
    db.session.commit()
    flash(
        f"邀请码已生成：{invite.code}（角色：{role.name}，"
        f"{INVITE_TTL_DAYS} 天内有效），请发给受邀成员，注册时填写即可加入",
        "success",
    )
    return redirect(url_for("members.member_list"))


@members_bp.route("/invites/<int:invite_id>/revoke", methods=["POST"])
def invite_revoke(invite_id):
    """撤销邀请：只有"待使用"的邀请可以撤销。"""
    redir = _require_login()
    if redir:
        return redir
    _require_permission("member.manage")

    invite = db.session.get(Invite, invite_id)
    if invite is None:
        abort(404)
    # 过期但仍为 pending 的邀请允许撤销（清理用），其他状态才提示"无需撤销"
    if invite.status != "pending":
        flash(f"邀请码 {invite.code} 当前状态为「{invite.status_label}」，无需撤销", "error")
        return redirect(url_for("members.member_list"))

    invite.status = "revoked"
    db.session.commit()
    flash(f"邀请码 {invite.code} 已撤销", "success")
    return redirect(url_for("members.member_list"))


@members_bp.route("/<int:user_id>/role", methods=["POST"])
def member_change_role(user_id):
    """调整成员角色：需 member.manage 权限。

    保护规则：不能修改自己的角色（避免误操作后失去管理入口），
    不能把最后一个管理员降级（避免系统没有管理员）。
    """
    redir = _require_login()
    if redir:
        return redir
    _require_permission("member.manage")

    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    if user.id == session.get("user_id"):
        flash("不能修改自己的角色，请让其他管理员操作", "error")
        return redirect(url_for("members.member_list"))

    role_name = request.form.get("role", "").strip()
    role = Role.query.filter_by(name=role_name).first()
    if role is None:
        flash(f"角色「{role_name}」不存在", "error")
        return redirect(url_for("members.member_list"))
    if _is_last_admin(user):
        flash("系统至少需要保留一名管理员，不能调整该成员的角色", "error")
        return redirect(url_for("members.member_list"))

    old_name = user.role.name
    user.role = role
    db.session.commit()
    flash(f"成员 {user.username} 的角色已由「{old_name}」调整为「{role.name}」", "success")
    return redirect(url_for("members.member_list"))


@members_bp.route("/<int:user_id>/delete", methods=["POST"])
def member_delete(user_id):
    """移除成员：需 member.manage 权限。

    保护规则：
    - 不能移除自己；
    - 不能移除最后一个管理员；
    - 名下还有未完成（pending / in_progress）任务时拒绝移除，
      要求先转派或关闭任务，避免出现"无主任务"（与任务模块的集成约束）；
    - 已完成任务保留历史记录，负责人置空后再删除成员。
    """
    redir = _require_login()
    if redir:
        return redir
    _require_permission("member.manage")

    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    if user.id == session.get("user_id"):
        flash("不能移除自己", "error")
        return redirect(url_for("members.member_list"))
    if _is_last_admin(user):
        flash("系统至少需要保留一名管理员，不能移除该成员", "error")
        return redirect(url_for("members.member_list"))

    unfinished = [t for t in user.tasks if t.status != "done"]
    if unfinished:
        flash(
            f"成员 {user.username} 名下还有 {len(unfinished)} 项未完成任务，"
            f"请先转派或标记完成后再移除",
            "error",
        )
        return redirect(url_for("members.member_list"))

    username = user.username
    for task in user.tasks:
        task.assignee_id = None  # 已完成的历史任务保留，负责人置空
    db.session.delete(user)
    db.session.commit()
    flash(f"成员 {username} 已移除（其已完成任务保留为未指派状态）", "success")
    return redirect(url_for("members.member_list"))
