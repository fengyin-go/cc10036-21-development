import { request } from './client'
import type {
  ImpactLocationPage,
  PartitionAddResult,
  PlanDiagnosis,
  PlanLocationPage,
  PlanPredecessors,
} from '@/data/types'

async function read<T>(response: Response, fallback: string): Promise<T> {
  const payload = (await response.json()) as T & { message?: string }
  if (!response.ok) throw new Error(payload?.message || fallback)
  return payload
}

export type ImpactLocationQuery = {
  scope?: 'path' | 'table'
  keyword?: string
  page?: number
  size?: number
  onlyUnrefreshed?: boolean
}

export async function loadImpactLocations(
  sourceId: string,
  query: ImpactLocationQuery = {},
): Promise<ImpactLocationPage> {
  const params = new URLSearchParams({
    scope: query.scope ?? 'path',
    keyword: query.keyword ?? '',
    page: String(query.page ?? 1),
    size: String(query.size ?? 10),
    only_unrefreshed: String(Boolean(query.onlyUnrefreshed)),
  })
  const response = await request(`/api/analytics/lineage/${sourceId}/locations?${params}`)
  return await read<ImpactLocationPage>(response, '影响定位读取失败')
}

export async function addNodePartition(
  nodeId: string,
  partition: string,
  readyAt?: string,
): Promise<PartitionAddResult> {
  const response = await request(`/api/analytics/lineage/${nodeId}/partitions`, {
    method: 'POST',
    body: JSON.stringify({ partition, ready_at: readyAt ?? '' }),
  })
  return await read<PartitionAddResult>(response, '补入分区失败')
}

export async function loadPlanPredecessors(planId: number): Promise<PlanPredecessors> {
  const response = await request(`/api/analytics/scheduler/plans/${planId}/predecessors`)
  return await read<PlanPredecessors>(response, '前驱计划读取失败')
}

export async function loadPlanLocations(
  planId: number,
  page = 1,
  size = 10,
): Promise<PlanLocationPage> {
  const response = await request(
    `/api/analytics/scheduler/plans/${planId}/locations?page=${page}&size=${size}`,
  )
  return await read<PlanLocationPage>(response, '计划影响定位读取失败')
}

export async function loadPlanDiagnosis(planId: number): Promise<PlanDiagnosis> {
  const response = await request(`/api/analytics/scheduler/plans/${planId}/partitions`)
  return await read<PlanDiagnosis>(response, '分区诊断读取失败')
}
