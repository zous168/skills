<template>
  <el-steps :active="active" finish-status="success" align-center>
    <el-step v-for="s in stages" :key="s" :title="s" />
  </el-steps>
</template>

<script setup>
import { computed } from 'vue'
const props = defineProps({ stages: Array, current: String })
const active = computed(() => Math.max(props.stages.indexOf(props.current), 0))
</script>
