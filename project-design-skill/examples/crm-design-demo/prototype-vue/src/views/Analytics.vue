<template>
  <div class="page-pad">
    <div class="page-head">
      <span class="sp" />
      <el-segmented v-model="range" :options="['本周', '本月', '本季']" size="small" />
    </div>

    <KpiStrip :items="aKpis" />

    <el-row :gutter="14" class="mt16">
      <el-col :span="12">
        <el-card shadow="never" header="销售漏斗 · 阶段转化率">
          <div v-for="(f, i) in funnel" :key="f.label" class="fbar" :style="{ width: f.pct + '%', background: barColor(i) }">
            <span>{{ f.label }}</span><span class="num">{{ f.value }} · {{ f.pct }}%</span>
          </div>
          <el-text size="small" class="muted mt8">瓶颈：线索→商机（51%）低于目标 60%，集中在「意向确认」停留过久。</el-text>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never" header="销售绩效 · 按人成交额">
          <div v-for="p in perf" :key="p.name" class="prow">
            <span class="pname">{{ p.name }}</span>
            <el-progress :percentage="p.pct" :stroke-width="14" :show-text="false" style="flex:1" />
            <span class="num muted" style="width:60px;text-align:right">¥{{ p.amount }}K</span>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" header="话术分析 · 高意向未转化" class="mt16">
      <el-table :data="stuck">
        <el-table-column prop="name" label="商机" min-width="180" />
        <el-table-column label="停留" width="160"><template #default="{ row }"><el-tag size="small" :type="row.type" effect="light">{{ row.stuck }}</el-tag></template></el-table-column>
        <el-table-column prop="amount" label="金额" width="100" align="right" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import KpiStrip from '../components/KpiStrip.vue'
import { funnel, perf } from '../mock/data.js'

const range = ref('本月')
const aKpis = [
  { label: '线索量', value: '1,284', delta: '+12%', up: true }, { label: '线索→商机', value: '42.8%' },
  { label: '赢单率', value: '31.4%', delta: '+2.1%', up: true }, { label: '成交额', value: '¥ 1.46M' },
  { label: '平均周期', value: '5.8 天', delta: '-0.4 天', up: false }
]
const colors = ['#409eff', '#5a8fe0', '#6e9bd6', '#7aa6cc', '#67c23a']
const barColor = i => colors[i] || '#409eff'
const stuck = [
  { name: '宏图教育 · 续约扩容', stuck: '报价 9 天', type: 'warning', amount: '¥96K' },
  { name: '远途供应链 · ERP', stuck: '谈判 折扣挂起', type: 'danger', amount: '¥128K' },
  { name: '青柚生鲜 · 多门店', stuck: '意向 7 天', type: 'warning', amount: '¥54K' }
]
</script>

<style scoped>
.fbar { margin-bottom: 6px; min-width: 120px; }
.prow { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.pname { width: 48px; }
</style>
