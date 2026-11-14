# -*- coding: utf-8 -*-
"""数据模型模块：用户、角色、权限、任务、邀请、评论、任务活动七张表。

Sprint 1：User / Role / Permission / Task
Sprint 2：Invite（邀请码）
Sprint 3：Comment（任务评论）、TaskActivity（任务状态变更记录），
          并为 Invite 增加 expires_at（邀请码有效期，清偿 TD-05）。

关系：
- User.role_id          -> Role（用户属于一个角色）
- Role.permissions      -> Permission（多对多）
- Task.assignee_id      -> User（任务负责人）
- Invite.role_id        -> Role（邀请码预设角色）
- Comment.task_id       -> Task（任务下的讨论）
- Comment.author_id     -> User（评论作者）
- TaskActivity.task_id  -> Task（任务的时间线事件）
- TaskActivity.actor_id -> User（谁触发了事件）
"""
from datetime import date, datetime, timedelta, timezone

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

# 任务状态可选值（Sprint 2 起状态更新、筛选、看板分列共用同一份枚举）
STATUS_CHOICES = [
    ("pending", "未开始"),
    ("in_progress", "进行中"),
    ("done", "完成"),
]
STATUS_VALUES = [value for value, _label in STATUS_CHOICES]


def utcnow():
    """统一的时间入口：数据库里存 UTC（aware），SQLite 读回后为 naive。

    展示层用 ``_localtime`` 过滤器加 8 小时；过期判断用 ``as_utc`` 归一化，
    避免 naive/aware 混用导致 TypeError。
    """
    return datetime.now(timezone.utc)


def as_utc(value):
    """把数据库读回的 naive 时间按 UTC 解释，便于与 utcnow() 比较。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# 角色与权限的多对多关联表
role_permissions = db.Table(
    "role_permissions",
    db.Column("role_id", db.Integer, db.ForeignKey("role.id"), primary_key=True),
    db.Column("permission_id", db.Integer, db.ForeignKey("permission.id"), primary_key=True),
)


class User(db.Model):
    """用户表：账号、邮箱、密码哈希与所属角色。"""
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    role_id = db.Column(db.Integer, db.ForeignKey("role.id"), nullable=False)

    role = db.relationship("Role", backref="users", lazy="joined")

    def set_password(self, raw):
        """设置密码（存储加盐哈希，不保存明文）。"""
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        """校验密码是否正确。"""
        return check_password_hash(self.password_hash, raw)


class Role(db.Model):
    """角色表：名称、描述与关联权限集合。"""
    __tablename__ = "role"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.String(200), default="")
    permissions = db.relationship(
        "Permission", secondary=role_permissions, backref="roles", lazy="joined"
    )

    def has_permission(self, perm_name):
        """判断角色是否拥有指定权限（按权限名）。"""
        return any(p.name == perm_name for p in self.permissions)


class Permission(db.Model):
    """权限表：预定义权限（role.manage / task.assign / member.manage / report.export）。"""
    __tablename__ = "permission"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)


class Task(db.Model):
    """任务表：标题、描述、优先级、截止时间、状态与负责人。

    优先级：1=高 2=中 3=低
    状态：pending=未开始 / in_progress=进行中 / done=完成
    """
    __tablename__ = "task"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    priority = db.Column(db.Integer, default=2, nullable=False)
    due_date = db.Column(db.Date)
    status = db.Column(db.String(20), default="pending", nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    assignee_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    assignee = db.relationship("User", backref="tasks", lazy="joined")

    @property
    def priority_label(self):
        """优先级的中文展示名。"""
        return {1: "高", 2: "中", 3: "低"}.get(self.priority, "中")

    @property
    def status_label(self):
        """状态的中文展示名。"""
        return {"pending": "未开始", "in_progress": "进行中", "done": "完成"}.get(
            self.status, self.status
        )

    @property
    def status_css(self):
        """状态标签的样式类（任务列表/看板配色）。"""
        return {
            "pending": "s-pending",
            "in_progress": "s-progress",
            "done": "s-done",
        }.get(self.status, "s-pending")

    @property
    def is_overdue(self):
        """是否逾期：有截止时间、未完成、且已早于今天。"""
        return bool(
            self.due_date and self.status != "done" and self.due_date < date.today()
        )

    @property
    def is_open(self):
        """是否为未完成任务（Sprint 3：负载与趋势统计共用口径）。"""
        return self.status != "done"

    @property
    def weight(self):
        """优先级加权负载（Sprint 3）：高=3、中=2、低=1，用于成员负载视图。

        用"点数"而不是"任务条数"衡量负载，避免把 3 条低优先级任务
        误判成比 1 条高优先级任务更重。
        """
        return {1: 3, 2: 2, 3: 1}.get(self.priority, 2)

    def can_update_status(self, user, permissions):
        """判断某用户能否更新本任务的状态。

        规则：任务负责人本人可以更新；拥有 task.assign 权限的人（项目经理及以上）
        可以更新任意任务。页面渲染与路由共用这一处判断。
        """
        if user is None:
            return False
        if "task.assign" in (permissions or []):
            return True
        return self.assignee_id == user.id


class Invite(db.Model):
    """邀请表：管理员生成邀请码，新成员凭码注册并自动加入团队。

    状态机：pending（待使用）-> accepted（已被使用，一次性）
                              -> revoked（管理员手动撤销）
    Sprint 3 增加 expires_at（默认 7 天）：到期的 pending 邀请不可再使用，
    在页面上显示为"已过期"，用于清偿 TD-05。
    """
    __tablename__ = "invite"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey("role.id"), nullable=False)
    invited_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    expires_at = db.Column(db.DateTime)
    accepted_at = db.Column(db.DateTime)
    accepted_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    # 同一张表有两个 user 外键，必须显式声明 foreign_keys
    role = db.relationship("Role", lazy="joined")
    invited_by = db.relationship(
        "User", foreign_keys=[invited_by_id], backref="invites_sent", lazy="joined"
    )
    accepted_by = db.relationship("User", foreign_keys=[accepted_by_id], lazy="joined")

    @property
    def is_expired(self):
        """是否已过期。没有 expires_at 的历史邀请视为不过期（迁移兼容）。"""
        if self.expires_at is None:
            return False
        return as_utc(self.expires_at) <= utcnow()

    @property
    def is_usable(self):
        """是否还可用于注册：待使用且未过期。"""
        return self.status == "pending" and not self.is_expired

    @property
    def days_left(self):
        """剩余有效天数（向上取整，最小 0）；无有效期返回 None。"""
        if self.expires_at is None:
            return None
        delta = as_utc(self.expires_at) - utcnow()
        if delta.total_seconds() <= 0:
            return 0
        return max(1, int((delta.total_seconds() + 86399) // 86400))

    @property
    def status_label(self):
        """邀请状态的中文展示名（过期优先于待使用）。"""
        if self.status == "pending" and self.is_expired:
            return "已过期"
        return {
            "pending": "待使用",
            "accepted": "已使用",
            "revoked": "已撤销",
        }.get(self.status, self.status)


class Comment(db.Model):
    """任务评论（Sprint 3 / US-12）：团队成员在任务下的讨论记录。

    设计取舍：
    - 只支持纯文本，Jinja 自动转义，不引入富文本编辑器（XSS 面更小）；
    - 评论不可编辑，只能由作者本人或管理员删除，保留"说过什么"的可追溯性；
    - 与 TaskActivity 分表存放，时间线在查询层合并，避免评论与状态事件互相污染。
    """
    __tablename__ = "comment"

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    task = db.relationship(
        "Task",
        backref=db.backref(
            "comments",
            cascade="all, delete-orphan",
            order_by="Comment.created_at",
        ),
    )
    author = db.relationship("User", lazy="joined")

    MAX_LENGTH = 500


class TaskActivity(db.Model):
    """任务活动记录（Sprint 3 / US-14）：谁在什么时候把任务从什么状态改成了什么。

    这是评审会上教师提出的新故事：Sprint 2 只有"当前状态"，没有"状态怎么来的"，
    出了问题无法回溯。本轮只记录状态变更与任务创建两类事件；评论走 Comment 表。
    """
    __tablename__ = "task_activity"

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id"), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    action = db.Column(db.String(20), default="status", nullable=False)
    from_status = db.Column(db.String(20))
    to_status = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    task = db.relationship(
        "Task",
        backref=db.backref(
            "activities",
            cascade="all, delete-orphan",
            order_by="TaskActivity.created_at",
        ),
    )
    actor = db.relationship("User", lazy="joined")

    @property
    def action_label(self):
        """事件的中文描述，供时间线直接渲染。"""
        if self.action == "created":
            return "创建了任务"
        if self.action == "status":
            old = dict(STATUS_CHOICES).get(self.from_status, self.from_status or "—")
            new = dict(STATUS_CHOICES).get(self.to_status, self.to_status or "—")
            return f"将状态由「{old}」改为「{new}」"
        return self.action
