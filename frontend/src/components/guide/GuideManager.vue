<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { useGuideStore } from '@/stores'
import GuideWelcome from './GuideWelcome.vue'
import FlowGuide from './FlowGuide.vue'
import RegionGuide from './RegionGuide.vue'

const guide = useGuideStore()
const showWelcome = ref(false)
const showFlow = ref(false)
const showRegion = ref(false)

onMounted(() => {
  if (guide.pending === 'flow') { showFlow.value = true; guide.clearPending() }
  else if (guide.pending === 'region') { showRegion.value = true; guide.clearPending() }
  else if (!guide.completed) { showWelcome.value = true }
})

watch(() => guide.pending, (v) => {
  if (!v) return
  showWelcome.value = false
  showFlow.value = false
  showRegion.value = false
  if (v === 'flow') showFlow.value = true
  else if (v === 'region') showRegion.value = true
  guide.clearPending()
})

function startFlow() { showWelcome.value = false; showFlow.value = true }
function startRegion() { showWelcome.value = false; showRegion.value = true }
function skipWelcome() { guide.markCompleted(); showWelcome.value = false }
function closeFlow() { guide.markCompleted(); showFlow.value = false }
function closeRegion() { guide.markCompleted(); showRegion.value = false }
</script>

<template>
  <GuideWelcome :open="showWelcome" @start-flow="startFlow" @start-region="startRegion" @skip="skipWelcome" />
  <FlowGuide :open="showFlow" @close="closeFlow" />
  <RegionGuide :open="showRegion" @close="closeRegion" />
</template>