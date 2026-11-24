import { request } from './client'

/**
 * 历史规则回放专用接口。
 * 与通用资源接口分开：回放是多步骤领域链路（登记→回放→申请→审批→领用→撤回/派发→复查）。
 */

export type ReplayEvent = {
  id: number
  status: string
  事件编号: string
  模板编号: string
  事件签名: string
  错误类别: string
  原规则编号: string
  原尝试次数: string
  采集时间: string
}

export type RuleVersionDiff = {
  rule_id: number
  规则编号: string
  规则版本: string
  处置动作: string
  次数耗尽?: boolean
  剩余次数: number
  归入错误类别?: string
  仍可处理?: boolean
}

export type ReplayResult = {
  模板编号: string
  回放事件数: number
  冲突差异数: number
  结果: Array<{
    event_id: number
    事件编号?: string
    事件签名: string
    可回放: boolean
    冲突结论?: boolean
    原因?: string
    原规则版本?: RuleVersionDiff
    待启规则版本?: RuleVersionDiff
  }>
}

export type RecoveryApplication = {
  id: number
  status: string
  申请编号: string
  模板编号: string
  事件签名: string
  原规则编号: string
  原规则版本: string
  待启规则编号: string
  待启规则版本: string
  待启规则ID: number
  处置差异: string
  申请人: string
  申请时间: string
  审批时间: string
  失效时间: string
}

export type ClaimRecord = {
  id: number
  领用编号?: string
  授权编号?: string
  领用状态?: string
  授权状态?: string
  模板编号: string
  事件签名: string
  原规则编号: string
  原规则版本: string
  原尝试?: string
  派发结果?: string
  采集任务号?: string
  失效时间?: string
  授权时间?: string
}

async function postJson<T>(path: string, body: unknown = {}): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error('回放接口调用失败')
  return (await response.json()) as T
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error('回放数据读取失败')
  return (await response.json()) as T
}

export function registerReplayEvent(values: Record<string, string | number>) {
  return postJson<{ ok: boolean; message: string; entry?: ReplayEvent }>(
    '/api/rule-replay/events',
    { values },
  )
}

export function listReplayEvents(templateCode = '') {
  const query = templateCode ? `?template_code=${encodeURIComponent(templateCode)}` : ''
  return getJson<{ items: ReplayEvent[]; total: number }>(
    `/api/rule-replay/events${query}`,
  )
}

export function replayTemplate(templateCode: string, signatures: string[] = []) {
  return postJson<ReplayResult>('/api/rule-replay/replay', {
    template_code: templateCode,
    signatures: signatures.length ? signatures : null,
  })
}

export function submitRecoveryApplication(templateCode: string, signature: string) {
  return postJson<{ ok: boolean; reason?: string; 申请ID?: number; 去重?: boolean; entry?: RecoveryApplication }>(
    '/api/rule-replay/applications',
    { template_code: templateCode, signature },
  )
}

export function listApplications() {
  return getJson<{ items: RecoveryApplication[]; total: number }>(
    '/api/rule-replay/applications',
  )
}

export function reviewApplication(applicationId: number) {
  return getJson<{
    ok: boolean
    说明: string
    申请: RecoveryApplication
    原失败事件: ReplayEvent | null
    授权记录: ClaimRecord[]
    领用记录: ClaimRecord[]
    审计条目: Array<{ 条目编号: string; 操作类型: string; 操作者标识: string; 操作时间: string; 变更摘要: string }>
  }>(`/api/rule-replay/applications/${applicationId}`)
}

export function approveApplication(applicationId: number) {
  return postJson<{ ok: boolean; reason?: string; 授权编号?: string }>(
    `/api/rule-replay/applications/${applicationId}/approve`,
    {},
  )
}

export function rejectApplication(applicationId: number) {
  return postJson<{ ok: boolean; reason?: string }>(
    `/api/rule-replay/applications/${applicationId}/reject`,
    {},
  )
}

export function claimRecovery(applicationId: number) {
  return postJson<{ ok: boolean; reason?: string; 领用编号?: string; 领用ID?: number; entry?: ClaimRecord }>(
    `/api/rule-replay/applications/${applicationId}/claim`,
    {},
  )
}

export function withdrawRule(ruleId: number) {
  return postJson<{ ok: boolean; 失效授权数: number; reason?: string }>(
    '/api/rule-replay/rules/withdraw',
    { rule_id: ruleId },
  )
}

export function pauseClaim(claimId: number) {
  return postJson<{ ok: boolean; reason?: string }>(
    `/api/rule-replay/claims/${claimId}/pause`,
  )
}

export function dispatchClaim(claimId: number) {
  return postJson<{ ok: boolean; reason?: string; 采集任务号: string; entry?: ClaimRecord }>(
    `/api/rule-replay/claims/${claimId}/dispatch`,
  )
}

export function listClaims() {
  return getJson<{ items: ClaimRecord[]; total: number }>('/api/rule-replay/claims')
}
