"""异常规则服务：识别特征匹配、判定条件评估与处置动作执行。

把流程运行中的失败分类转成可执行的动作（重试/抑制/废弃），
按生效优先级决定匹配顺序，高优先级规则优先命中。

历史规则回放依赖规则的版本维度：同一规则编号可以有多个「规则版本」，
并通过「适用模板」绑定流程模板、「恢复错误类别」声明待启版本可恢复的错误。
「撤回规则」用于拟启版本在派发前被撤回的场景，撤回后历史恢复授权同步失效。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.base import BaseModuleService


class ExceptionRuleService(BaseModuleService):
    MODULE = "exception_rule"
    ENTITY = "处置规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "规则编号", "规则版本", "适用模板", "匹配特征", "判定条件", "处置动作",
        "适用范围", "恢复错误类别", "生效优先级", "命中次数", "最近命中",
    ]
    REQUIRED_FIELDS = ["规则编号", "匹配特征", "判定条件"]
    STATUS_ORDER = ["草稿", "已生效", "已抑制", "已废弃"]
    ACTION_RULES = {
        "启效规则": "已生效",
        "抑制规则": "已抑制",
        "废弃规则": "已废弃",
        "撤回规则": "已抑制",
    }
    NEGATIVE_ACTIONS = ["撤回规则"]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        result = super().run_action(entry_id, action)
        if action == "撤回规则" and result[0] is not None:
            result[0]["撤回时间"] = datetime.now(
                timezone(timedelta(hours=8))
            ).isoformat(timespec="seconds")
        return result

    def match_exception(self, signature: str) -> dict[str, Any] | None:
        """用异常特征匹配最优先的已生效规则。"""
        items, _ = self.list_entries(size=10000)
        active = [r for r in items if r.get("status") == "已生效"]
        active.sort(key=lambda r: int(r.get("生效优先级", "0") or "0"), reverse=True)
        for rule in active:
            feature = rule.get("匹配特征", "")
            if feature and signature.find(feature) >= 0:
                return {"rule": rule, "matched": True}
        return None

    def record_hit(self, rule_id: int) -> dict[str, Any]:
        """记录规则命中并更新命中次数与最近命中时间。"""
        entry = self.get_entry(rule_id)
        if entry is None:
            return {"ok": False, "reason": "规则不存在"}
        hits = int(entry.get("命中次数", "0") or "0")
        entry["命中次数"] = str(hits + 1)
        entry["最近命中"] = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        return {"ok": True, "rule_id": rule_id, "total_hits": hits + 1}

    def get_rules_by_scope(self, scope: str) -> list[dict[str, Any]]:
        """按适用范围筛选已生效规则（如定位、字段、超时等）。"""
        items, _ = self.list_entries(size=10000)
        active = [r for r in items if r.get("status") == "已生效"]
        return [r for r in active if scope in str(r.get("适用范围", ""))]

    def list_versions(self, rule_code: str) -> list[dict[str, Any]]:
        """列出同一规则编号下的全部版本，按版本号倒序，供历史回放比对。"""
        items, _ = self.list_entries(size=10000)
        versions = [r for r in items if r.get("规则编号") == rule_code]
        versions.sort(key=lambda r: int(r.get("规则版本", "1") or "1"), reverse=True)
        return versions
