# -*- coding: utf-8 -*-
"""演示数据初始化脚本（Sprint 3 发布演示用）。

用途：把 project.db 重置为一套"看起来像真实项目"的数据，覆盖三个阶段的故事、
不同状态/优先级/负责人、逾期任务、评论与状态变更记录、有效/过期邀请码，
便于评审现场演示趋势图、负载视图与报表导出。

警告：会清空当前数据库（仅用于演示/截图环境）。
运行：
    python scripts/seed_demo.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import _seed_default_data, _sync_permissions, create_app  # noqa: E402
from app.models import (  # noqa: E402
    Comment, Invite, Role, Task, TaskActivity, User, db,
)

PASSWORD = "sprint3"


def _utc(days_ago=0, hours_ago=0):
    return datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)


def _user(username, role_name, email):
    role = Role.query.filter_by(name=role_name).one()
    user = User(username=username, email=email, role_id=role.id)
    user.set_password(PASSWORD)
    db.session.add(user)
    db.session.flush()
    return user


def _task(title, status, priority, assignee, created_days, due_days=None,
          description="", comments=None):
    task = Task(
        title=title,
        status=status,
        priority=priority,
        assignee=assignee,
        description=description,
        created_at=_utc(days_ago=created_days),
        due_date=(datetime.now(timezone.utc) + timedelta(days=due_days)).date()
        if due_days is not None else None,
    )
    db.session.add(task)
    db.session.flush()

    db.session.add(
        TaskActivity(
            task=task, actor=assignee, action="created", to_status="pending",
            created_at=task.created_at,
        )
    )
    if status in ("in_progress", "done"):
        started = task.created_at + timedelta(days=1)
        db.session.add(
            TaskActivity(
                task=task, actor=assignee, action="status",
                from_status="pending", to_status="in_progress", created_at=started,
            )
        )
    if status == "done":
        done_at = task.created_at + timedelta(days=max(2, min(5, created_days)))
        if done_at > datetime.now(timezone.utc):
            done_at = datetime.now(timezone.utc) - timedelta(hours=3)
        db.session.add(
            TaskActivity(
                task=task, actor=assignee, action="status",
                from_status="in_progress", to_status="done", created_at=done_at,
            )
        )

    for content, author, days_ago in comments or []:
        db.session.add(
            Comment(
                task=task, author=author, content=content,
                created_at=_utc(days_ago=days_ago),
            )
        )
    return task


def seed():
    app = create_app()
    with app.app_context():
        db.drop_all()
        db.create_all()
        _seed_default_data()
        _sync_permissions()

        admin = User.query.filter_by(username="admin").one()
        gaoyibo = _user("高一博", "管理员", "gaoyibo@example.com")
        sanjunyan = _user("伞俊岩", "项目经理", "sanjunyan@example.com")
        jiangyuxiang = _user("姜羽翔", "项目经理", "jiangyuxiang@example.com")
        xuerui = _user("薛睿", "团队成员", "xuerui@example.com")
        db.session.commit()

        # Sprint 1 已交付（5 条）
        _task("设计用户/角色/权限/任务数据模型", "done", 1, gaoyibo, 24, -18,
              "四张表 + 关联关系，权限按名称校验", [("模型评审通过，补了唯一索引。", xuerui, 23)])
        _task("实现注册、登录、登出与密码重置", "done", 1, jiangyuxiang, 23, -17,
              "密码加盐哈希；错误提示不泄露账号是否存在", [("登录失败提示统一为'用户名或密码错误'。", jiangyuxiang, 22)])
        _task("角色创建与权限分配页面", "done", 1, gaoyibo, 21, -15,
              "管理员可创建角色、勾选权限、删除空角色", [])
        _task("任务创建、负责人指派、优先级与截止时间", "done", 2, sanjunyan, 20, -14,
              "任务列表按优先级排序", [("截止日期用日期控件，避免手输格式错。", sanjunyan, 19)])
        _task("Sprint 1 关键路径用例（15 条）", "done", 2, xuerui, 19, -13,
              "覆盖登录/注册/角色/任务", [])

        # Sprint 2 已交付（6 条）
        _task("成员邀请码生成、撤销与凭码注册", "done", 1, xuerui, 16, -10,
              "邀请码一次性、预设角色、可撤销", [("邀请了 3 位同学，其中 1 个码过期后补发。", sanjunyan, 12)])
        _task("成员列表、角色调整与移除保护", "done", 2, xuerui, 15, -9,
              "不能改自己、不能移除最后一个管理员", [])
        _task("任务状态三态流转与权限判断", "done", 1, jiangyuxiang, 14, -8,
              "负责人本人或 task.assign 可改", [("越权改状态返回 403，已补用例。", jiangyuxiang, 13)])
        _task("任务列表按状态筛选与计数", "done", 2, jiangyuxiang, 13, -7,
              "非法 status 回退为全部", [])
        _task("项目看板三列视图与统计接口", "done", 1, sanjunyan, 12, -6,
              "未开始/进行中/完成三列", [("看板计数与列表筛选口径必须一致。", xuerui, 11)])
        _task("Sprint 2 集成与回归用例（29 条）", "done", 2, gaoyibo, 11, -5,
              "跨模块链路与权限回归", [])

        # Sprint 3 已交付（8 条）
        t_comment = _task("任务评论与活动时间线", "done", 1, sanjunyan, 8, -2,
                          "评论 + 状态变更合并为一条时间线",
                          [("评论不能编辑，删除保留操作提示；XSS 用 Jinja 自动转义。", gaoyibo, 6),
                           ("时间线排序用 UTC 归一化，避免 naive/aware 混用。", jiangyuxiang, 5)])
        _task("任务状态变更记录（US-14，评审会新增）", "done", 1, jiangyuxiang, 7, -1,
              "谁、从什么状态、到什么状态、何时",
              [("重开再完成只记一条完成时间，趋势不会翻倍。", gaoyibo, 4)])
        _task("成员负载视图（优先级加权）", "done", 2, xuerui, 6, 0,
              "高=3/中=2/低=1，逾期优先提示",
              [("先按点数看，再看条数；未指派单独统计。", sanjunyan, 3)])
        _task("项目趋势监控与累计燃起图", "done", 1, gaoyibo, 5, 1,
              "服务端 SVG，不依赖外部 CDN",
              [("燃起图两条线：范围与完成，差距就是未交付范围。", jiangyuxiang, 2)])
        _task("任务报表 CSV 导出（UTF-8 BOM）", "done", 2, xuerui, 4, 2,
              "管理员/项目经理可导出，普通成员 403", [])
        _task("看板 SSE 实时推送（清偿 TD-01）", "done", 1, sanjunyan, 3, 2,
              "EventSource + 自动重载卡片 + 轮询降级",
              [("生成器里必须重新进 app_context，否则第二次推送 500。", gaoyibo, 1)])
        _task("邀请码 7 天有效期（清偿 TD-05）", "done", 2, xuerui, 3, 2,
              "过期显示'已过期'，注册被拒", [])
        _task("权限实时生效（清偿 TD-02）", "done", 2, jiangyuxiang, 2, 3,
              "before_request 重新查库，收回权限无需重登", [])

        # 发布收尾（进行中/未完成，用于展示负载与风险）
        _task("发布就绪检查清单逐项确认", "in_progress", 1, gaoyibo, 2, 1,
              "12 项逐条确认，未通过项不得计入发布范围",
              [("第 5、6 项技术债处置需要 DRI 最终确认。", xuerui, 0)])
        _task("发布前全量回归复跑", "in_progress", 1, gaoyibo, 1, 0,
              "80 条用例 + 两条端到端旅程在发布候选上复跑")
        _task("用户手册与发布说明定稿", "in_progress", 2, xuerui, 2, -1,
              "与实现逐条对照，禁止写未实现功能", [])
        _task("端到端异常/权限旅程回归", "in_progress", 1, jiangyuxiang, 1, 0,
              "越权导出、越权改任务、过期邀请码", [("已跑通 5 条负向路径，剩 1 条待补录屏。", jiangyuxiang, 0)])
        _task("发布演示脚本与彩排", "in_progress", 2, sanjunyan, 1, 0,
              "按一条用户旅程演示，不逐功能点罗列",
              [("演示账号与数据已固定，彩排两遍。", sanjunyan, 0)])
        _task("技术债台账与发布边界复核", "in_progress", 1, xuerui, 1, 0,
              "TD-01/02/04/05 清偿证据，TD-03/06/07/08 结转理由")
        _task("发布风险复盘与预案", "in_progress", 2, gaoyibo, 1, 1,
              "演示失败、环境异常、教师临时加要求三类预案")
        _task("演示环境与账户检查", "done", 2, sanjunyan, 1, 1,
              "管理员/项目经理/成员三类账户各一", [])

        # 发布后待办（明确不计入 v3.0 范围）
        _task("移动端响应式适配", "pending", 3, sanjunyan, 1, 12,
              "进阶 2 候选：看板与列表在小屏可用")
        _task("邮件/站内通知", "pending", 2, sanjunyan, 1, 15,
              "进阶 2 候选：任务指派与评论提醒")
        _task("任务附件上传", "pending", 3, jiangyuxiang, 1, 18,
              "实验一故事地图中的 Could 项，v3.0 未纳入")

        # 邀请码：有效 / 已过期 / 已撤销
        db.session.add_all([
            Invite(code="A1B2C3D4", role=Role.query.filter_by(name="团队成员").one(),
                   invited_by=gaoyibo, status="pending",
                   created_at=_utc(days_ago=1), expires_at=_utc(days_ago=-6)),
            Invite(code="E5F6A7B8", role=Role.query.filter_by(name="项目经理").one(),
                   invited_by=gaoyibo, status="pending",
                   created_at=_utc(days_ago=10), expires_at=_utc(days_ago=3)),
            Invite(code="C9D0E1F2", role=Role.query.filter_by(name="团队成员").one(),
                   invited_by=gaoyibo, status="revoked",
                   created_at=_utc(days_ago=4), expires_at=_utc(days_ago=-3)),
        ])
        db.session.commit()

        total = Task.query.count()
        done = Task.query.filter_by(status="done").count()
        print("演示数据已生成：%d 个用户，%d 项任务（完成 %d，完成率 %.1f%%），%d 条评论"
              % (User.query.count(), total, done, done * 100.0 / total,
                 Comment.query.count()))
        print("账号（密码均为 %s）：" % PASSWORD)
        for user in User.query.order_by(User.id).all():
            print("  %-8s / %s" % (user.username, user.role.name))
        print("  管理员：admin / admin123（Sprint 1 预置账号）")


if __name__ == "__main__":
    seed()
