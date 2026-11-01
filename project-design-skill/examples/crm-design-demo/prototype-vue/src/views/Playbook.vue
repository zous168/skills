<template>
  <div class="page-pad">
    <el-alert type="primary" :closable="false" show-icon class="mb12">
      <template #title><span style="font-size:12px">分域存储：本库仅管销售转化素材；售后 FAQ 归 CSM 知识库。Hermes 按 Agent 类型路由检索。</span></template>
    </el-alert>

    <div class="page-head">
      <el-segmented v-model="cat" :options="['全部', '产品卖点', '活动政策', '竞品对比']" size="small" />
      <span class="sp" />
      <el-select model-value="客户等级：全部" size="default" style="width:140px"><el-option label="客户等级：全部" value="all" /></el-select>
      <el-button type="primary" @click="newDlg = true">+ 新建话术</el-button>
    </div>

    <el-table :data="playbooks">
      <el-table-column label="话术 / 政策" min-width="200">
        <template #default="{ row }"><div class="qname">{{ row.title }}</div><div class="muted">{{ row.desc }}</div></template>
      </el-table-column>
      <el-table-column label="适用" width="150">
        <template #default="{ row }"><el-tag v-for="s in row.scope" :key="s" size="small" effect="plain" style="margin-right:4px">{{ s }}</el-tag></template>
      </el-table-column>
      <el-table-column prop="version" label="版本" width="70" />
      <el-table-column prop="period" label="生效期" width="120" />
      <el-table-column label="RAG" width="90">
        <template #default="{ row }"><el-tag size="small" :type="row.ragType" effect="light">{{ row.rag }}</el-tag></template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }"><el-tag size="small" :type="row.statusType" effect="light">{{ row.status }}</el-tag></template>
      </el-table-column>
      <el-table-column label="操作" width="150" align="right">
        <template #default="{ row }">
          <el-button size="small" type="primary" link @click="edit(row)">编辑升版</el-button>
          <el-button size="small" type="danger" link @click="retire(row)">失效</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="newDlg" :title="editing ? '编辑并升版' : '新建话术 / 政策'" width="460px">
      <el-alert v-if="editing" type="warning" :closable="false" show-icon class="mb12"><template #title>保存将生成新版本并保留历史；旧版自动失效。</template></el-alert>
      <el-form label-position="top">
        <el-form-item label="标题"><el-input v-model="form.title" placeholder="如：多门店版·试用转化话术" /></el-form-item>
        <el-form-item label="类型"><el-select model-value="产品卖点" style="width:100%"><el-option label="产品卖点" value="a" /><el-option label="活动政策" value="b" /><el-option label="竞品对比" value="c" /></el-select></el-form-item>
        <el-form-item label="话术正文"><el-input type="textarea" :rows="4" placeholder="供 Hermes 销售 Agent RAG 检索的官方素材" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="newDlg = false">取消</el-button><el-button type="primary" @click="save">{{ editing ? '发布新版本' : '保存草稿' }}</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { playbooks } from '../mock/data.js'

const cat = ref('全部')
const newDlg = ref(false)
const editing = ref(false)
const form = reactive({ title: '' })

function edit (row) { editing.value = true; form.title = row.title; newDlg.value = true }
function save () { newDlg.value = false; ElMessage.success(editing.value ? '已发布新版本 · 重新索引中' : '话术已保存为草稿 · 待索引'); editing.value = false }
function retire (row) {
  ElMessageBox.confirm('失效后 Hermes 将不再检索到它。确认失效？', '失效话术', { type: 'warning', confirmButtonText: '确认失效' })
    .then(() => ElMessage.warning(`「${row.title}」已失效 · 已从 RAG 索引移除`)).catch(() => {})
}
</script>

<style scoped>
.qname { font-weight: 600; }
.mb12 { margin-bottom: 12px; }
</style>
