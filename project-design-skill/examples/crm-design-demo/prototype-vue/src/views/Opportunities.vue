<template>
  <div class="page-pad">
    <div class="page-head">
      <el-segmented v-model="scope" :options="['我的商机', '全组']" size="small" />
      <el-tag type="primary" effect="plain">阶段可配置 · §4.2</el-tag>
      <span class="sp" />
      <el-button type="primary" @click="newDlg = true">+ 新建商机</el-button>
    </div>

    <el-alert type="primary" :closable="false" show-icon class="mb12">
      <template #title><span style="font-size:12px">点击任意商机卡片进入工作台详情。赢单触发：回写客户全景 → 通知 CSM → 回调 Hermes。折扣/特价须经规则校验或人工审批。</span></template>
    </el-alert>

    <div class="kb">
      <el-card v-for="col in columns" :key="col.stage" shadow="never" body-style="padding:9px" class="kb-col">
        <template #header>
          <div class="row-between"><strong style="font-size:13px">{{ col.stage }}</strong>
            <span class="faint num" style="font-size:11px">{{ col.items.length }} · ¥{{ col.sum }}K</span></div>
        </template>
        <el-card v-for="o in col.items" :key="o.id" shadow="hover" class="oc mt8" body-style="padding:10px 11px" @click="open(o)">
          <div class="qname" style="font-size:12.5px">{{ o.name }}</div>
          <div class="row-between mt8">
            <span class="amt num">¥ {{ (o.amount / 1000).toFixed(0) }}K</span>
            <el-tag size="small" :type="o.level === 'A' ? 'success' : 'info'" effect="light">{{ o.level }} 级</el-tag>
          </div>
          <div class="mt8"><el-tag size="small" effect="plain">{{ o.src }}</el-tag></div>
        </el-card>
        <el-empty v-if="!col.items.length" :image-size="40" description="暂无" />
      </el-card>
    </div>

    <el-dialog v-model="newDlg" title="新建商机" width="440px">
      <el-form label-position="top">
        <el-form-item label="客户"><el-select placeholder="选择客户" style="width:100%"><el-option label="远途供应链(customerId 8841)" value="8841" /><el-option label="+ 关联新客户" value="new" /></el-select></el-form-item>
        <el-form-item label="商机名称"><el-input placeholder="如：ERP 集成采购" /></el-form-item>
        <el-form-item label="预计金额(¥)"><el-input class="num" placeholder="128000" /></el-form-item>
        <el-form-item label="初始阶段"><el-select model-value="意向确认" style="width:100%"><el-option label="意向确认" value="意向确认" /><el-option label="方案报价" value="方案报价" /></el-select></el-form-item>
      </el-form>
      <template #footer><el-button @click="newDlg = false">取消</el-button><el-button type="primary" @click="create">创建商机</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { opportunities } from '../mock/data.js'

const router = useRouter()
const scope = ref('我的商机')
const newDlg = ref(false)
const stageList = ['意向确认', '方案报价', '谈判', '赢单', '输单']

const columns = computed(() => stageList.map(stage => {
  const items = opportunities.filter(o => o.stage === stage)
  return { stage, items, sum: items.reduce((s, o) => s + o.amount, 0) / 1000 }
}))

function open (o) { router.push('/workbench') }
function create () { newDlg.value = false; ElMessage.success('商机已创建 · 进入「意向确认」') }
</script>

<style scoped>
.kb-col { background: var(--el-fill-color-light); }
.qname { font-weight: 600; }
.mb12 { margin-bottom: 12px; }
</style>
