<template>
  <section class="impact-panel" data-testid="lineage-impact-panel">
    <header class="impact-head">
      <div>
        <h3>影响定位</h3>
        <p class="impact-desc">
          从变更源按实际命中的下游路径追查未刷新表。同一张表可由多条路径命中，
          每条路径单独说明经由哪个中间表、命中原因和关联的调度计划。
        </p>
      </div>
    </header>

    <form class="impact-controls" @submit.prevent="applyFilters">
      <label class="impact-field">
        <span>变更源节点</span>
        <select v-model="sourceId" data-testid="impact-source" @change="reload(1)">
          <option v-for="node in sourceNodes" :key="node.id" :value="String(node.id)">
            {{ node['节点名称'] }}（{{ node['节点类型'] }}）
          </option>
        </select>
      </label>
      <label class="impact-field">
        <span>统计口径</span>
        <select v-model="scope" data-testid="impact-scope" @change="reload(1)">
          <option value="path">按路径计</option>
          <option value="table">按表计</option>
        </select>
      </label>
      <label class="impact-field">
        <span>筛选词（仅筛展示条目）</span>
        <input v-model="keyword" data-testid="impact-keyword" placeholder="路径/表/计划名称" />
      </label>
      <label class="impact-field">
        <span>每页</span>
        <select v-model.number="size" data-testid="impact-size" @change="reload(1)">
          <option :value="5">5</option>
          <option :value="10">10</option>
          <option :value="20">20</option>
        </select>
      </label>
      <button class="btn" type="submit" data-testid="impact-query">查询</button>
      <label class="impact-check">
        <input v-model="onlyUnrefreshed" type="checkbox" data-testid="impact-unrefreshed" @change="reload(1)" />
        只看未刷新表
      </label>
    </form>

    <p v-if="payload" class="impact-summary" data-testid="impact-summary">
      共 <strong>{{ payload.total }}</strong> 条（按{{ payload.unit }}计），
      依赖表 {{ payload.dependency_total_by_table }} 张 / 路径 {{ payload.dependency_total_by_path }} 条；
      其中未刷新表 {{ payload.unrefreshed_node_ids.length }} 张。
      <span v-if="payload.keyword">
        当前筛选词「{{ payload.keyword }}」只筛出 {{ payload.filtered_total }} 条展示，
        依赖集合仍按 {{ payload.total }} 条计算。
      </span>
    </p>
    <p v-if="message" class="impact-tip" data-testid="impact-tip">{{ message }}</p>
    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <table class="data-table impact-table">
      <thead>
        <tr>
          <th>命中路径</th>
          <th>经由</th>
          <th>命中原因</th>
          <th>缺失分区</th>
          <th>实际就绪时间</th>
          <th>关联计划</th>
          <th>补分区</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in payload?.items ?? []"
          :key="item.location_id"
          :data-testid="`impact-row-${item.location_id}`"
          :data-stale="item.stale ? 'true' : 'false'"
        >
          <td>
            <strong>{{ item.target_node_name }}</strong>
            <div class="impact-path">{{ item.path_text }}</div>
            <span v-if="item.matched_paths" class="impact-badge">
              {{ item.path_count }} 条路径：{{ (item.via_node_names || []).join('、') }}
            </span>
          </td>
          <td>{{ item.via_node_name }}</td>
          <td>{{ item.match_reason }}</td>
          <td>
            <span v-if="item.missing_partitions.length" class="impact-badge warn">
              {{ item.missing_partitions.join('、') }}
            </span>
            <span v-else class="impact-muted">—</span>
          </td>
          <td>
            {{ item.actual_ready_at || '—' }}
            <span v-if="item.path_ready" class="impact-badge ok">已就绪</span>
          </td>
          <td>
            <div v-for="link in item.plan_links" :key="`${link.plan_id}-${link.matched_path}`" class="impact-plan">
              <span class="impact-badge" :class="link.blocking ? 'warn' : 'ok'">
                #{{ link.plan_id }} {{ link.plan_name }} · {{ link.plan_status }}
              </span>
              <span v-if="link.waits_on_this_path" class="impact-muted">计划等待这一侧</span>
              <span v-else class="impact-muted">另一侧上游</span>
            </div>
            <span v-if="!item.plan_links.length" class="impact-muted">—</span>
          </td>
          <td>
            <div v-if="item.partition_key" class="impact-add">
              <input
                v-model="partitionInputs[item.target_node_id]"
                :data-testid="`partition-input-${item.target_node_id}`"
                :placeholder="item.missing_partitions[0] || '分区值'"
              />
              <button
                class="link"
                type="button"
                :data-testid="`partition-add-${item.target_node_id}`"
                @click="submitPartition(item.target_node_id)"
              >
                补入
              </button>
            </div>
            <span v-else class="impact-muted">不按分区管理</span>
          </td>
        </tr>
        <tr v-if="payload && !payload.items.length">
          <td colspan="7" class="empty-state">当前条件下没有命中路径</td>
        </tr>
      </tbody>
    </table>

    <footer class="impact-pager">
      <span>第 {{ payload?.page ?? 1 }} / {{ payload?.page_count || 1 }} 页</span>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="impact-prev" :disabled="(payload?.page ?? 1) <= 1" @click="reload((payload?.page ?? 1) - 1)">
          上一页
        </button>
        <button class="btn" type="button" data-testid="impact-next" :disabled="(payload?.page ?? 1) >= (payload?.page_count || 1)" @click="reload((payload?.page ?? 1) + 1)">
          下一页
        </button>
      </div>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { addNodePartition, loadImpactLocations, type ImpactLocationQuery } from '@/api/impact-service'
import { listEntries } from '@/api/backend-service'
import type { EntryRow, ImpactLocationPage } from '@/data/types'

const sourceNodes = ref<EntryRow[]>([])
const sourceId = ref('1')
const scope = ref<ImpactLocationQuery['scope']>('path')
const keyword = ref('')
const size = ref(10)
const onlyUnrefreshed = ref(false)
const page = ref(1)
const payload = ref<ImpactLocationPage | null>(null)
const partitionInputs = reactive<Record<string, string>>({})
const message = ref('')
const errorMessage = ref('')

async function applyFilters() {
  await reload(1)
}

async function reload(nextPage = page.value) {
  errorMessage.value = ''
  try {
    const result = await loadImpactLocations(sourceId.value, {
      scope: scope.value,
      keyword: keyword.value,
      page: nextPage,
      size: size.value,
      onlyUnrefreshed: onlyUnrefreshed.value,
    })
    if (!result.ok) throw new Error(result.message || '影响定位读取失败')
    payload.value = result
    page.value = result.page
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '影响定位读取失败'
  }
}

async function submitPartition(nodeId: string) {
  errorMessage.value = ''
  message.value = ''
  const partition = (partitionInputs[nodeId] || '').trim()
  if (!partition) {
    errorMessage.value = '请先填写要补入的分区'
    return
  }
  try {
    const result = await addNodePartition(nodeId, partition)
    if (!result.ok) throw new Error(result.message)
    message.value = result.message
    partitionInputs[nodeId] = ''
    await reload(page.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补入分区失败'
  }
}

onMounted(async () => {
  try {
    const result = await listEntries('data_lineage', {})
    sourceNodes.value = result.items
    if (result.items.length) sourceId.value = String(result.items[0].id)
  } catch {
    sourceNodes.value = [{ id: 1, status: '在线', pending: false, abnormal: false } as EntryRow]
  }
  await reload(1)
})
</script>
