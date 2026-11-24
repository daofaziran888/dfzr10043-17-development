<template>
  <section class="page" data-module="rule_replay">
    <header class="page-head">
      <div>
        <h2>历史规则回放</h2>
        <p class="page-desc">
          按模板筛选历史采集故障事件，用事件签名定位原规则与待启规则，核对两版处置差异后申请恢复；
          规则撤回后授权立即失效，已撤授权不能用于实际派发。回放只读，不清零在跑任务的次数。
        </p>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="replay-error">{{ errorMessage }}</p>

    <!-- 步骤一：登记历史事件 -->
    <article class="replay-card">
      <h3>① 登记历史采集故障（按模板 + 事件签名）</h3>
      <form class="replay-form" @submit.prevent="onRegisterEvent">
        <label><span>模板编号</span>
          <input v-model="eventForm.模板编号" data-testid="event-template" placeholder="如 FLOW-0001" required />
        </label>
        <label><span>事件签名</span>
          <input v-model="eventForm.事件签名" data-testid="event-signature" placeholder="如 ERR:采集超时:gateway-timeout-504" required />
        </label>
        <label><span>事件编号</span>
          <input v-model="eventForm.事件编号" data-testid="event-code" required />
        </label>
        <label><span>错误类别</span>
          <input v-model="eventForm.错误类别" data-testid="event-category" placeholder="如 超时可重试" />
        </label>
        <label><span>原规则编号</span>
          <input v-model="eventForm.原规则编号" data-testid="event-rule" placeholder="如 EXCE-0004" />
        </label>
        <label><span>原尝试次数</span>
          <input v-model.number="eventForm.原尝试次数" data-testid="event-attempts" type="number" min="0" />
        </label>
        <button class="btn primary" data-testid="event-submit" type="submit" :disabled="busy">
          {{ busy ? '提交中…' : '登记事件' }}
        </button>
      </form>
    </article>

    <!-- 步骤二：按模板回放 -->
    <article class="replay-card">
      <h3>② 按模板筛选并回放（只重放选定模板的历史错误）</h3>
      <form class="replay-form" @submit.prevent="onReplay">
        <label class="grow"><span>模板编号</span>
          <input v-model="replayTemplateCode" data-testid="replay-template" placeholder="只回放该模板下的事件" required />
        </label>
        <button class="btn primary" data-testid="replay-submit" type="submit" :disabled="busy">执行回放</button>
        <button class="btn ghost" type="button" @click="reloadEvents">刷新历史事件</button>
      </form>

      <table v-if="replayResult" class="data-table" data-testid="replay-result">
        <thead>
          <tr><th>事件编号</th><th>事件签名</th><th>原规则结论</th><th>待启规则结论</th><th>冲突</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in replayResult.结果" :key="item.event_id" :data-conflict="String(!!item.冲突结论)">
            <td>{{ item.事件编号 ?? item.event_id }}</td>
            <td>{{ item.事件签名 }}</td>
            <td v-if="item.可回放">
              {{ item.原规则版本?.规则编号 }} v{{ item.原规则版本?.规则版本 }}
              （{{ item.原规则版本?.次数耗尽 ? '次数耗尽，剩余0' : `剩余${item.原规则版本?.剩余次数}` }}）
            </td>
            <td v-else class="empty-state">{{ item.原因 }}</td>
            <td v-if="item.可回放">
              {{ item.待启规则版本?.规则编号 }} v{{ item.待启规则版本?.规则版本 }}
              （{{ item.待启规则版本?.仍可处理 ? `归入「${item.待启规则版本?.归入错误类别}」，剩余${item.待启规则版本?.剩余次数}` : '不可恢复' }}）
            </td>
            <td v-else>—</td>
            <td>
              <strong :class="item.冲突结论 ? 'conflict-yes' : 'conflict-no'">
                {{ item.冲突结论 ? '冲突可恢复' : '无冲突' }}
              </strong>
            </td>
            <td>
              <button
                v-if="item.可回放 && item.冲突结论"
                class="link"
                type="button"
                :data-testid="`apply-${item.event_id}`"
                @click="onApply(item.事件签名)"
              >
                申请恢复
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </article>

    <!-- 步骤三：恢复申请与授权处置 -->
    <article class="replay-card">
      <h3>③ 恢复申请、审批与领用（同一差异只产生一个申请）</h3>
      <table class="data-table" data-testid="application-list">
        <thead>
          <tr>
            <th>申请编号</th><th>模板</th><th>处置差异</th><th>状态</th>
            <th>审批时间</th><th>失效时间</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="app in applications" :key="app.id" :data-status="app.status">
            <td>{{ app.申请编号 }}</td>
            <td>{{ app.模板编号 }}</td>
            <td class="diff-cell">{{ app.处置差异 }}</td>
            <td>{{ app.status }}</td>
            <td>{{ app.审批时间 || '—' }}</td>
            <td>{{ app.失效时间 || '—' }}</td>
            <td class="row-actions">
              <button v-if="app.status === '待审批'" class="link" type="button"
                      :data-testid="`approve-${app.id}`" @click="onApprove(app.id)">批准</button>
              <button v-if="app.status === '待审批'" class="link" type="button"
                      :data-testid="`reject-${app.id}`" @click="onReject(app.id)">驳回</button>
              <button v-if="app.status === '已批准'" class="link" type="button"
                      :data-testid="`claim-${app.id}`" @click="onClaim(app.id)">领用授权</button>
              <button v-if="app.status === '已批准' || app.status === '已撤回'" class="link" type="button"
                      :data-testid="`review-${app.id}`" @click="onReview(app.id)">复查</button>
              <button v-if="app.status === '已批准'" class="link danger" type="button"
                      :data-testid="`withdraw-${app.id}`" @click="onWithdraw(app.待启规则ID)">撤回拟启规则</button>
            </td>
          </tr>
          <tr v-if="!applications.length">
            <td colspan="7" class="empty-state">暂无恢复申请，先在上方回放结果中提交</td>
          </tr>
        </tbody>
      </table>
    </article>

    <!-- 步骤四：领用记录与派发结果 -->
    <article class="replay-card">
      <h3>④ 领用记录与补采派发（已撤授权一律阻断，不产生新采集）</h3>
      <table class="data-table" data-testid="claim-list">
        <thead>
          <tr>
            <th>编号</th><th>类型</th><th>模板</th><th>关联原规则/原尝试</th>
            <th>状态</th><th>派发结果</th><th>采集任务号</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="claim in claims" :key="claim.id">
            <td>{{ claim.领用编号 || claim.授权编号 }}</td>
            <td>{{ claim.授权状态 ? '恢复授权' : '领用记录' }}</td>
            <td>{{ claim.模板编号 }}</td>
            <td>{{ claim.原规则编号 }} v{{ claim.原规则版本 }}<template v-if="claim.原尝试"> / 原尝试{{ claim.原尝试 }}</template></td>
            <td>{{ claim.授权状态 || claim.领用状态 }}</td>
            <td>{{ claim.派发结果 || '—' }}</td>
            <td>{{ claim.采集任务号 || '—' }}</td>
            <td class="row-actions">
              <button v-if="claim.领用状态 === '已领用'" class="link" type="button"
                      :data-testid="`pause-${claim.id}`" @click="onPause(claim.id)">暂停补采</button>
              <button v-if="claim.领用状态 === '已领用' || claim.领用状态 === '已暂停'" class="link" type="button"
                      :data-testid="`dispatch-${claim.id}`" @click="onDispatch(claim.id)">继续旧领用</button>
            </td>
          </tr>
          <tr v-if="!claims.length">
            <td colspan="8" class="empty-state">暂无授权或领用记录</td>
          </tr>
        </tbody>
      </table>
    </article>

    <!-- 复查抽屉 -->
    <div v-if="review" class="modal-backdrop" data-testid="review-modal" @click.self="review = null">
      <div class="modal review-modal">
        <h3>恢复申请复查 · {{ review.申请.申请编号 }}</h3>
        <p class="page-desc">{{ review.说明 }}</p>
        <dl class="review-grid">
          <dt>处置差异（为什么批准）</dt><dd>{{ review.申请.处置差异 }}</dd>
          <dt>申请时间</dt><dd>{{ review.申请.申请时间 }}</dd>
          <dt>审批时间</dt><dd>{{ review.申请.审批时间 || '未审批' }}</dd>
          <dt>当前状态</dt><dd>{{ review.申请.status }}</dd>
          <dt>失效时间</dt><dd>{{ review.申请.失效时间 || '无' }}</dd>
          <dt>原失败事件</dt>
          <dd>
            {{ review.原失败事件?.事件编号 }}（{{ review.原失败事件?.事件签名 }}，
            原尝试 {{ review.原失败事件?.原尝试次数 }} 次）
          </dd>
        </dl>
        <h4>授权与领用</h4>
        <ul class="review-list">
          <li v-for="g in review.授权记录" :key="g.id">
            授权 {{ g.授权编号 }}：{{ g.授权状态 }}，失效时间 {{ g.失效时间 || '无' }}
          </li>
          <li v-for="c in review.领用记录" :key="c.id">
            领用 {{ c.领用编号 }}：{{ c.领用状态 }}，{{ c.派发结果 || '未派发' }}
            <template v-if="c.采集任务号">，任务号 {{ c.采集任务号 }}</template>
          </li>
        </ul>
        <h4>审计轨迹（此前的审批和原失败都能复查）</h4>
        <ul class="review-list">
          <li v-for="a in review.审计条目" :key="a.条目编号">
            {{ a.操作时间 }} · {{ a.操作类型 }} · {{ a.操作者标识 }}：{{ a.变更摘要 }}
          </li>
        </ul>
        <div class="page-actions">
          <button class="btn primary" type="button" @click="review = null">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  approveApplication,
  claimRecovery,
  dispatchClaim,
  listApplications,
  listClaims,
  listReplayEvents,
  pauseClaim,
  registerReplayEvent,
  rejectApplication,
  replayTemplate,
  reviewApplication,
  submitRecoveryApplication,
  withdrawRule,
} from '@/api/rule-replay'
import type {
  ClaimRecord,
  RecoveryApplication,
  ReplayEvent,
  ReplayResult,
} from '@/api/rule-replay'

const errorMessage = ref('')
const busy = ref(false)
const eventForm = ref({
  模板编号: 'FLOW-0001',
  事件签名: 'ERR:采集超时:gateway-timeout-504',
  事件编号: '',
  错误类别: '超时可重试',
  原规则编号: 'EXCE-0004',
  原尝试次数: 3,
})
const replayTemplateCode = ref('FLOW-0001')
const events = ref<ReplayEvent[]>([])
const replayResult = ref<ReplayResult | null>(null)
const applications = ref<RecoveryApplication[]>([])
const claims = ref<ClaimRecord[]>([])
const review = ref<Awaited<ReturnType<typeof reviewApplication>> | null>(null)

function fail(error: unknown) {
  errorMessage.value = error instanceof Error ? error.message : '历史规则回放操作失败'
}

async function reloadAll() {
  const [eventPage, appPage, claimPage] = await Promise.all([
    listReplayEvents(),
    listApplications(),
    listClaims(),
  ])
  events.value = eventPage.items
  applications.value = appPage.items
  claims.value = claimPage.items
}

async function reloadEvents() {
  const page = await listReplayEvents()
  events.value = page.items
}

async function reloadApplications() {
  const page = await listApplications()
  applications.value = page.items
}

async function reloadClaims() {
  const page = await listClaims()
  claims.value = page.items
}

async function onRegisterEvent() {
  busy.value = true
  errorMessage.value = ''
  try {
    const result = await registerReplayEvent({ ...eventForm.value })
    if (!result.ok) throw new Error(result.message)
    eventForm.value.事件编号 = ''
    await reloadEvents()
  } catch (error) {
    fail(error)
  } finally {
    busy.value = false
  }
}

async function onReplay() {
  busy.value = true
  errorMessage.value = ''
  try {
    replayResult.value = await replayTemplate(replayTemplateCode.value)
    await reloadEvents()
  } catch (error) {
    fail(error)
  } finally {
    busy.value = false
  }
}

async function onApply(signature: string) {
  errorMessage.value = ''
  try {
    const result = await submitRecoveryApplication(replayTemplateCode.value, signature)
    if (!result.ok) throw new Error(result.reason)
    await reloadApplications()
  } catch (error) {
    fail(error)
  }
}

async function onApprove(id: number) {
  errorMessage.value = ''
  try {
    const result = await approveApplication(id)
    if (!result.ok) throw new Error(result.reason)
    await Promise.all([reloadApplications(), reloadClaims()])
  } catch (error) {
    fail(error)
  }
}

async function onReject(id: number) {
  errorMessage.value = ''
  try {
    const result = await rejectApplication(id)
    if (!result.ok) throw new Error(result.reason)
    await reloadApplications()
  } catch (error) {
    fail(error)
  }
}

async function onClaim(id: number) {
  errorMessage.value = ''
  try {
    const result = await claimRecovery(id)
    if (!result.ok) throw new Error(result.reason)
    await Promise.all([reloadApplications(), reloadClaims()])
  } catch (error) {
    fail(error)
  }
}

async function onWithdraw(ruleId: number) {
  errorMessage.value = ''
  try {
    const result = await withdrawRule(ruleId)
    if (!result.ok) throw new Error(result.reason)
    await Promise.all([reloadApplications(), reloadClaims()])
  } catch (error) {
    fail(error)
  }
}

async function onPause(claimId: number) {
  errorMessage.value = ''
  try {
    const result = await pauseClaim(claimId)
    if (!result.ok) throw new Error(result.reason)
    await reloadClaims()
  } catch (error) {
    fail(error)
  }
}

async function onDispatch(claimId: number) {
  errorMessage.value = ''
  try {
    const result = await dispatchClaim(claimId)
    if (!result.ok) {
      // 阻断是预期业务结果：提示但不抛红，列表会显示已阻断、无采集任务号
      errorMessage.value = result.reason || '派发被阻断'
    }
    await reloadClaims()
  } catch (error) {
    fail(error)
  }
}

async function onReview(applicationId: number) {
  errorMessage.value = ''
  try {
    review.value = await reviewApplication(applicationId)
  } catch (error) {
    fail(error)
  }
}

onMounted(reloadAll)
</script>

<style scoped>
.replay-card {
  background: var(--card-bg, #fff);
  border: 1px solid var(--border-color, #e3e6ee);
  border-radius: 10px;
  padding: 16px 18px;
  margin-bottom: 18px;
}

.replay-card h3 {
  margin: 0 0 12px;
  font-size: 15px;
}

.replay-form {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 14px;
  align-items: flex-end;
}

.replay-form label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--muted, #6b7280);
}

.replay-form label.grow {
  flex: 1 1 240px;
}

.replay-form input {
  min-width: 150px;
}

.diff-cell {
  max-width: 320px;
  font-size: 12px;
}

.conflict-yes {
  color: #b45309;
}

.conflict-no {
  color: #047857;
}

.link.danger {
  color: #b91c1c;
}

.review-modal {
  max-width: 720px;
  max-height: 82vh;
  overflow: auto;
}

.review-grid {
  display: grid;
  grid-template-columns: 160px 1fr;
  gap: 6px 12px;
  margin: 12px 0;
  font-size: 13px;
}

.review-grid dt {
  color: var(--muted, #6b7280);
}

.review-grid dd {
  margin: 0;
}

.review-list {
  margin: 6px 0 14px;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.7;
}
</style>
