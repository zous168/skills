<template>
  <el-timeline>
    <el-timeline-item
      v-for="e in events"
      :key="e.id"
      :timestamp="e.time"
      placement="top"
      :type="e.ai ? 'primary' : 'info'"
      :hollow="!e.ai"
    >
      <div class="row-between" style="gap:8px;flex-wrap:wrap">
        <strong>{{ e.by }}</strong>
        <el-tag size="small" type="info" effect="plain">{{ e.channel }}</el-tag>
      </div>
      <div class="muted mt8">{{ e.text }}</div>
      <div v-if="e.trace" class="faint num mt8" style="font-size:11px">{{ e.trace }}</div>
    </el-timeline-item>
  </el-timeline>
</template>

<script setup>
defineProps({ events: { type: Array, default: () => [] } })
</script>
