import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import RuleReplayView from '@/views/RuleReplayView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/rule_replay', name: 'rule-replay', component: RuleReplayView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
