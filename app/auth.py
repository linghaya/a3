# -*- coding: utf-8 -*-
"""认证模块：注册、登录、登出、密码重置。

对应 Sprint 1 用户故事：
- US-01 账号密码登录（Must）
- US-02 注册账号（Must）
- US-03 重置密码（Should）

路由：
- GET/POST /auth/register  注册
- GET/POST /auth/login     登录
- GET     /auth/logout     登出
- GET/POST /auth/reset-password  重置密码
"""
from datetime import datetime, timezone
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, session,
)
from .models import db, User, Role, Invite

auth_bp = Blueprint("auth", __name__)

# 注册时可选的角色（管理员不开放自助注册，避免权限扩散）
REGISTERABLE_ROLES = ["项目经理", "团队成员"]


@auth_bp.route("/auth/register", methods=["GET", "POST"])
def register():
    """注册新账号：校验用户名/邮箱唯一性，默认角色为团队成员。

    Sprint 2 增量（US-08 集成点）：表单新增可选字段 invite_code。
    填写邀请码时，角色以邀请码预设角色为准（忽略表单选择的角色），
    注册成功后邀请码置为"已使用"，保证一次性使用；不填则保持 Sprint 1 行为。
    """
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        role_name = request.form.get("role", "团队成员")
        invite_code = request.form.get("invite_code", "").strip().upper()

        if not username or not email or not password:
            flash("用户名、邮箱和密码都不能为空", "error")
            return render_template("register.html", roles=REGISTERABLE_ROLES)
        if len(password) < 6:
            flash("密码至少需要 6 位", "error")
            return render_template("register.html", roles=REGISTERABLE_ROLES)

        # 邀请码校验：只有"待使用且未过期"的邀请码可用，且一次失效
        invite = None
        if invite_code:
            invite = Invite.query.filter_by(code=invite_code).first()
            if invite is None or not invite.is_usable:
                # 保持 Sprint 2 的提示前缀不变（回归用例依赖），补充过期说明
                flash("邀请码无效或已被使用（可能已过期），请联系管理员重新获取", "error")
                return render_template("register.html", roles=REGISTERABLE_ROLES)

        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash("用户名或邮箱已被占用", "error")
            return render_template("register.html", roles=REGISTERABLE_ROLES)

        if invite is not None:
            role = invite.role  # 以邀请码预设角色为准，防止被邀请人自选更高角色
        else:
            role = Role.query.filter_by(name=role_name).first()
            if role is None:
                role = Role.query.filter_by(name="团队成员").first()

        user = User(username=username, email=email, role=role)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()  # 先拿到 user.id，供邀请码记录"被谁使用"

        if invite is not None:
            invite.status = "accepted"
            invite.accepted_by_id = user.id
            invite.accepted_at = datetime.now(timezone.utc)

        db.session.commit()
        if invite is not None:
            flash(f"注册成功，{username}，已通过邀请码加入团队，当前角色：{role.name}", "success")
        else:
            flash(f"注册成功，{username}，当前角色：{role.name}", "success")
        return redirect(url_for("auth.login"))

    return render_template("register.html", roles=REGISTERABLE_ROLES)


@auth_bp.route("/auth/login", methods=["GET", "POST"])
def login():
    """账号密码登录：校验通过后写入 session，携带角色与权限信息。"""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter_by(username=username).first()
        # 用户不存在或密码错误时统一提示，避免泄露账号是否存在
        if user is None or not user.check_password(password):
            flash("用户名或密码错误", "error")
            return render_template("login.html")

        session["user_id"] = user.id
        session["role_name"] = user.role.name
        session["permissions"] = [p.name for p in user.role.permissions]
        session["login_time"] = datetime.now(timezone.utc)
        flash(f"欢迎回来，{user.username}（{user.role.name}）", "success")
        return redirect(url_for("auth.dashboard"))

    return render_template("login.html")


@auth_bp.route("/auth/logout")
def logout():
    """登出：清空会话。"""
    session.clear()
    flash("已安全退出", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/auth/reset-password", methods=["GET", "POST"])
def reset_password():
    """重置密码（教学简化版）：验证用户名 + 旧密码后可设置新密码。

    真实系统应采用邮箱验证码或安全链接，此处为 MVP 教学版本。
    """
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        old_password = request.form.get("old_password", "")
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        user = User.query.filter_by(username=username).first()
        if user is None or not user.check_password(old_password):
            flash("用户名或旧密码不正确", "error")
            return render_template("reset_password.html")
        if len(new_password) < 6:
            flash("新密码至少需要 6 位", "error")
            return render_template("reset_password.html")
        if new_password != confirm:
            flash("两次输入的新密码不一致", "error")
            return render_template("reset_password.html")

        user.set_password(new_password)
        db.session.commit()
        flash("密码重置成功，请使用新密码登录", "success")
        return redirect(url_for("auth.login"))

    return render_template("reset_password.html")


@auth_bp.route("/")
def dashboard():
    """首页：未登录跳转登录页，已登录展示概览。"""
    if "user_id" not in session:
        return redirect(url_for("auth.login"))
    user = db.session.get(User, session["user_id"])
    return render_template("dashboard.html", user=user)
