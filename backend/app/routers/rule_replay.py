"""历史规则回放专用路由。

通用资源路由只覆盖列表/登记/动作三类标准操作，回放链路包含多步骤领域
行为（回放、申请、审批、撤回、领用、派发、复查），因此单独开一组路由，
业务判断全部下沉到 RuleReplayService，路由层只做参数装配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.rule_replay import RuleReplayService

router = APIRouter(prefix="/api/rule-replay", tags=["历史规则回放"])


class EventPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class TemplateFilter(BaseModel):
    template_code: str
    signatures: list[str] | None = None


class ApplicationPayload(BaseModel):
    template_code: str
    signature: str
    operator: str = "值班员"


class ApprovalPayload(BaseModel):
    approver: str = "审批人"


class WithdrawPayload(BaseModel):
    rule_id: int
    operator: str = "值班主管"


class OperatorPayload(BaseModel):
    operator: str = "值班员"


def _service() -> RuleReplayService:
    from app.services import service_for
    return service_for("rule_replay_event")  # type: ignore[return-value]


@router.post("/events")
def register_event(payload: EventPayload) -> dict[str, Any]:
    """登记一笔历史采集故障事件（同模板同签名幂等）。"""
    entry, missing = _service().register_event(payload.values)
    if missing:
        return {"ok": False, "message": f"缺少必填字段：{'、'.join(missing)}"}
    return {"ok": True, "message": "历史事件已登记", "entry": entry}


@router.get("/events")
def list_events(template_code: str | None = None) -> dict[str, Any]:
    """按模板查看历史事件；不传模板返回全部。"""
    items = _service().list_events(template_code)
    return {"items": items, "total": len(items)}


@router.post("/replay")
def replay(payload: TemplateFilter) -> dict[str, Any]:
    """按模板筛选历史事件并回放，输出原规则/待启规则的冲突结论。"""
    return _service().replay_template(payload.template_code, payload.signatures)


@router.post("/applications")
def submit_application(payload: ApplicationPayload) -> dict[str, Any]:
    """核对差异后提交恢复申请；同一份差异重复提交只产生一个申请。"""
    return _service().submit_application(
        payload.template_code, payload.signature, payload.operator)


@router.get("/applications")
def list_applications() -> dict[str, Any]:
    items = _service().applications
    return {"items": [dict(row) for row in items], "total": len(items)}


@router.get("/applications/{application_id}")
def review_application(application_id: int) -> dict[str, Any]:
    """复查申请：原失败事件、审批、授权、领用与审计条目一次返回。"""
    return _service().review_application(application_id)


@router.post("/applications/{application_id}/approve")
def approve_application(application_id: int, payload: ApprovalPayload) -> dict[str, Any]:
    """批准恢复申请，生成有效恢复授权。"""
    return _service().approve_application(application_id, payload.approver)


@router.post("/applications/{application_id}/reject")
def reject_application(application_id: int, payload: ApprovalPayload) -> dict[str, Any]:
    """驳回恢复申请。"""
    return _service().reject_application(application_id, payload.approver)


@router.post("/applications/{application_id}/claim")
def claim_recovery(application_id: int, payload: OperatorPayload) -> dict[str, Any]:
    """领取恢复授权并取得领用记录；已失效授权不可领用。"""
    return _service().claim_recovery(application_id, payload.operator)


@router.post("/rules/withdraw")
def withdraw_rule(payload: WithdrawPayload) -> dict[str, Any]:
    """撤回拟启规则，挂在该规则上的有效授权同步失效。"""
    return _service().withdraw_rule(payload.rule_id, payload.operator)


@router.post("/claims/{claim_id}/pause")
def pause_claim(claim_id: int) -> dict[str, Any]:
    """暂停尚未发出的补采。"""
    return _service().pause_pending_dispatch(claim_id)


@router.post("/claims/{claim_id}/dispatch")
def dispatch_claim(claim_id: int) -> dict[str, Any]:
    """用实际有效的授权发起补采派发；已撤授权一律阻断，不产生新采集。"""
    return _service().dispatch_recovery(claim_id)


@router.get("/claims")
def list_claims() -> dict[str, Any]:
    items = _service().claims
    return {"items": [dict(row) for row in items], "total": len(items)}
