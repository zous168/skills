<template>
  <div class="page-pad">
    <KpiStrip :items="kpis" />

    <div class="wb-grid mt16">
      <!-- 左：待办队列 -->
      <el-card shadow="never" body-style="padding:0">
        <el-tabs v-model="tab" class="q-tabs">
          <el-tab-pane label="线索 38" name="lead" />
          <el-tab-pane label="商机 21" name="opp" />
        </el-tabs>
        <div class="queue">
          <div
            v-for="o in opportunities"
            :key="o.id"
            class="qrow"
            :class="{ on: o.id === current.id }"
            @click="current = o"
          >
            <el-badge is-dot :type="o.priority === 'high' ? 'danger' : o.priority === 'mid' ? 'warning' : 'info'" />
            <div class="qmain">
              <div class="qname">{{ o.name }}</div>
              <div class="mt8">
                <el-tag size="small" type="primary" effect="light">{{ o.stage }}</el-tag>
                <el-tag size="small" effect="plain" class="ml4">{{ o.src }}</el-tag>
              </div>
            </div>
            <div class="qright">
              <div class="amt num">¥ {{ (o.amount / 1000).toFixed(0) }}K</div>
              <div class="faint" style="font-size:11px">{{ o.wait }}</div>
            </div>
          </div>
        </div>
      </el-card>

      <!-- 右：商机详情 -->
      <div>
        <div class="row-between" style="align-items:flex-start">
          <div>
            <h2 style="margin:0">{{ current.name }}</h2>
            <div class="faint num mt8">商机 {{ current.id }} · 客户 customerId {{ current.customerId }} · {{ current.customer }}</div>
          </div>
          <div style="text-align:right">
            <el-tag type="warning" effect="plain">{{ current.level }} 级客户</el-tag>
            <div class="amt num" style="font-size:18px;margin-top:6px">¥ {{ current.amount.toLocaleString() }}</div>
            <div class="faint" style="font-size:12px">预计成交 · 2026-07-05</div>
          </div>
        </div>

        <StageSteps :stages="stages" :current="current.stage" class="mt16" />

        <el-row :gutter="14" class="mt16">
          <el-col :span="12"><CustomerPanel :panel="customerPanel" /></el-col>
          <el-col :span="12">
            <el-card shadow="never" header="商机要素">
              <el-descriptions :column="1" size="small">
                <el-descriptions-item label="来源">{{ current.src }}（计划 #A-307）</el-descriptions-item>
                <el-descriptions-item label="负责人">{{ current.owner }}（华东大客户组）</el-descriptions-item>
                <el-descriptions-item label="产品线">供应链协同 · 旗舰版 + 集成模块</el-descriptions-item>
                <el-descriptions-item label="报价状态">已报价 ¥128K（标准折 9.2 折）</el-descriptions-item>
              </el-descriptions>
              <el-alert type="warning" :closable="false" show-icon class="mt8">
                <template #title><span style="font-size:12px">客户申请 8.5 折特价 → 超坐席权限（≥9 折），需主管审批。</span></template>
              </el-alert>
            </el-card>
          </el-col>
        </el-row>

        <el-card shadow="never" header="跟进时间线 · 全渠道 14 条" class="mt16">
          <FollowTimeline :events="followUps" />
        </el-card>

        <!-- 人工操作（真实交互） -->
        <el-affix position="bottom" :offset="0">
          <el-card shadow="never" body-style="padding:12px 16px">
            <el-space wrap>
              <el-button type="primary" @click="advance">推进阶段 → 赢单</el-button>
              <el-button @click="reassign = true">改派</el-button>
              <el-button type="warning" plain :icon="Lock" @click="special = true">审批特价</el-button>
              <el-button @click="close">关闭线索</el-button>
              <el-text size="small" class="faint">“审批特价”需主管权限（&lt;9 折）；操作回写商机并通知 Hermes</el-text>
            </el-space>
          </el-card>
        </el-affix>
      </div>
    </div>

    <!-- 改派抽屉 -->
    <el-drawer v-model="reassign" title="改派负责人" size="400px">
      <el-form label-position="top">
        <el-form-item label="当前负责人"><el-input model-value="张文（华东大客户组）" disabled /></el-form-item>
        <el-form-item label="改派给">
          <el-select v-model="reassignTo" style="width:100%">
            <el-option label="李航（华东大客户组）" value="李航" />
            <el-option label="周琳（华南组）" value="周琳" />
          </el-select>
        </el-form-item>
        <el-form-item label="改派说明"><el-input v-model="reassignNote" type="textarea" :rows="3" placeholder="将记入跟进日志" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="reassign = false">取消</el-button>
        <el-button type="primary" @click="doReassign">确认改派</el-button>
      </template>
    </el-drawer>

    <!-- 审批特价弹框 -->
    <el-dialog v-model="special" title="审批特价" width="440px">
      <el-alert type="warning" :closable="false" show-icon class="mb12">
        <template #title>客户申请 8.5 折，低于坐席权限（≥9 折），需主管审批。</template>
      </el-alert>
      <el-form label-position="top">
        <el-form-item label="申请折扣"><el-input v-model="discount" /></el-form-item>
        <el-form-item label="理由"><el-input v-model="reason" type="textarea" :rows="3" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="special = false">取消</el-button>
        <el-button type="primary" @click="doSpecial">提交主管审批</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Lock } from '@element-plus/icons-vue'
import KpiStrip from '../components/KpiStrip.vue'
import StageSteps from '../components/StageSteps.vue'
import FollowTimeline from '../components/FollowTimeline.vue'
import CustomerPanel from '../components/CustomerPanel.vue'
import { kpis, stages, opportunities, followUps, customerPanel } from '../mock/data.js'

const tab = ref('opp')
const current = ref(opportunities[0])

const reassign = ref(false)
const reassignTo = ref('李航')
const reassignNote = ref('')
const special = ref(false)
const discount = ref('8.5 折')
const reason = ref('客户预算 12–13 万，要求 8.5 折并含一年免费实施；A 级战略客户。')

function advance () {
  ElMessageBox.confirm(
    '确认将商机推进为赢单？提交后将：回写客户全景、通知 CSM 开通售后、回调 Hermes。',
    '推进至「赢单」？', { confirmButtonText: '确认赢单', cancelButtonText: '取消', type: 'success' }
  ).then(() => ElMessage.success('已赢单 · 已通知 CSM 与 Hermes')).catch(() => {})
}
function close () {
  ElMessageBox.confirm(
    '此操作将关闭该商机并回写、通知 Hermes，确认继续？',
    '关闭线索 / 商机', { confirmButtonText: '确认关闭', cancelButtonText: '取消', type: 'warning' }
  ).then(() => ElMessage.warning('已关闭 · 原因已记录')).catch(() => {})
}
function doReassign () { reassign.value = false; ElMessage.success(`已改派给 ${reassignTo.value} · 已通知 Hermes`) }
function doSpecial () { special.value = false; ElMessage.info('特价审批已提交主管 · 待审批') }
</script>

<style scoped>
.q-tabs :deep(.el-tabs__header) { margin: 0; padding: 0 12px; }
.queue { max-height: 60vh; overflow: auto; }
.qrow { display: grid; grid-template-columns: 14px 1fr auto; gap: 9px; align-items: start; padding: 11px 14px; border-top: 1px solid var(--el-border-color-lighter); cursor: pointer; }
.qrow:hover { background: var(--el-fill-color-light); }
.qrow.on { background: var(--el-color-primary-light-9); box-shadow: inset 3px 0 0 var(--el-color-primary); }
.qname { font-weight: 600; }
.qright { text-align: right; }
.ml4 { margin-left: 4px; }
.mb12 { margin-bottom: 12px; }
</style>
