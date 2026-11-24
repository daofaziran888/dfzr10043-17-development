"""历史规则回放服务：从历史采集事件检索候选规则，核对处置差异并申请恢复。

异常规则原本只能查看现行处理条件，缺少「从历史事件里找回可恢复处置」的入口。
本服务补齐这条链路：

1. 登记历史采集故障事件（按模板编号 + 事件签名定位）；
2. 按模板筛选历史事件做规则回放，用事件签名同时定位「原规则版本」与
   「待启规则版本」，输出两版规则的冲突结论（原规则次数耗尽 vs 待启规则
   把同一事件归入仍可处理的错误类别）；
3. 值班员核对差异后提交恢复申请——同一份差异重复提交只产生一个申请；
4. 审批通过产生恢复授权与领用记录；拟启规则在派发前被撤回时，授权同步失效，
   已撤授权不能用于实际派发，也不会产生新的采集；
5. 只能重放选定模板的历史错误，回放全程不清零在跑任务的累计重试次数；
6. 原审批、原失败与撤回/派发结果都写入审计轨迹，可随时复查；回放结果作为
   重试策略的复核输入回流给 retry_policy。

数据落在内存 store 的三张独立表上，与既有 22 个业务模块风格一致。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

_TZ = timezone(timedelta(hours=8))


def _service_for(key: str):
    """延迟导入，避免与 services 包初始化形成循环依赖。"""
    from app.services import service_for
    return service_for(key)

# 恢复申请状态机
APPLICATION_STATUSES = ["待审批", "已批准", "已驳回", "已撤回"]
# 授权/领用状态机：授权批准后可领用，规则撤回则授权失效
GRANT_STATUSES = ["有效", "已失效"]
CLAIM_STATUSES = ["已领用", "已继续", "已阻断"]


def _now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


class RuleReplayService(BaseModuleService):
    """历史规则回放、差异核对、恢复申请与授权领用的领域服务。"""

    MODULE = "rule_replay_event"
    ENTITY = "回放事件"
    OPERATION_TERM = "回放"

    # ── 通用资源接口需要的最小骨架（三张表各自有独立访问方法） ──────────────
    LIST_FIELDS = [
        "事件编号", "模板编号", "事件签名", "错误类别", "原规则编号",
        "原尝试次数", "采集时间",
    ]
    REQUIRED_FIELDS = ["事件编号", "模板编号", "事件签名"]
    STATUS_ORDER = ["已登记", "已回放", "已恢复", "已关闭"]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []

    # ── 表访问 ──────────────────────────────────────────────────────────────

    @property
    def events(self) -> list[dict[str, Any]]:
        return store.rows("rule_replay_event")

    @property
    def applications(self) -> list[dict[str, Any]]:
        return store.rows("rule_recovery_application")

    @property
    def claims(self) -> list[dict[str, Any]]:
        return store.rows("recovery_claim")

    # ── 历史事件登记 ────────────────────────────────────────────────────────

    def register_event(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记一笔历史采集故障事件。

        同一模板下事件签名唯一：重复登记直接返回既有事件，保证「同一份差异」
        有稳定的锚点，也让重复提交恢复申请时可以判重。
        """
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing

        existed = self.find_event(values["模板编号"], values["事件签名"])
        if existed is not None:
            return existed, []

        entry = {
            "id": _next_id(self.events),
            "status": "已登记",
            "pending": True,
            "abnormal": False,
            "事件编号": values["事件编号"],
            "模板编号": values["模板编号"],
            "事件签名": values["事件签名"],
            "错误类别": values.get("错误类别", "未分类错误"),
            "原规则编号": values.get("原规则编号", ""),
            "原尝试次数": str(values.get("原尝试次数", 0) or 0),
            "采集时间": values.get("采集时间", _now_iso()),
        }
        self.events.append(entry)
        return entry, []

    def find_event(self, template_code: str, signature: str) -> dict[str, Any] | None:
        for row in self.events:
            if row.get("模板编号") == template_code and row.get("事件签名") == signature:
                return row
        return None

    def list_events(self, template_code: str | None = None) -> list[dict[str, Any]]:
        """按模板筛选历史事件；不传模板则返回全部（调用方负责限定范围）。"""
        if not template_code:
            return [dict(row) for row in self.events]
        return [dict(row) for row in self.events if row.get("模板编号") == template_code]

    # ── 规则版本定位 ────────────────────────────────────────────────────────

    def _rule_versions(self, template_code: str, signature: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """用事件签名定位同一规则的两个版本：原规则与待启规则。

        规则必须绑定适用模板（适用模板字段）且匹配特征是签名子串才算命中。
        原规则取事件上记录的原规则编号；同编号里版本号更高的非废弃版本为待启版本。
        """
        event = self.find_event(template_code, signature)
        rule_svc = _service_for("exception_rule")
        all_rules, _ = rule_svc.list_entries(size=10000)
        candidates = [
            rule for rule in all_rules
            if template_code in str(rule.get("适用模板", ""))
            and str(rule.get("匹配特征", ""))
            and signature.find(str(rule.get("匹配特征", ""))) >= 0
        ]

        original: dict[str, Any] | None = None
        if event is not None:
            original = next(
                (rule for rule in candidates
                 if rule.get("规则编号") == event.get("原规则编号")),
                None,
            )
        if original is None and candidates:
            # 兜底：版本号最小的一版视为原规则
            original = sorted(candidates, key=lambda r: int(r.get("规则版本", "1") or "1"))[0]

        if original is None:
            return None, None
        family = [
            rule for rule in candidates
            if rule.get("规则编号") == original.get("规则编号")
        ]
        pending = [rule for rule in family if rule.get("status") != "已废弃"]
        pending.sort(key=lambda r: int(r.get("规则版本", "1") or "1"), reverse=True)
        candidate_rule = next((r for r in pending if r["id"] != original["id"]), None)
        return original, candidate_rule

    # ── 历史回放 ────────────────────────────────────────────────────────────

    def replay_template(self, template_code: str, signatures: list[str] | None = None) -> dict[str, Any]:
        """按模板筛选历史事件并回放，输出两版规则下的冲突结论。

        - 只重放选定模板的历史错误，其他模板的事件不参与；
        - 回放只读规则与策略，绝不修改在跑任务的累计重试次数；
        - 结果同时回流给重试策略作为复核输入。
        """
        scoped_events = self.list_events(template_code)
        if signatures:
            wanted = set(signatures)
            scoped_events = [ev for ev in scoped_events if ev.get("事件签名") in wanted]

        retry_svc = _service_for("retry_policy")
        results: list[dict[str, Any]] = []
        for event in scoped_events:
            signature = event["事件签名"]
            original, candidate = self._rule_versions(template_code, signature)
            if original is None or candidate is None:
                results.append({
                    "event_id": event["id"],
                    "事件签名": signature,
                    "可回放": False,
                    "原因": "未同时定位到原规则与待启规则版本",
                })
                continue

            original_policy = retry_svc.policy_for_rule(
                original["规则编号"], str(original.get("规则版本", "1")))
            candidate_policy = retry_svc.policy_for_rule(
                candidate["规则编号"], str(candidate.get("规则版本", "1")))
            original_exhausted = retry_svc.is_exhausted(original_policy) if original_policy else True
            # 拟启规则把原事件归入仍可处理的错误类别：类别命中且策略仍有剩余次数
            candidate_remaining = (
                int(candidate_policy.get("最大次数", "0") or "0")
                - int(candidate_policy.get("累计重试", "0") or "0")
            ) if candidate_policy else 0
            candidate_recoverable = (
                candidate.get("status") == "已生效"
                and event.get("错误类别") in str(candidate.get("恢复错误类别", ""))
                and candidate_remaining > 0
            )

            conflict = original_exhausted and candidate_recoverable
            diff = {
                "event_id": event["id"],
                "事件编号": event["事件编号"],
                "事件签名": signature,
                "可回放": True,
                "冲突结论": conflict,
                "原规则版本": {
                    "rule_id": original["id"],
                    "规则编号": original["规则编号"],
                    "规则版本": original.get("规则版本", "1"),
                    "处置动作": original.get("处置动作", ""),
                    "次数耗尽": original_exhausted,
                    "剩余次数": 0 if original_exhausted else (
                        int(original_policy.get("最大次数", "0") or "0")
                        - int(original_policy.get("累计重试", "0") or "0")
                    ),
                },
                "待启规则版本": {
                    "rule_id": candidate["id"],
                    "规则编号": candidate["规则编号"],
                    "规则版本": candidate.get("规则版本", "1"),
                    "处置动作": candidate.get("处置动作", ""),
                    "归入错误类别": event.get("错误类别"),
                    "仍可处理": candidate_recoverable,
                    "剩余次数": candidate_remaining,
                },
            }
            results.append(diff)

            # 回放结果成为重试策略的复核输入（只登记复核，不动累计重试）
            if original_policy is not None:
                retry_svc.attach_replay_review(
                    original_policy["id"],
                    {
                        "模板编号": template_code,
                        "事件签名": signature,
                        "原结论": "次数耗尽" if original_exhausted else "仍可重试",
                        "待启结论": "仍可处理" if candidate_recoverable else "不可恢复",
                        "回放时间": _now_iso(),
                    },
                )

            if conflict:
                event["status"] = "已回放"
                event["pending"] = True
                event["abnormal"] = True

        return {
            "模板编号": template_code,
            "回放事件数": len(results),
            "冲突差异数": sum(1 for r in results if r.get("冲突结论")),
            "结果": results,
        }

    # ── 恢复申请：同一差异只产生一个申请 ────────────────────────────────────

    def submit_application(self, template_code: str, signature: str,
                           operator: str = "值班员") -> dict[str, Any]:
        """对回放差异提交恢复申请；同一份差异重复提交只返回首个申请。"""
        event = self.find_event(template_code, signature)
        if event is None:
            return {"ok": False, "reason": "历史事件不存在，无法提交恢复申请"}
        original, candidate = self._rule_versions(template_code, signature)
        if original is None or candidate is None:
            return {"ok": False, "reason": "原规则或待启规则缺失，无法形成处置差异"}

        duplicate = next(
            (app for app in self.applications
             if app.get("模板编号") == template_code
             and app.get("事件签名") == signature
             and app.get("原规则ID") == original["id"]
             and app.get("待启规则ID") == candidate["id"]),
            None,
        )
        if duplicate is not None:
            return {"ok": True, "申请ID": duplicate["id"], "去重": True, "entry": duplicate}

        entry = {
            "id": _next_id(self.applications),
            "status": "待审批",
            "pending": True,
            "abnormal": False,
            "申请编号": f"RECV-{event['事件编号']}",
            "模板编号": template_code,
            "事件签名": signature,
            "事件ID": event["id"],
            "原规则ID": original["id"],
            "原规则编号": original["规则编号"],
            "原规则版本": original.get("规则版本", "1"),
            "待启规则ID": candidate["id"],
            "待启规则编号": candidate["规则编号"],
            "待启规则版本": candidate.get("规则版本", "1"),
            "处置差异": (
                f"原规则{original['规则编号']}v{original.get('规则版本', '1')}次数耗尽；"
                f"待启规则{candidate['规则编号']}v{candidate.get('规则版本', '1')}"
                f"将事件归入可处理错误类别"
            ),
            "申请人": operator,
            "申请时间": _now_iso(),
            "审批时间": "",
            "失效时间": "",
        }
        self.applications.append(entry)
        self._write_audit(
            operation="恢复申请提交",
            target=f"恢复申请.{entry['申请编号']}",
            operator=operator,
            summary=entry["处置差异"],
            mark=f"replay-apply-{entry['id']}",
        )
        return {"ok": True, "申请ID": entry["id"], "去重": False, "entry": entry}

    # ── 审批：批准产生授权与领用记录 ────────────────────────────────────────

    def approve_application(self, application_id: int, approver: str = "审批人") -> dict[str, Any]:
        entry = self._find_application(application_id)
        if entry is None:
            return {"ok": False, "reason": "恢复申请不存在"}
        if entry["status"] != "待审批":
            return {"ok": False, "reason": f"申请当前状态为{entry['status']}，不可审批"}

        candidate = _service_for("exception_rule").get_entry(entry["待启规则ID"])
        if candidate is None or candidate.get("status") != "已生效":
            entry["status"] = "已驳回"
            entry["pending"] = False
            return {"ok": False, "reason": "待启规则未处于已生效状态，驳回恢复申请"}

        entry["status"] = "已批准"
        entry["pending"] = False
        entry["审批时间"] = _now_iso()

        grant = {
            "id": _next_id(self.claims),
            "status": "有效",
            "pending": False,
            "abnormal": False,
            "授权编号": f"GRNT-{entry['申请编号']}",
            "申请ID": entry["id"],
            "模板编号": entry["模板编号"],
            "事件签名": entry["事件签名"],
            "待启规则ID": entry["待启规则ID"],
            "待启规则编号": entry["待启规则编号"],
            "待启规则版本": entry["待启规则版本"],
            "原规则ID": entry["原规则ID"],
            "原规则编号": entry["原规则编号"],
            "原规则版本": entry["原规则版本"],
            "授权状态": "有效",
            "授权时间": _now_iso(),
            "失效时间": "",
        }
        self.claims.append(grant)
        self._write_audit(
            operation="恢复申请批准",
            target=f"恢复授权.{grant['授权编号']}",
            operator=approver,
            summary=(f"批准{entry['申请编号']}，授权按待启规则"
                     f"{entry['待启规则编号']}v{entry['待启规则版本']}恢复事件{entry['事件签名']}"),
            mark=f"replay-approve-{entry['id']}",
            linked_rule=entry["待启规则编号"],
            rule_version=entry["待启规则版本"],
        )
        return {"ok": True, "entry": entry, "授权ID": grant["id"], "授权编号": grant["授权编号"]}

    def reject_application(self, application_id: int, approver: str = "审批人") -> dict[str, Any]:
        entry = self._find_application(application_id)
        if entry is None:
            return {"ok": False, "reason": "恢复申请不存在"}
        if entry["status"] != "待审批":
            return {"ok": False, "reason": f"申请当前状态为{entry['status']}，不可审批"}
        entry["status"] = "已驳回"
        entry["pending"] = False
        entry["审批时间"] = _now_iso()
        self._write_audit(
            operation="恢复申请驳回",
            target=f"恢复申请.{entry['申请编号']}",
            operator=approver,
            summary=f"驳回{entry['申请编号']}：事件{entry['事件签名']}不满足恢复条件",
            mark=f"replay-reject-{entry['id']}",
        )
        return {"ok": True, "entry": entry}

    # ── 规则撤回：派发前撤回，授权同步失效 ─────────────────────────────────

    def withdraw_rule(self, rule_id: int, operator: str = "值班主管") -> dict[str, Any]:
        """撤回拟启规则：所有挂在该规则上的有效授权立即失效。

        值班员手里可能还留着旧的可派结果，但已撤授权不能再用于实际派发。
        """
        rule_svc = _service_for("exception_rule")
        rule = rule_svc.get_entry(rule_id)
        if rule is None:
            return {"ok": False, "reason": "规则不存在"}
        rule_svc.run_action(rule_id, "撤回规则")

        invalidated: list[int] = []
        for grant in self.claims:
            if grant.get("待启规则ID") == rule_id and grant.get("授权状态") == "有效":
                grant["授权状态"] = "已失效"
                grant["status"] = "已失效"
                grant["失效时间"] = _now_iso()
                invalidated.append(grant["id"])
                linked = self._find_application(grant["申请ID"])
                if linked is not None and linked.get("status") == "已批准":
                    linked["status"] = "已撤回"
                    linked["失效时间"] = grant["失效时间"]
                self._write_audit(
                    operation="恢复授权失效",
                    target=f"恢复授权.{grant['授权编号']}",
                    operator=operator,
                    summary=(f"规则{rule.get('规则编号')}v{rule.get('规则版本', '1')}派发前撤回，"
                             f"授权{grant['授权编号']}同步失效，禁止再派发"),
                    mark=f"replay-withdraw-{grant['id']}",
                    linked_rule=rule.get("规则编号", ""),
                    rule_version=str(rule.get("规则版本", "1")),
                )
        return {"ok": True, "规则ID": rule_id, "失效授权数": len(invalidated), "失效授权": invalidated}

    # ── 领用与派发：只用实际有效的授权 ─────────────────────────────────────

    def claim_recovery(self, application_id: int, operator: str = "值班员") -> dict[str, Any]:
        """批准后领取恢复授权，取得领用记录；已撤回/失效的授权不能领用。"""
        entry = self._find_application(application_id)
        if entry is None:
            return {"ok": False, "reason": "恢复申请不存在"}
        if entry["status"] != "已批准":
            return {"ok": False, "reason": f"申请状态为{entry['status']}，尚无有效恢复授权"}
        grant = next((g for g in self.claims if g.get("申请ID") == application_id), None)
        if grant is None:
            return {"ok": False, "reason": "未找到恢复授权记录"}
        if grant["授权状态"] != "有效":
            return {"ok": False, "reason": "恢复授权已失效，不能领用"}

        claim_record = {
            "id": _next_id(self.claims),
            "status": "已领用",
            "pending": True,
            "abnormal": False,
            "领用编号": f"CLM-{entry['申请编号']}",
            "申请ID": entry["id"],
            "授权编号": grant["授权编号"],
            "模板编号": entry["模板编号"],
            "事件签名": entry["事件签名"],
            "待启规则编号": entry["待启规则编号"],
            "待启规则版本": entry["待启规则版本"],
            "原规则编号": entry["原规则编号"],
            "原规则版本": entry["原规则版本"],
            "原尝试": self._original_attempts(entry),
            "领用人": operator,
            "领用时间": _now_iso(),
            "领用状态": "已领用",
            "派发结果": "",
            "采集任务号": "",
        }
        self.claims.append(claim_record)
        self._write_audit(
            operation="恢复授权领用",
            target=f"领用记录.{claim_record['领用编号']}",
            operator=operator,
            summary=(f"值班员领用{grant['授权编号']}，关联原规则"
                     f"{entry['原规则编号']}v{entry['原规则版本']}与原尝试"
                     f"{claim_record['原尝试']}"),
            mark=f"replay-claim-{claim_record['id']}",
            linked_rule=entry["原规则编号"],
            rule_version=entry["原规则版本"],
        )
        return {"ok": True, "领用ID": claim_record["id"], "领用编号": claim_record["领用编号"],
                "entry": claim_record}

    def dispatch_recovery(self, claim_id: int) -> dict[str, Any]:
        """持领用记录发起补采派发。

        处置策略只认实际有效的恢复授权：授权已撤回则阻断，不产生新采集，
        也不清零任何在跑任务的次数；派发结果关联原规则版本与原尝试。
        """
        claim = next((c for c in self.claims if c.get("id") == claim_id), None)
        if claim is None:
            return {"ok": False, "reason": "领用记录不存在"}
        # 授权行与领用行同表，授权行带「授权状态」字段，领用行带「领用状态」字段
        grant = next((g for g in self.claims
                      if g.get("授权编号") == claim.get("授权编号")
                      and "授权状态" in g), None)
        if grant is None or grant.get("授权状态") != "有效":
            claim["领用状态"] = "已阻断"
            claim["status"] = "已阻断"
            claim["pending"] = False
            claim["派发结果"] = "阻断：授权已撤回或失效，未产生采集"
            self._write_audit(
                operation="恢复派发阻断",
                target=f"领用记录.{claim.get('领用编号')}",
                operator=claim.get("领用人", "值班员"),
                summary=(f"授权{claim.get('授权编号')}已失效，阻断补采派发，"
                         f"原规则{claim.get('原规则编号')}v{claim.get('原规则版本')}、"
                         f"原尝试{claim.get('原尝试')}保持可复查"),
                mark=f"replay-block-{claim['id']}",
                linked_rule=claim.get("原规则编号", ""),
                rule_version=claim.get("原规则版本", ""),
            )
            return {"ok": False, "reason": "恢复授权已撤回，不能用于实际派发", "采集任务号": ""}

        task_no = f"CAPT-REPLAY-{claim_id}-{_now_iso()[:10].replace('-', '')}"
        claim["领用状态"] = "已继续"
        claim["status"] = "已继续"
        claim["pending"] = False
        claim["派发结果"] = "已派发补采"
        claim["采集任务号"] = task_no
        self._write_audit(
            operation="恢复补采派发",
            target=f"领用记录.{claim['领用编号']}",
            operator=claim.get("领用人", "值班员"),
            summary=(f"按待启规则{claim.get('待启规则编号')}v{claim.get('待启规则版本')}派发补采"
                     f"{task_no}，关联原规则{claim.get('原规则编号')}v{claim.get('原规则版本')}"
                     f"与原尝试{claim.get('原尝试')}"),
            mark=f"replay-dispatch-{claim['id']}",
            linked_rule=claim.get("原规则编号", ""),
            rule_version=claim.get("原规则版本", ""),
        )
        return {"ok": True, "采集任务号": task_no, "entry": claim}

    def pause_pending_dispatch(self, claim_id: int) -> dict[str, Any]:
        """暂停尚未发出的补采：只对「已领用、未派发」的记录生效。"""
        claim = next((c for c in self.claims if c.get("id") == claim_id), None)
        if claim is None:
            return {"ok": False, "reason": "领用记录不存在"}
        if claim.get("领用状态") != "已领用":
            return {"ok": False, "reason": "补采已派发或已阻断，无法暂停"}
        claim["领用状态"] = "已暂停"
        claim["pending"] = True
        self._write_audit(
            operation="补采暂停",
            target=f"领用记录.{claim.get('领用编号')}",
            operator=claim.get("领用人", "值班员"),
            summary=f"暂停领用{claim.get('领用编号')}尚未发出的补采，未产生新采集",
            mark=f"replay-pause-{claim['id']}",
        )
        return {"ok": True, "entry": claim}

    # ── 复查：原审批、原失败、撤回与派发结果 ───────────────────────────────

    def review_application(self, application_id: int) -> dict[str, Any]:
        """复查一笔恢复申请的完整来龙去脉，含原失败、审批、授权、领用与审计。"""
        entry = self._find_application(application_id)
        if entry is None:
            return {"ok": False, "reason": "恢复申请不存在"}
        event = next((e for e in self.events if e.get("id") == entry.get("事件ID")), None)
        grants = [dict(g) for g in self.claims
                  if g.get("申请ID") == application_id and "授权状态" in g]
        claim_records = [dict(c) for c in self.claims
                         if c.get("申请ID") == application_id and "领用状态" in c]
        audit_svc = _service_for("audit_trail")
        audits = [
            dict(a) for a in audit_svc.list_entries(size=10000)[0]
            if str(a.get("追溯标记", "")).startswith((
                f"replay-apply-{application_id}",
                f"replay-approve-{application_id}",
                f"replay-reject-{application_id}",
            ))
            or (entry.get("申请编号") and entry["申请编号"] in str(a.get("变更摘要", "")))
        ]
        return {
            "ok": True,
            "申请": dict(entry),
            "原失败事件": dict(event) if event else None,
            "授权记录": grants,
            "领用记录": claim_records,
            "审计条目": audits,
            "说明": (f"该申请于{entry.get('申请时间')}提交、{entry.get('审批时间') or '未'}审批；"
                     f"当前状态{entry.get('status')}，"
                     f"失效时间{entry.get('失效时间') or '无'}"),
        }

    # ── 内部工具 ────────────────────────────────────────────────────────────

    def _find_application(self, application_id: int) -> dict[str, Any] | None:
        return next((a for a in self.applications if a.get("id") == application_id), None)

    def _original_attempts(self, application: dict[str, Any]) -> str:
        event = next((e for e in self.events if e.get("id") == application.get("事件ID")), None)
        return str(event.get("原尝试次数", "0")) if event else "0"

    def _write_audit(self, *, operation: str, target: str, operator: str,
                     summary: str, mark: str, linked_rule: str = "",
                     rule_version: str = "") -> None:
        audit_svc = _service_for("audit_trail")
        values = {
            "条目编号": f"AUDI-RR-{mark}",
            "操作对象": target,
            "操作类型": operation,
            "操作者标识": operator,
            "操作时间": _now_iso(),
            "变更摘要": summary,
            "关联执行": "",
            "追溯标记": mark,
            "关联规则版本": f"{linked_rule}@v{rule_version}" if linked_rule else "",
        }
        audit_svc.create_entry(values)
