<template>
  <el-card shadow="never" body-style="padding:8px 0">
    <div class="kpi-strip">
      <div v-for="(k, i) in items" :key="i" class="kpi-cell">
        <div class="v num">{{ k.value }}</div>
        <div class="l">
          {{ k.label }}
          <el-text v-if="k.delta" :type="k.up ? 'success' : 'danger'" size="small">{{ k.delta }}</el-text>
        </div>
      </div>
    </div>
  </el-card>
</template>

<script setup>
defineProps({ items: { type: Array, default: () => [] } })
</script>
