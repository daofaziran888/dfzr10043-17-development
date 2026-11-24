"""审计轨迹服务：变更记录、核查流转与封存归档。

记录流程发布、凭据轮换、批次确认等关键操作的变更摘要，
形成可封存核查的审计链。支持按操作类型和追溯标记查询。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.base import BaseModuleService


class AuditTrailService(BaseModuleService):
    MODULE = "audit_trail"
    ENTITY = "审计条目"
    OPERATION_TERM = "关联"
    LIST_FIELDS = [
        "条目编号", "操作对象", "操作类型", "操作者标识", "操作时间",
        "变更摘要", "关联执行", "追溯标记", "关联规则版本",
    ]
    REQUIRED_FIELDS = ["条目编号", "操作对象", "操作类型"]
    STATUS_ORDER = ["已记录", "待核查", "已核查", "已封存"]
    ACTION_RULES = {"提交核查": "待核查", "完成核查": "已核查", "封存条目": "已封存"}
    NEGATIVE_ACTIONS = []

    def query_by_trace(self, trace_mark: str) -> list[dict[str, Any]]:
        """按追溯标记查询关联审计条目。"""
        items, _ = self.list_entries(size=10000)
        return [it for it in items if str(it.get("追溯标记", "")) == trace_mark]

    def query_by_operation(self, operation_type: str) -> list[dict[str, Any]]:
        """按操作类型查询审计条目。"""
        items, _ = self.list_entries(size=10000)
        return [it for it in items if it.get("操作类型") == operation_type]

    def seal_verified_entries(self) -> dict[str, Any]:
        """封存所有已核查的审计条目。"""
        items, _ = self.list_entries(size=10000)
        sealed = 0
        for it in items:
            if it.get("status") == "已核查":
                self.run_action(it["id"], "封存条目")
                sealed += 1
        return {"ok": True, "sealed_count": sealed}

    def get_unreviewed_count(self) -> int:
        """获取待核查条目数量。"""
        items, _ = self.list_entries(size=10000)
        return sum(1 for it in items if it.get("status") == "待核查")