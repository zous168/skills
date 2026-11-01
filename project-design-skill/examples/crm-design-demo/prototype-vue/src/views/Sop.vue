<template>
  <div class="page-pad">
    <div class="page-head">
      <el-tag type="primary" effect="plain">Hermes 按场景编码调用</el-tag>
      <span class="sp" />
      <el-button @click="historyDrawer = true">版本历史</el-button>
      <el-button type="primary" @click="publish">发布新版本</el-button>
    </div>

    <el-alert type="primary" :closable="false" show-icon class="mb12">
      <template #title><span style="font-size:12px">引擎按场景编码返回：流程节点 + 权限边界 + 标准话术模板。后台配置、版本灰度，新会话加载最新生效版。</span></template>
    </el-alert>

    <el-row :gutter="14">
      <el-col :span="7">
        <el-card shadow="never" header="场景模板" body-style="padding:0">
          <div v-for="s in sopScenarios" :key="s.code" class="scn" :class="{ on: s.code === cur.code }" @click="cur = s">
            <div class="qname">{{ s.name }}</div>
            <div class="faint num" style="font-size:11.5px">{{ s.code }} · {{ s.version }}</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="17">
        <el-card shadow="never" :header="`${cur.name} · 节点规则（${cur.code} · ${cur.version}）`">
          <el-timeline>
            <el-timeline-item v-for="nd in sopNodes" :key="nd.n" :timestamp="`节点 ${nd.n}`" placement="top">
              <div class="row-between" style="flex-wrap:wrap;gap:8px">
                <strong>{{ nd.name }}</strong>
                <el-tag size="small" :type="nd.tagType" effect="light">{{ nd.tag }}</el-tag>
              </div>
              <div class="mt8">
                <el-tag v-for="r in nd.rules" :key="r" size="small" effect="plain" style="margin:0 4px 4px 0">{{ r }}</el-tag>
              </div>
            </el-timeline-item>
          </el-timeline>
        </el-card>
        <el-card shadow="never" header="SOP 查询返回（GET /api/v1/sop?code=SOP-LEAD-INTAKE）" class="mt16">
          <pre class="code">{
  "code": "SOP-LEAD-INTAKE", "version": "v4",
  "nextNode": "需求澄清", "interval": "D1",
  "quoteFloor": 0.90,   // 低于则必人工
  "escalateAfter": 2,
  "template": "tmpl_need_clarify_v4"
}</pre>
        </el-card>
      </el-col>
    </el-row>

    <el-drawer v-model="historyDrawer" title="版本历史 · SOP-LEAD-INTAKE" size="380px">
      <el-timeline>
        <el-timeline-item timestamp="2026-06-10" type="primary">v4（当前生效）报价上限收紧至 9 折；新增升级规则。</el-timeline-item>
        <el-timeline-item timestamp="2026-04-22">v3 即时响应间隔 10→5 分钟。</el-timeline-item>
        <el-timeline-item timestamp="2026-03-01">v2 增加「需求澄清」节点。</el-timeline-item>
        <el-timeline-item timestamp="2026-01-15">v1 初版上线。</el-timeline-item>
      </el-timeline>
    </el-drawer>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessageBox, ElMessage } from 'element-plus'
import { sopScenarios, sopNodes } from '../mock/data.js'

const cur = ref(sopScenarios[0])
const historyDrawer = ref(false)

function publish () {
  ElMessageBox.confirm('发布后新会话将加载本版本（灰度按配置生效），进行中会话不受影响。确认发布 v5？', '发布新版本',
    { type: 'warning', confirmButtonText: '确认发布' })
    .then(() => ElMessage.success('SOP-LEAD-INTAKE v5 已发布 · 灰度 20%')).catch(() => {})
}
</script>

<style scoped>
.scn { padding: 10px 13px; border-top: 1px solid var(--el-border-color-lighter); cursor: pointer; }
.scn:first-child { border-top: 0; }
.scn.on { background: var(--el-color-primary-light-9); }
.scn.on .qname { color: var(--el-color-primary); }
.qname { font-weight: 600; font-size: 12.5px; }
.code { font-family: monospace; font-size: 12px; background: #1a1f2b; color: #d6deea; border-radius: 4px; padding: 12px 14px; margin: 0; overflow: auto; }
.mb12 { margin-bottom: 12px; }
</style>
