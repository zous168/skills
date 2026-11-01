<template>
  <div class="page-pad">
    <div class="page-head">
      <el-tag type="primary" effect="plain">管理后台 · 渠道与分配规则(R3)</el-tag>
      <span class="sp" />
      <el-button @click="newDlg = true">+ 新增渠道</el-button>
      <el-button type="primary" @click="save">保存</el-button>
    </div>

    <el-row :gutter="14">
      <el-col :span="14">
        <el-card shadow="never" header="来源渠道(FR-L3)">
          <el-table :data="channels">
            <el-table-column prop="name" label="渠道" min-width="140" />
            <el-table-column prop="campaign" label="默认活动标记" min-width="140" />
            <el-table-column label="启用" width="90" align="center">
              <template #default="{ row }"><el-switch v-model="row.on" /></template>
            </el-table-column>
            <el-table-column label="默认分配组" width="140">
              <template #default="{ row }">
                <el-select v-model="row.group" size="small">
                  <el-option label="华东大客户组" value="华东" /><el-option label="华南组" value="华南" /><el-option label="公海" value="公海" />
                </el-select>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="10">
        <el-card shadow="never" header="分配规则(FR-L4)">
          <el-form label-position="top">
            <el-form-item label="分配策略">
              <el-segmented v-model="strategy" :options="['区域', '负载均衡', '技能组']" />
            </el-form-item>
            <el-form-item label="单坐席并发上限"><el-input-number v-model="cap" :min="1" :max="50" /></el-form-item>
            <el-form-item label="查重有效期(天)"><el-input-number v-model="dedupDays" :min="1" :max="180" /></el-form-item>
            <el-form-item label="无响应升级(次)"><el-input-number v-model="escalate" :min="1" :max="5" /></el-form-item>
          </el-form>
          <el-alert type="primary" :closable="false"><template #title><span style="font-size:12px">规则即时作用于新进线索的自动分配;历史不回溯。</span></template></el-alert>
        </el-card>
      </el-col>
    </el-row>

    <el-dialog v-model="newDlg" title="新增渠道" width="420px">
      <el-form label-position="top">
        <el-form-item label="渠道名"><el-input placeholder="如:视频号" /></el-form-item>
        <el-form-item label="接入方式"><el-select placeholder="选择" style="width:100%"><el-option label="Automan" value="a" /><el-option label="站点组件" value="b" /><el-option label="广告平台 API" value="c" /></el-select></el-form-item>
      </el-form>
      <template #footer><el-button @click="newDlg = false">取消</el-button><el-button type="primary" @click="addCh">添加</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'
import { ElMessage } from 'element-plus'

const channels = reactive([
  { name: '官网表单', campaign: '官网·方案下载', on: true, group: '华东' },
  { name: '企业微信', campaign: '广告留资', on: true, group: '华东' },
  { name: '抖音 · Automan', campaign: '夏季获客', on: true, group: '华南' },
  { name: '微信', campaign: '朋友圈广告', on: true, group: '公海' },
  { name: '广告留资', campaign: '信息流计划', on: false, group: '公海' }
])
const strategy = ref('负载均衡')
const cap = ref(12)
const dedupDays = ref(30)
const escalate = ref(2)
const newDlg = ref(false)
function save () { ElMessage.success('渠道与分配规则已保存 · 即时生效') }
function addCh () { newDlg.value = false; ElMessage.success('渠道已添加') }
</script>
