# -*- coding: utf-8 -*-
"""应用工厂模块：创建 Flask 应用、初始化数据库与默认数据。

Sprint 1：默认角色/权限、管理员账号、认证与任务骨架。
Sprint 2：新增 member.manage 权限、members 蓝图、幂等权限同步。
Sprint 3（凤凰）：
- 新增 report.export 权限（报表导出，授予管理员与项目经理）；
- 新增 analytics 蓝图（趋势监控、成员负载、CSV 报表）；
- 新增 Comment / TaskActivity 两张表，Invite 增加 expires_at；
- 新增 _migrate_schema()：对 Sprint 2 遗留 project.db 做增量迁移
  （create_all 只建新表，不会给旧表加列，这是本轮最典型的集成点）；
- 新增 before_request 会话权限刷新：清偿 TD-02（权限快照需重新登录才生效）。
"""
import os
from datetime import timedelta

from flask import Flask, g, session
from .models import db

# 预定义权限清单（名称 -> 中文说明）
PERMISSIONS = {
    "role.manage": "角色与权限管理（创建/编辑/删除角色）",
    "task.assign": "任务创建与分配（设置负责人/优先级/截止时间）",
    "member.manage": "成员管理（邀请成员/调整角色/移除成员）",
    # Sprint 3 新增：报表导出属于"对外交付数据"的动作，单独授权，避免所有成员都能导出
    "report.export": "项目报表导出（CSV）",
}

DEFAULT_ROLES = [
    # (角色名, 描述, 权限列表)
    (
        "管理员",
        "系统最高权限，管理角色、权限、成员与报表",
        ["role.manage", "task.assign", "member.manage", "report.export"],
    ),
    ("项目经理", "负责项目规划、任务分配与报表汇报", ["task.assign", "report.export"]),
    ("团队成员", "普通成员，执行被分配的任务并参与讨论", []),
]


# 数据库统一以 UTC 存储，展示层统一转换为本地时间。
# 教学环境固定东八区（+08:00）；若将来多时区部署，再改为按用户配置转换。
LOCAL_TZ_OFFSET = timedelta(hours=8)


def _localtime(value, fmt="%Y-%m-%d %H:%M"):
    """Jinja 过滤器：UTC 时间戳 → 本地时间字符串。"""
    if value is None:
        return "—"
    return (value + LOCAL_TZ_OFFSET).strftime(fmt)


def create_app(testing=None, database_uri=None):
    """应用工厂：构建并配置 Flask 应用。

    :param testing: 为 True 时使用内存数据库，便于单元测试
    :param database_uri: 显式指定数据库（迁移演练/演示环境用），优先级最高
    """
    app = Flask(__name__)
    base_dir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    app.config["SECRET_KEY"] = os.environ.get(
        "AIMANAGE_SECRET_KEY", "sprint3-phoenix-secret"
    )
    if database_uri is not None:
        app.config["SQLALCHEMY_DATABASE_URI"] = database_uri
    elif testing:
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    else:
        # 支持通过环境变量切到 PostgreSQL（TD-03 的缓解措施，见 README）
        app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
            "AIMANAGE_DATABASE_URL",
            "sqlite:///" + os.path.join(base_dir, "project.db"),
        )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["TESTING"] = bool(testing)

    db.init_app(app)

    # 注册各功能模块蓝图
    from .auth import auth_bp
    from .roles import roles_bp
    from .tasks import tasks_bp
    from .members import members_bp
    from .board import board_bp
    from .analytics import analytics_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(roles_bp, url_prefix="/roles")
    app.register_blueprint(tasks_bp, url_prefix="/tasks")
    app.register_blueprint(members_bp, url_prefix="/members")
    app.register_blueprint(board_bp, url_prefix="/board")
    app.register_blueprint(analytics_bp, url_prefix="/analytics")

    with app.app_context():
        db.create_all()
        # 集成点 I-1：Sprint 2 的 project.db 里 invite 表没有 expires_at，
        # db.create_all() 不会补列，必须显式 ALTER TABLE。
        _migrate_schema()
        _seed_default_data()
        _sync_permissions()
        _enable_sqlite_wal(testing, database_uri)

    app.add_template_filter(_localtime, "localtime")
    _register_session_refresh(app)
    _register_error_handlers(app)

    return app


def _register_session_refresh(app):
    """每个请求按 user_id 重新读取角色与权限（清偿 TD-02）。

    Sprint 1/2 把 permissions 快照写进 session，管理员收回权限后，
    已登录用户必须重新登录才失效——这是一个真实的安全窗口。
    这里在 before_request 中重新查库并刷新 session，代价是每请求一次主键查询
    （SQLite 本地 < 1ms）。若将来 QPS 变高，可改为"权限版本号 + 缓存"，
    见技术债登记 TD-06 的演进说明。
    """
    from .models import User

    @app.before_request
    def _load_current_user():
        uid = session.get("user_id")
        if not uid:
            return None
        user = db.session.get(User, uid)
        if user is None:
            # 账号被移除后，旧会话立即失效，避免"幽灵登录"
            session.clear()
            return None
        g.current_user = user
        g.current_permissions = [p.name for p in user.role.permissions]
        session["role_name"] = user.role.name
        session["permissions"] = g.current_permissions
        return None


def _register_error_handlers(app):
    """注册友好错误页：权限不足(403)/页面不存在(404)/服务器错误(500)。"""
    from flask import render_template

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403,
                               message="当前角色没有该操作的权限，请联系管理员分配权限。"), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", code=404,
                               message="页面不存在，请检查地址或返回登录页。"), 404

    @app.errorhandler(500)
    def server_error(e):
        return render_template("error.html", code=500,
                               message="服务器内部错误，请稍后重试。"), 500


def _migrate_schema():
    """对旧数据库做最小增量迁移（幂等，可重复执行）。

    为什么需要：SQLAlchemy 的 create_all() 只创建缺失的"表"，不会给已存在的表
    增加"列"。Sprint 2 的 project.db 已经存在 invite 表，Sprint 3 新增的
    expires_at 列不会自动出现，注册时查询会直接报 "no such column"。
    这里只做加法（ADD COLUMN），不删不改，失败可安全重试。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    tables = inspector.get_table_names()
    if "invite" not in tables:
        return
    columns = {c["name"] for c in inspector.get_columns("invite")}
    if "expires_at" not in columns:
        db.session.execute(text("ALTER TABLE invite ADD COLUMN expires_at DATETIME"))
        db.session.commit()
        # 历史待使用邀请没有有效期：保持 NULL（视为不过期），
        # 由管理员决定撤销或等待其被使用，避免迁移时误伤。
        print("[migrate] invite.expires_at 已补齐（历史邀请保持不过期）")


def _enable_sqlite_wal(testing, database_uri):
    """SQLite 开启 WAL 与 busy_timeout（TD-03 的缓解措施，不改变技术债结论）。

    教学演示是单机多标签页并发，WAL 能显著降低 "database is locked" 概率；
    但它不能替代 PostgreSQL 的连接池与并发写能力，因此 TD-03 仍记为
    "带债发布"，在进阶 2 迁移 PostgreSQL。
    """
    from sqlalchemy import text

    if testing:
        return
    uri = database_uri or os.environ.get("AIMANAGE_DATABASE_URL", "")
    if uri.startswith("sqlite") or (not uri and "sqlite" in str(db.engine.url)):
        try:
            db.session.execute(text("PRAGMA journal_mode=WAL"))
            db.session.execute(text("PRAGMA busy_timeout=5000"))
            db.session.commit()
        except Exception as exc:  # 内存库/只读库不支持 WAL，不影响启动
            db.session.rollback()
            print("[sqlite] WAL 未启用：", exc)


def _seed_default_data():
    """初始化默认角色、权限与管理员账号（幂等：已有数据则跳过）。"""
    from .models import Permission, Role, User

    if Role.query.count() == 0:
        # 先建齐权限，再逐个建角色并关联，避免 autoflush 告警
        for pname in PERMISSIONS:
            if Permission.query.filter_by(name=pname).first() is None:
                db.session.add(Permission(name=pname))
        db.session.flush()

        for name, desc, perms in DEFAULT_ROLES:
            role = Role(name=name, description=desc)
            db.session.add(role)
            db.session.flush()
            for pname in perms:
                role.permissions.append(Permission.query.filter_by(name=pname).first())
        db.session.commit()

    # 预置管理员账号 admin / admin123（教学演示用）
    if User.query.filter_by(username="admin").first() is None:
        admin_role = Role.query.filter_by(name="管理员").first()
        admin = User(username="admin", email="admin@example.com", role=admin_role)
        admin.set_password("admin123")
        db.session.add(admin)
        db.session.commit()


def _sync_permissions():
    """幂等补齐权限清单，兼容 Sprint 1/2 遗留数据库。

    规则：
    1. 按 PERMISSIONS 清单补齐缺失的权限行；
    2. "管理员"始终拥有全部预定义权限；
    3. Sprint 3 新增 report.export：仅当"项目经理"角色仍保有 task.assign
       （说明没有被管理员改造成特殊角色）时默认授予，避免覆盖自定义配置。
    """
    from .models import Permission, Role

    changed = False
    for pname in PERMISSIONS:
        if Permission.query.filter_by(name=pname).first() is None:
            db.session.add(Permission(name=pname))
            changed = True
    db.session.flush()

    admin_role = Role.query.filter_by(name="管理员").first()
    if admin_role is not None:
        for pname in PERMISSIONS:
            perm = Permission.query.filter_by(name=pname).first()
            if perm is not None and not admin_role.has_permission(pname):
                admin_role.permissions.append(perm)
                changed = True

    pm_role = Role.query.filter_by(name="项目经理").first()
    if (
        pm_role is not None
        and pm_role.has_permission("task.assign")
        and not pm_role.has_permission("report.export")
    ):
        perm = Permission.query.filter_by(name="report.export").first()
        if perm is not None:
            pm_role.permissions.append(perm)
            changed = True

    if changed:
        # 只有确实发生变更时才提交，避免每次启动都写库
        db.session.commit()
