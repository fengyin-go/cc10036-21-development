/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

export type PlanLink = {
  plan_id: number
  plan_name: string
  plan_status: string
  relation: string
  matched_path: string
  matched_path_node_ids: string[]
  waiting_node_ids: string[]
  waiting_node_names: string[]
  waits_on_this_path: boolean
  blocking: boolean
}

export type ImpactPath = {
  path_text: string
  path_node_ids: string[]
  path_node_names: string[]
  via_node_id: string
  via_node_name: string
  match_reason: string
  upstream_ready: boolean
  path_ready: boolean
  plan_ids: number[]
}

export type ImpactLocation = {
  location_id: string
  source_node_id: string
  source_node_name: string
  target_node_id: string
  target_node_name: string
  target_node_type: string
  is_table: boolean
  path_node_ids: string[]
  path_node_names: string[]
  path_text: string
  via_node_id: string
  via_node_name: string
  match_reason: string
  partition_key: string
  expected_partitions: string[]
  existing_partitions: string[]
  missing_partitions: string[]
  actual_ready_at: string
  stale: boolean
  upstream_ready: boolean
  path_ready: boolean
  plan_links: PlanLink[]
  blocking_plan_ids: number[]
  matched_paths?: ImpactPath[]
  path_count?: number
  via_node_ids?: string[]
  via_node_names?: string[]
  [key: string]: unknown
}

export type ImpactLocationPage = {
  ok: boolean
  message?: string
  source_node_id: string
  source_node_name: string
  scope: 'path' | 'table'
  count_by: 'path' | 'table'
  unit: string
  total: number
  filtered_total: number
  page: number
  size: number
  page_count: number
  items: ImpactLocation[]
  items_count: number
  keyword: string
  only_unrefreshed: boolean
  dependency_total_by_path: number
  dependency_total_by_table: number
  dependency_node_ids: string[]
  unrefreshed_node_ids: string[]
}

export type PartitionNode = {
  node_id: string
  node_name: string
  node_type: string
  partition_key: string
  tracked: boolean
  expected_partitions: string[]
  existing_partitions: string[]
  missing_partitions: string[]
  stale: boolean
  actual_ready_at: string
  is_target?: boolean
}

export type DependencySide = {
  dependency_plan_name: string
  dependency_plan_id: number | null
  dependency_plan_status: string
  waiting_node_id: string
  waiting_node_name: string
  on_target_path: boolean
  stale: boolean
  missing_partitions: string[]
  actual_ready_at: string
}

export type PlanDiagnosis = {
  ok: boolean
  message?: string
  plan_id: number
  plan_name: string
  plan_status: string
  blocking: boolean
  target_node_id: string
  target_node_name: string
  complete_predecessor_node_ids: string[]
  missing_partitions: string[]
  missing_node_ids: string[]
  missing_node_names: string[]
  explanation: string
  upstream_nodes: PartitionNode[]
  dependency_sides: DependencySide[]
  ready: boolean
  ready_reason: string
}

export type PlanPredecessors = {
  ok: boolean
  plan_id: number
  plan_name: string
  plan_status: string
  target_node_id: string
  predecessor_plans: {
    plan_id: number
    plan_name: string
    plan_status: string
    target_node_id: string
  }[]
  predecessor_node_ids: string[]
  lineage_upstream_node_ids: string[]
  complete_predecessor_node_ids: string[]
}

export type PlanLocationPage = {
  ok: boolean
  message?: string
  plan_id: number
  plan_name: string
  target_node_id: string
  target_node_name: string
  sources: string[]
  routes: { source_node_id: string; side_label: string | null }[]
  dependency_sides: { dependency_plan_name: string; dependency_target_node_id: string }[]
  count_by: 'path'
  total: number
  page: number
  size: number
  items: (ImpactLocation & {
    recalled_from_source_id: string
    recalled_from_source_name: string
    waits_on_this_path: boolean
    side_label: string
    is_waited_side: boolean
    matched_dependency_sides: string[]
  })[]
}

export type PartitionAddResult = {
  ok: boolean
  message: string
  added: boolean
  partition: string
  node: {
    id: number
    name: string
    partition_key: string
    existing_partitions: string[]
    expected_partitions: string[]
    missing_partitions: string[]
    stale: boolean
    actual_ready_at: string
  }
}
