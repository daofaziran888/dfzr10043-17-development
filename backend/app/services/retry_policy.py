"""重试策略服务：退避参数、放弃阈值与重试执行管理。

维护异常处置对应的重试策略，含退避基数、倍数和抖动区间，
达到放弃阈值后抑制并为下游写入审计轨迹。

历史规则回放把「回放复核输入」挂到策略上：它只作为重试策略的复核依据，
回放本身是只读比对，绝不递增或清零「累计重试」，在跑任务的次数不受影响。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService


class RetryPolicyService(BaseModuleService):
    MODULE = "retry_policy"
    ENTITY = "重试策略"
    OPERATION_TERM = "关联"
    LIST_FIELDS = [
        "策略编号", "关联规则", "最大次数", "退避基数", "退避倍数",
        "抖动区间", "放弃阈值", "累计重试", "回放复核输入",
    ]
    REQUIRED_FIELDS = ["策略编号", "关联规则", "最大次数"]
    STATUS_ORDER = ["草稿", "执行中", "已放弃", "已停用"]
    ACTION_RULES = {"启用策略": "执行中", "标记放弃": "已放弃", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    def policy_for_rule(self, rule_code: str, rule_version: str | None = None) -> dict[str, Any] | None:
        """按规则编号找到关联的重试策略；指定版本时取「规则编号@版本」精确匹配。"""
        items, _ = self.list_entries(size=10000)
        if rule_version is not None:
            versioned = [
                p for p in items
                if p.get("关联规则") == f"{rule_code}@v{rule_version}"
            ]
            if versioned:
                return versioned[-1]
        linked = [p for p in items if p.get("关联规则") == rule_code]
        return linked[-1] if linked else None

    def is_exhausted(self, policy: dict[str, Any] | None) -> bool:
        """判断策略次数是否耗尽：累计重试达到最大次数或放弃阈值。"""
        if policy is None:
            return True
        max_attempts = int(policy.get("最大次数", "3") or "3")
        current = int(policy.get("累计重试", "0") or "0")
        threshold = int(policy.get("放弃阈值", str(max_attempts)) or str(max_attempts))
        return current >= threshold or current >= max_attempts

    def attach_replay_review(self, policy_id: int, review: dict[str, Any]) -> dict[str, Any]:
        """把历史回放结果登记为复核输入。

        只追加复核记录，不修改累计重试/最大次数，保证回放不清零在跑任务的次数。
        """
        entry = self.get_entry(policy_id)
        if entry is None:
            return {"ok": False, "reason": "策略不存在"}
        history = entry.get("回放复核输入")
        reviews: list[dict[str, Any]] = history if isinstance(history, list) else []
        reviews.append(review)
        entry["回放复核输入"] = reviews
        return {"ok": True, "policy_id": policy_id, "复核条数": len(reviews)}

    def should_abandon(self, policy_id: int) -> bool:
        """判断重试是否应放弃：累计重试>=最大次数或达到放弃阈值。"""
        entry = self.get_entry(policy_id)
        if entry is None:
            return True
        return self.is_exhausted(entry)

    def calculate_backoff(self, policy_id: int, attempt: int) -> int:
        """计算退避等待毫秒数，含指数退避与抖动。"""
        entry = self.get_entry(policy_id)
        if entry is None:
            return 1000
        base = int(entry.get("退避基数", "500") or "500")
        multiplier = int(entry.get("退避倍数", "2") or "2")
        jitter_range = entry.get("抖动区间", "200") or "200"
        jitter = int(jitter_range)
        import random
        return base * (multiplier ** (attempt - 1)) + random.randint(0, jitter)

    def increment_retry(self, policy_id: int) -> dict[str, Any]:
        """递增累计重试次数。"""
        entry = self.get_entry(policy_id)
        if entry is None:
            return {"ok": False, "reason": "策略不存在"}
        current = int(entry.get("累计重试", "0") or "0")
        entry["累计重试"] = str(current + 1)
        return {"ok": True, "policy_id": policy_id, "total_retries": current + 1}
