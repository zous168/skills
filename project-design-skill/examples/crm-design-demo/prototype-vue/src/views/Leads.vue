<template>
  <div class="page-pad">
    <KpiStrip :items="leadKpis" />

    <div class="page-head mt16">
      <el-radio-group v-model="filter" size="small">
        <el-radio-button v-for="s in filters" :key="s" :value="s">{{ s }}</el-radio-button>
      </el-radio-group>
      <span class="sp" />
      <el-button @click="importDlg = true">批量导入</el-button>
      <el-button type="primary" @click="newDrawer = true">+ 录入线索</el-button>
    </div>

    <div class="page-head">
      <el-text size="small" class="muted">视图状态（演示）</el-text>
      <el-segmented v-model="viewState" :options="stateOpts" size="small" />
    </div>

    <el-alert type="primary" :closable="false" class="mb12" show-icon>
      <template #title><span style="font-size:12px">查重规则：同手机号 / 渠道 ID 在有效周期内不重复分配 —— 命中返回原线索 ID，不新建。来源经 Automan / 站点 → Hermes 创建。</span></template>
    </el-alert>

    <!-- 数据态 -->
    <el-table v-if="viewState === '数据'" :data="leads" @row-click="openDetail" style="cursor:pointer">
      <el-table-column label="线索 / 联系方式" min-width="160">
        <template #default="{ row }"><div class="qname">{{ row.name }}</div><div class="muted">{{ row.contact }}</div></template>
      </el-table-column>
      <el-table-column label="来源（渠道·活动）" min-width="150">
        <template #default="{ row }"><el-tag size="small" effect="plain">{{ row.src }}</el-tag><div class="muted mt8">{{ row.campaign }}</div></template>
      </el-table-column>
      <el-table-column prop="customer" label="客户" min-width="120" />
      <el-table-column label="状态" width="90">
        <template #default="{ row }"><el-tag size="small" :type="row.statusType || 'info'" effect="light">{{ row.status }}</el-tag></template>
      </el-table-column>
      <el-table-column prop="owner" label="负责人" width="110" />
      <el-table-column label="查重" width="150">
        <template #default="{ row }"><el-tag size="small" :type="row.dedupType" effect="light">{{ row.dedup }}</el-tag></template>
      </el-table-column>
      <el-table-column label="操作" width="100" align="right">
        <template #default="{ row }">
          <el-button size="small" @click.stop="assign(row)">分配</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 载入态 -->
    <el-card v-else-if="viewState === '加载中'" shadow="never"><el-skeleton :rows="5" animated /></el-card>

    <!-- 空态 -->
    <el-empty v-else-if="viewState === '空'" description="当前筛选条件下没有线索">
      <el-button type="primary" @click="newDrawer = true">+ 录入线索</el-button>
    </el-empty>

    <!-- 错误态 -->
    <el-result v-else icon="error" title="线索列表加载失败" sub-title="连接 CRM 服务超时（错误码 504）。请检查网络或稍后重试。">
      <template #extra><el-button @click="retry">重试</el-button></template>
    </el-result>

    <!-- 录入抽屉 -->
    <el-drawer v-model="newDrawer" title="录入线索" size="400px">
      <el-form label-position="top">
        <el-form-item label="联系方式（手机号，必填）"><el-input placeholder="如 138 0000 0000" /></el-form-item>
        <el-form-item label="客户 / 公司"><el-input placeholder="选填，可后续识别" /></el-form-item>
        <el-form-item label="来源渠道">
          <el-select placeholder="选择" style="width:100%">
            <el-option label="官网表单" value="官网表单" /><el-option label="企业微信" value="企业微信" />
            <el-option label="抖音 · Automan" value="抖音" /><el-option label="广告留资" value="广告" />
          </el-select>
        </el-form-item>
        <el-form-item label="备注"><el-input type="textarea" :rows="3" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="newDrawer = false">取消</el-button><el-button type="primary" @click="saveLead">保存并分配</el-button></template>
    </el-drawer>

    <!-- 导入弹框 -->
    <el-dialog v-model="importDlg" title="批量导入线索" width="440px">
      <el-alert type="primary" :closable="false" show-icon class="mb12"><template #title>导入时自动按手机号 / 渠道 ID 查重，命中将合并到已有线索。</template></el-alert>
      <el-form label-position="top">
        <el-form-item label="选择文件（.xlsx/.csv）"><el-input model-value="leads_2026Q2.xlsx" readonly /></el-form-item>
        <el-form-item label="来源标记"><el-select model-value="广告留资 · 信息流计划" style="width:100%"><el-option label="广告留资 · 信息流计划" value="ad" /></el-select></el-form-item>
      </el-form>
      <template #footer><el-button @click="importDlg = false">取消</el-button><el-button type="primary" @click="doImport">开始导入</el-button></template>
    </el-dialog>

    <!-- 线索详情抽屉 -->
    <el-drawer v-model="detailDrawer" :title="`线索详情 · ${cur.name || ''}`" size="400px">
      <el-descriptions :column="1" size="small" border>
        <el-descriptions-item label="联系方式">{{ cur.contact }}</el-descriptions-item>
        <el-descriptions-item label="来源">{{ cur.src }}（{{ cur.campaign }}）</el-descriptions-item>
        <el-descriptions-item label="状态">{{ cur.status }}</el-descriptions-item>
        <el-descriptions-item label="查重">{{ cur.dedup }}</el-descriptions-item>
        <el-descriptions-item label="负责人">{{ cur.owner }}</el-descriptions-item>
      </el-descriptions>
      <el-alert type="primary" :closable="false" class="mt12"><template #title>转为商机将创建商机并进入「意向确认」阶段，关联本线索。</template></el-alert>
      <template #footer><el-button @click="detailDrawer = false">关闭</el-button><el-button type="primary" @click="convert">转为商机</el-button></template>
    </el-drawer>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import KpiStrip from '../components/KpiStrip.vue'
import { leads } from '../mock/data.js'

const leadKpis = [
  { label: '今日新增线索', value: '126' }, { label: '待分配', value: '18' },
  { label: '查重拦截(24h)', value: '37' }, { label: '线索→商机转化', value: '42.8%', delta: '+3.2%', up: true }
]
const filters = ['全部', '待分配', '跟进中', '已转化', '无效']
const filter = ref('全部')
const stateOpts = ['数据', '加载中', '空', '错误']
const viewState = ref('数据')

const newDrawer = ref(false)
const importDlg = ref(false)
const detailDrawer = ref(false)
const cur = ref({})

function openDetail (row) { cur.value = row; detailDrawer.value = true }
function assign (row) { ElMessage.success(`已分配「${row.name}」给 张文 · 已通知 Hermes`) }
function saveLead () { newDrawer.value = false; ElMessage.success('线索已创建 · 已按区域自动分配给张文') }
function doImport () { importDlg.value = false; ElMessage.success('已导入 126 条 · 3 条查重命中已合并') }
function convert () { detailDrawer.value = false; ElMessage.success('已转为商机 · 进入「意向确认」阶段') }
function retry () { ElMessage.info('正在重试…'); viewState.value = '加载中'; setTimeout(() => (viewState.value = '数据'), 800) }
</script>

<style scoped>
.qname { font-weight: 600; }
.mb12 { margin-bottom: 12px; }
</style>
