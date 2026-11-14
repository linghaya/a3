# -*- coding: utf-8 -*-
"""角色与权限管理模块：角色的查看/创建/编辑权限/删除。

对应 Sprint 1 用户故事：
- US-04 管理员创建角色（Must）
- US-05 管理员管理角色权限（查看/分配/编辑/删除）（Must）

权限控制：需要 role.manage 权限（即管理员角色）。

路由（url_prefix=/roles）：
- GET  /roles            角色列表（含各角色的权限）
- POST /roles/create     创建角色
- POST /roles/<id>/edit-permissions  编辑角色权限
- POST /roles/<id>/delete            删除角色
"""
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, session, abort,
)
from . import PERMISSIONS
from .models import db, Role, Permission, User

roles_bp = Blueprint("roles", __name__)


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


@roles_bp.route("", methods=["GET"])
def role_list():
    """角色列表页：展示所有角色及其权限、成员数。"""
    redir = _require_login()
    if redir:
        return redir
    roles = Role.query.order_by(Role.id).all()
    return render_template("roles.html", roles=roles, permissions=PERMISSIONS)


@roles_bp.route("/create", methods=["POST"])
def role_create():
    """创建角色：需 role.manage 权限；角色名不可重复。"""
    redir = _require_login()
    if redir:
        return redir
    _require_permission("role.manage")

    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not name:
        flash("角色名不能为空", "error")
        return redirect(url_for("roles.role_list"))
    if Role.query.filter_by(name=name).first():
        flash("角色名已存在", "error")
        return redirect(url_for("roles.role_list"))

    role = Role(name=name, description=description)
    db.session.add(role)
    db.session.commit()
    flash(f"角色「{name}」创建成功，请为其分配权限", "success")
    return redirect(url_for("roles.role_list"))


@roles_bp.route("/<int:role_id>/edit-permissions", methods=["POST"])
def role_edit_permissions(role_id):
    """编辑角色权限：需 role.manage 权限；按勾选的权限名全量替换。"""
    redir = _require_login()
    if redir:
        return redir
    _require_permission("role.manage")

    role = db.session.get(Role, role_id)
    if role is None:
        abort(404)

    # 清空旧权限后按表单勾选项重新关联
    role.permissions = []
    selected = request.form.getlist("permissions")
    for pname in selected:
        perm = Permission.query.filter_by(name=pname).first()
        if perm is not None:
            role.permissions.append(perm)
    db.session.commit()

    perms_text = "、".join(selected) if selected else "（无）"
    flash(f"角色「{role.name}」权限已更新：{perms_text}", "success")
    return redirect(url_for("roles.role_list"))


@roles_bp.route("/<int:role_id>/delete", methods=["POST"])
def role_delete(role_id):
    """删除角色：需 role.manage 权限；仍有成员绑定时拒绝删除。

    注意：默认三大角色（管理员/项目经理/团队成员）不允许删除，
    避免系统失去基本角色骨架。
    """
    redir = _require_login()
    if redir:
        return redir
    _require_permission("role.manage")

    role = db.session.get(Role, role_id)
    if role is None:
        abort(404)
    if role.name in ("管理员", "项目经理", "团队成员"):
        flash("系统默认角色不可删除", "error")
        return redirect(url_for("roles.role_list"))
    if role.users:
        flash(f"角色「{role.name}」下还有 {len(role.users)} 名成员，不能删除", "error")
        return redirect(url_for("roles.role_list"))

    db.session.delete(role)
    db.session.commit()
    flash(f"角色「{role.name}」已删除", "success")
    return redirect(url_for("roles.role_list"))
