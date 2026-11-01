<template>
  <el-card shadow="never">
    <template #header>
      <div class="row-between">
        <span>客户全景</span>
        <el-tag size="small" type="info" effect="plain">只读 · 源自客户全景域</el-tag>
      </div>
    </template>
    <el-descriptions :column="1" size="small">
      <el-descriptions-item label="等级">{{ panel.level }}</el-descriptions-item>
      <el-descriptions-item label="累计成交"><span class="num">{{ panel.deals }}</span></el-descriptions-item>
      <el-descriptions-item label="全局标签">
        <el-tag v-for="t in panel.tags" :key="t" size="small" effect="plain" style="margin-right:4px">{{ t }}</el-tag>
      </el-descriptions-item>
      <el-descriptions-item label="风险标签">
        <el-tag v-for="t in panel.risk" :key="t" size="small" type="warning" effect="plain">{{ t }}</el-tag>
      </el-descriptions-item>
    </el-descriptions>
    <el-alert type="info" :closable="false" class="mt8" show-icon>
      <template #title><span style="font-size:12px">CSM 售后摘要 · {{ panel.csm }}</span></template>
    </el-alert>
  </el-card>
</template>

<script setup>
defineProps({ panel: { type: Object, required: true } })
</script>
