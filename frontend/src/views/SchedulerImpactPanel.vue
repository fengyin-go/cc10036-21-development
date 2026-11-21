<template>
  <section class="impact-panel" data-testid="scheduler-impact-panel">
    <header class="impact-head">
      <div>
        <h3>分区等待核对</h3>
        <p class="impact-desc">
          调度读取影响定位数据，按计划每一侧上游分别检索命中路径，
          再用完整前驱解释本次缺少哪些分区，核对等待的数据和实际就绪时间。
        </p>
      </div>
    </header>

    <form class="impact-controls" @submit.prevent="selectPlan">
      <label class="impact-field">
        <span>调度计划</span>
        <select v-model.number="planId" data-testid="scheduler-plan" @change="selectPlan">
          <option v-for="plan in plans" :key="plan.id" :value="Number(plan.id)">
            #{{ plan.id }} {{ plan['计划名称'] }} · {{ plan.status }}
          </option>
        </select>
      </label>
      <button class="btn" type="submit" data-testid="scheduler-query">核对</button>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <div v-if="diagnosis" class="analysis-summary">
      <article class="analysis-card">
        <span class="stat-label">计划状态</span>
        <strong>{{ diagnosis.plan_status }}</strong>
      </article>
      <article class="analysis-card">
        <span class="stat-label">本次缺失分区</span>
        <strong :class="diagnosis.missing_partitions.length ? 'trend-down' : 'trend-up'">
          {{ diagnosis.missing_partitions.join('、') || '无' }}
        </strong>
      </article>
      <article class="analysis-card">
        <span class="stat-label">等待数据是否就绪</span>
        <strong :class="diagnosis.ready ? 'trend-up' : 'trend-down'">
          {{ diagnosis.ready ? '已就绪' : '未就绪' }}
        </strong>
      </article>
    </div>

    <p v-if="diagnosis" class="impact-tip" data-testid="scheduler-explanation">
      {{ diagnosis.explanation }}
    </p>

    <table v-if="diagnosis" class="data-table impact-table" data-testid="scheduler-sides">
      <thead>
        <tr><th>等待侧（依赖任务）</th><th>等待节点</th><th>实际就绪时间</th><th>缺失分区</th><th>在目标路径上</th></tr>
      </thead>
      <tbody>
        <tr v-for="side in diagnosis.dependency_sides" :key="side.dependency_plan_name">
          <td>#{{ side.dependency_plan_id }} {{ side.dependency_plan_name }} · {{ side.dependency_plan_status }}</td>
          <td>{{ side.waiting_node_name }}</td>
          <td>{{ side.actual_ready_at || '—' }}</td>
          <td>
            <span v-if="side.missing_partitions.length" class="impact-badge warn">{{ side.missing_partitions.join('、') }}</span>
            <span v-else class="impact-badge ok">已齐</span>
          </td>
          <td>{{ side.on_target_path ? '是' : '否' }}</td>
        </tr>
      </tbody>
    </table>

    <h4 class="impact-subhead">影响定位命中路径（按两路检索）</h4>
    <table class="data-table impact-table">
      <thead>
        <tr><th>命中路径</th><th>检索来源</th><th>侧路</th><th>缺失分区</th><th>实际就绪时间</th></tr>
      </thead>
      <tbody>
        <tr
          v-for="item in locations?.items ?? []"
          :key="item.location_id"
          :data-testid="`scheduler-loc-${item.location_id}`"
          :data-waited="item.waits_on_this_path ? 'true' : 'false'"
        >
          <td>{{ item.path_text }}</td>
          <td>#{{ item.recalled_from_source_id }} {{ item.recalled_from_source_name }}</td>
          <td>
            <span class="impact-badge" :class="item.waits_on_this_path ? 'warn' : ''">
              {{ item.side_label }}
            </span>
          </td>
          <td>{{ item.missing_partitions.join('、') || '—' }}</td>
          <td>{{ item.actual_ready_at || '—' }}</td>
        </tr>
        <tr v-if="locations && !locations.items.length">
          <td colspan="5" class="empty-state">该计划没有可追溯的命中路径</td>
        </tr>
      </tbody>
    </table>

    <footer class="impact-pager">
      <span>共 {{ locations?.total ?? 0 }} 条路径，第 {{ locations?.page ?? 1 }} 页</span>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="scheduler-prev" :disabled="(locations?.page ?? 1) <= 1" @click="loadLocations((locations?.page ?? 1) - 1)">
          上一页
        </button>
        <button class="btn" type="button" data-testid="scheduler-next" :disabled="pageFull" @click="loadLocations((locations?.page ?? 1) + 1)">
          下一页
        </button>
      </div>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { listEntries } from '@/api/backend-service'
import { loadPlanDiagnosis, loadPlanLocations } from '@/api/impact-service'
import type { EntryRow, PlanDiagnosis, PlanLocationPage } from '@/data/types'

const plans = ref<EntryRow[]>([])
const planId = ref(1)
const diagnosis = ref<PlanDiagnosis | null>(null)
const locations = ref<PlanLocationPage | null>(null)
const errorMessage = ref('')
const pageSize = 10

const pageFull = computed(() => (locations.value?.items.length ?? 0) < pageSize)

async function loadLocations(nextPage = 1) {
  errorMessage.value = ''
  try {
    const result = await loadPlanLocations(planId.value, nextPage, pageSize)
    if (!result.ok) throw new Error(result.message || '计划影响定位读取失败')
    locations.value = result
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '计划影响定位读取失败'
  }
}

async function selectPlan() {
  errorMessage.value = ''
  diagnosis.value = null
  try {
    const result = await loadPlanDiagnosis(planId.value)
    if (!result.ok) throw new Error(result.message || '分区诊断读取失败')
    diagnosis.value = result
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分区诊断读取失败'
  }
  await loadLocations(1)
}

onMounted(async () => {
  try {
    const result = await listEntries('query_scheduler', {})
    plans.value = result.items
    if (result.items.length) planId.value = Number(result.items[0].id)
  } catch {
    plans.value = []
  }
  await selectPlan()
})
</script>
