<template>
  <div class="page-pad">
    <div class="page-head">
      <el-tag type="primary" effect="plain">管理后台 · 仅运营管理员(R3)</el-tag>
      <span class="sp" />
      <el-button @click="newDlg = true">+ 新建角色</el-button>
      <el-button type="primary" @click="save">保存权限</el-button>
    </div>

    <el-alert type="primary" :closable="false" show-icon class="mb12">
      <template #title><span style="font-size:12px">角色 × 能力矩阵(对应需求 01 / 功能树 04)。勾选即授权;受控操作(审批特价/关闭)默认仅主管。</span></template>
    </el-alert>

    <el-table :data="caps" border>
      <el-table-column prop="cap" label="能力" min-width="220" />
      <el-table-column v-for="r in roles" :key="r.code" :label="r.name" width="150" align="center">
        <template #default="{ row }">
          <el-switch v-model="row.grant[r.code]" :disabled="r.code === 'R4' && !row.api" />
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="newDlg" title="新建角色" width="420px">
      <el-form label-position="top">
        <el-form-item label="角色名"><el-input placeholder="如:渠道运营" /></el-form-item>
        <el-form-item label="代号"><el-input placeholder="如:R5" /></el-form-item>
        <el-form-item label="说明"><el-input type="textarea" :rows="2" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="newDlg = false">取消</el-button><el-button type="primary" @click="addRole">创建</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'
import { ElMessage } from 'element-plus'

const roles = [
  { code: 'R1', name: '销售坐席' }, { code: 'R2', name: '销售主管' },
  { code: 'R3', name: '运营管理员' }, { code: 'R4', name: '集成账号' }
]
function g(r1, r2, r3, r4) { return reactive({ R1: r1, R2: r2, R3: r3, R4: r4 }) }
const caps = reactive([
  { cap: '查看/跟进本人线索商机', grant: g(true, true, false, false) },
  { cap: '改派 / 关闭 / 推进赢单', grant: g(false, true, false, false) },
  { cap: '审批特价(<9折)', grant: g(false, true, false, false) },
  { cap: '配置 SOP / 话术 / 规则', grant: g(false, false, true, false) },
  { cap: '角色与权限管理', grant: g(false, false, true, false) },
  { cap: '建线索 / 回写跟进(API)', api: true, grant: g(false, false, false, true) },
  { cap: '查 SOP / 检索话术(API)', api: true, grant: g(false, false, false, true) }
])
const newDlg = ref(false)
function save () { ElMessage.success('权限矩阵已保存 · 即时生效') }
function addRole () { newDlg.value = false; ElMessage.success('角色已创建') }
</script>

<style scoped>.mb12{margin-bottom:12px;}</style>
