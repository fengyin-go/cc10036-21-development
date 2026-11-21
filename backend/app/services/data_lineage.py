"""数据血缘领域服务：图结构计算、影响分析、上下游追踪、影响定位。"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.analytics_engine import LineageGraph, LineageNode, _node_sort_key

# 计划处于这些状态时视为尚未闭环，可以阻塞下游取数。
BLOCKING_PLAN_STATUSES = ("待调度", "执行中", "调度失败")


def split_list(value: Any) -> list[str]:
    """把逗号分隔的配置值拆成去空的列表。"""
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def lineage_rows_by_id() -> dict[str, dict[str, Any]]:
    """血缘节点按字符串ID索引，图里的边也用字符串ID。"""
    return {str(row.get("id", "")): row for row in store.rows("data_lineage")}


def plans_by_name() -> dict[str, dict[str, Any]]:
    return {str(row.get("计划名称", "")): row for row in store.rows("query_scheduler")}


def partition_info(row: dict[str, Any] | None) -> dict[str, Any]:
    """读取节点的分区就绪信息。

    没有配置分区键和分区列表的节点（指标、报表等）不参与分区判断，
    tracked=False，也不会被当成“未刷新表”。
    """
    if row is None:
        return {
            "tracked": False, "partition_key": "", "existing_partitions": [],
            "expected_partitions": [], "missing_partitions": [],
            "stale": False, "actual_ready_at": "",
        }
    partition_key = str(row.get("分区键") or "").strip()
    existing = split_list(row.get("已有分区"))
    expected = split_list(row.get("期望分区"))
    tracked = bool(partition_key or existing or expected)
    missing = [part for part in expected if part not in existing]
    return {
        "tracked": tracked,
        "partition_key": partition_key,
        "existing_partitions": existing,
        "expected_partitions": expected,
        "missing_partitions": missing,
        "stale": bool(missing),
        "actual_ready_at": str(row.get("最近就绪时间") or ""),
    }


def _plan_dependency_targets(
    plan: dict[str, Any], plan_index: dict[str, dict[str, Any]],
) -> list[str]:
    """计划直接依赖任务对应的目标血缘节点。"""
    targets: list[str] = []
    for dep_name in split_list(plan.get("依赖任务")):
        dep = plan_index.get(dep_name)
        target = str((dep or {}).get("_目标节点") or "").strip()
        if target and target not in targets:
            targets.append(target)
    return targets


class DataLineageService(BaseModuleService):
    MODULE = "data_lineage"
    ENTITY = "血缘节点"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["节点名称", "节点类型", "上游列表", "下游列表", "数据量级", "更新频率", "负责人"]
    REQUIRED_FIELDS = ["节点名称", "节点类型", "上游列表"]
    STATUS_ORDER = ["在线", "已下线", "已停用"]
    ACTION_RULES = {"标记下线": "已下线", "停用节点": "已停用"}
    NEGATIVE_ACTIONS = ["停用节点"]

    # ---- 业务方法 ----

    def build_graph_from_store(self) -> LineageGraph:
        """从Store中构建血缘图。"""
        graph = LineageGraph()
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("status") != "在线":
                continue
            node_id = str(row.get("id", ""))
            upstream_raw = str(row.get("上游列表", ""))
            downstream_raw = str(row.get("下游列表", ""))
            node = LineageNode(
                node_id=node_id,
                node_name=str(row.get("节点名称", "")),
                node_type=str(row.get("节点类型", "")),
                upstream_ids=[u.strip() for u in upstream_raw.split(",") if u.strip()],
                downstream_ids=[d.strip() for d in downstream_raw.split(",") if d.strip()],
            )
            graph.add_node(node)
        return graph

    def compute_upstream(self, node_id: str) -> dict[str, Any]:
        """计算上游血缘链。"""
        graph = self.build_graph_from_store()
        entry = store.find(self.MODULE, int(node_id))
        if entry is None:
            return {"ok": False, "message": f"节点 {node_id} 不存在"}
        chains = graph.upstream_chain(node_id)
        return {
            "ok": True,
            "node_name": str(entry.get("节点名称", "")),
            "node_type": str(entry.get("节点类型", "")),
            "upstream_chains": chains,
            "chain_count": len(chains),
            "max_depth": max((len(c) for c in chains), default=0),
        }

    def compute_downstream(self, node_id: str) -> dict[str, Any]:
        """计算下游影响链。"""
        graph = self.build_graph_from_store()
        entry = store.find(self.MODULE, int(node_id))
        if entry is None:
            return {"ok": False, "message": f"节点 {node_id} 不存在"}
        chains = graph.downstream_chain(node_id)
        return {
            "ok": True,
            "node_name": str(entry.get("节点名称", "")),
            "node_type": str(entry.get("节点类型", "")),
            "downstream_chains": chains,
            "chain_count": len(chains),
            "max_depth": max((len(c) for c in chains), default=0),
        }

    def impact_analysis(self, node_id: str) -> dict[str, Any]:
        """变更影响分析：找出所有下游节点。"""
        graph = self.build_graph_from_store()
        entry = store.find(self.MODULE, int(node_id))
        if entry is None:
            return {"ok": False, "message": f"节点 {node_id} 不存在"}
        impacted = graph.impact_analysis(node_id)
        return {
            "ok": True,
            "node_name": str(entry.get("节点名称", "")),
            "impacted_node_ids": list(impacted),
            "impacted_count": len(impacted),
            "severity": "高" if len(impacted) > 5 else "中" if len(impacted) > 2 else "低",
        }

    # ---- 影响定位：把“下游路径”变成可追查的定位数据 ----

    def build_impact_locations(self, source_id: str) -> list[dict[str, Any]]:
        """从变更源出发，按实际命中的下游路径生成影响定位数据。

        每条定位数据保留完整路径，所以同一张派生表从两个中间表都能被找到时，
        会得到两条独立记录，各自说明“经由哪个中间表命中”以及阻塞计划等待的是哪一侧。
        定位数据每次按当前血缘关系和分区状态即时计算，补分区后重算即可生效。
        """
        graph = self.build_graph_from_store()
        rows = lineage_rows_by_id()
        source_key = str(source_id)
        if source_key not in graph.nodes:
            return []

        source_row = rows.get(source_key, {})
        source_name = str(source_row.get("节点名称", source_key))
        path_map = graph.downstream_paths(source_key)
        plan_rows = store.rows("query_scheduler")
        plan_index = plans_by_name()

        locations: list[dict[str, Any]] = []
        for target_id in sorted(path_map, key=_node_sort_key):
            target_row = rows.get(target_id, {})
            target_info = partition_info(target_row)
            target_name = str(target_row.get("节点名称", target_id))
            target_type = str(target_row.get("节点类型", ""))

            for path_index, path in enumerate(path_map[target_id]):
                path_names = [
                    str(rows.get(node_id, {}).get("节点名称", node_id)) for node_id in path
                ]
                # 路径上的中间节点（不含变更源和目标）都就绪，才认为上游这一侧可等待。
                intermediate_ids = path[1:-1]
                upstream_ready = all(
                    not partition_info(rows.get(node_id)).get("stale")
                    for node_id in intermediate_ids
                )
                via_id = path[-2] if len(path) >= 2 else source_key
                via_name = str(rows.get(via_id, {}).get("节点名称", via_id))

                plan_links: list[dict[str, Any]] = []
                for plan in plan_rows:
                    plan_target = str(plan.get("_目标节点") or "").strip()
                    if not plan_target:
                        continue
                    if plan_target == target_id:
                        relation = "直接产出"
                    elif plan_target in path:
                        relation = "路径上的前驱计划"
                    else:
                        continue
                    waiting_ids = _plan_dependency_targets(plan, plan_index)
                    matched_waiting = [nid for nid in waiting_ids if nid in path]
                    blocking = str(plan.get("status", "")) in BLOCKING_PLAN_STATUSES
                    plan_links.append({
                        "plan_id": int(plan.get("id", 0)),
                        "plan_name": str(plan.get("计划名称", "")),
                        "plan_status": str(plan.get("status", "")),
                        "relation": relation,
                        "matched_path": " → ".join(path_names),
                        "matched_path_node_ids": list(path),
                        "waiting_node_ids": matched_waiting,
                        "waiting_node_names": [
                            str(rows.get(nid, {}).get("节点名称", nid)) for nid in matched_waiting
                        ],
                        "waits_on_this_path": bool(matched_waiting),
                        "blocking": blocking,
                    })

                blocking_plan_ids = sorted({
                    link["plan_id"] for link in plan_links if link["blocking"]
                })
                locations.append({
                    "location_id": f"L{source_key}-{target_id}-{path_index + 1}",
                    "source_node_id": source_key,
                    "source_node_name": source_name,
                    "target_node_id": target_id,
                    "target_node_name": target_name,
                    "target_node_type": target_type,
                    "is_table": target_type == "table",
                    "path_node_ids": list(path),
                    "path_node_names": path_names,
                    "path_text": " → ".join(path_names),
                    "path_length": len(path) - 1,
                    "via_node_id": via_id,
                    "via_node_name": via_name,
                    "match_reason": f"从变更源 {source_name} 经 {via_name} 命中 {target_name}",
                    "partition_key": target_info["partition_key"],
                    "expected_partitions": target_info["expected_partitions"],
                    "existing_partitions": target_info["existing_partitions"],
                    "missing_partitions": target_info["missing_partitions"],
                    "actual_ready_at": target_info["actual_ready_at"],
                    "stale": target_info["stale"],
                    "upstream_ready": upstream_ready,
                    "path_ready": upstream_ready and not target_info["stale"],
                    "plan_links": plan_links,
                    "blocking_plan_ids": blocking_plan_ids,
                })
        return locations

    def _table_locations(self, locations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按表聚合定位结果：同一张表的多条命中路径合并为一行。"""
        merged: dict[str, dict[str, Any]] = {}
        for loc in locations:
            target_id = loc["target_node_id"]
            entry = merged.get(target_id)
            matched_path = {
                "path_node_ids": loc["path_node_ids"],
                "path_node_names": loc["path_node_names"],
                "path_text": loc["path_text"],
                "via_node_id": loc["via_node_id"],
                "via_node_name": loc["via_node_name"],
                "match_reason": loc["match_reason"],
                "upstream_ready": loc["upstream_ready"],
                "path_ready": loc["path_ready"],
                "plan_ids": sorted({link["plan_id"] for link in loc["plan_links"]}),
            }
            if entry is None:
                entry = {
                    "location_id": f"T{loc['source_node_id']}-{target_id}",
                    "source_node_id": loc["source_node_id"],
                    "source_node_name": loc["source_node_name"],
                    "target_node_id": target_id,
                    "target_node_name": loc["target_node_name"],
                    "target_node_type": loc["target_node_type"],
                    "is_table": loc["is_table"],
                    "partition_key": loc["partition_key"],
                    "expected_partitions": loc["expected_partitions"],
                    "existing_partitions": loc["existing_partitions"],
                    "missing_partitions": loc["missing_partitions"],
                    "actual_ready_at": loc["actual_ready_at"],
                    "stale": loc["stale"],
                    "matched_paths": [],
                    "plan_links": [],
                }
                merged[target_id] = entry
            entry["matched_paths"].append(matched_path)
            for link in loc["plan_links"]:
                dedupe_key = (link["plan_id"], link["matched_path"])
                if not any(
                    (existing["plan_id"], existing["matched_path"]) == dedupe_key
                    for existing in entry["plan_links"]
                ):
                    entry["plan_links"].append(link)

        for entry in merged.values():
            paths = entry["matched_paths"]
            entry["path_count"] = len(paths)
            entry["via_node_ids"] = [path["via_node_id"] for path in paths]
            entry["via_node_names"] = [path["via_node_name"] for path in paths]
            entry["match_reason"] = (
                f"从变更源 {entry['source_node_name']} 命中 {entry['target_node_name']}，"
                f"共 {len(paths)} 条路径（经由{'、'.join(entry['via_node_names'])}）"
            )
            entry["upstream_ready"] = all(path["upstream_ready"] for path in paths)
            entry["path_ready"] = any(path["path_ready"] for path in paths)
            entry["blocking_plan_ids"] = sorted({
                link["plan_id"] for link in entry["plan_links"] if link["blocking"]
            })
        return [merged[key] for key in sorted(merged, key=_node_sort_key)]

    def impact_locations(
        self,
        source_id: str,
        *,
        scope: str = "path",
        keyword: str = "",
        page: int = 1,
        size: int = 20,
        only_unrefreshed: bool = False,
    ) -> dict[str, Any]:
        """分页返回影响定位数据。

        scope=path 时一行一条命中路径，scope=table 时一行一张表。
        total 始终按所选口径统计完整依赖集合；keyword 只筛显展示条目，
        不会缩小依赖集合，也不会影响计划/分区的判断范围。
        """
        scope = scope if scope in ("path", "table") else "path"
        source_key = str(source_id)
        source_row = lineage_rows_by_id().get(source_key)
        if source_row is None:
            return {"ok": False, "message": f"节点 {source_id} 不存在"}

        all_locations = self.build_impact_locations(source_key)
        all_units = all_locations if scope == "path" else self._table_locations(all_locations)
        # 依赖集合始终按完整血缘关系统计，keyword 和“只看未刷新”都不会改变它。
        dependency_node_ids = sorted(
            {unit["target_node_id"] for unit in all_units}, key=_node_sort_key,
        )
        unrefreshed_node_ids = sorted(
            {unit["target_node_id"] for unit in all_units if unit.get("stale")},
            key=_node_sort_key,
        )
        units = (
            [unit for unit in all_units if unit.get("stale")]
            if only_unrefreshed else list(all_units)
        )

        keyword_text = keyword.strip().casefold()
        filtered = (
            [unit for unit in units if _matches_keyword(unit, keyword_text)]
            if keyword_text else list(units)
        )

        page = max(1, int(page))
        size = max(1, int(size))
        start = (page - 1) * size
        items = filtered[start:start + size]
        total = len(units)

        return {
            "ok": True,
            "source_node_id": source_key,
            "source_node_name": str(source_row.get("节点名称", source_key)),
            "scope": scope,
            "count_by": scope,
            "unit": "路径" if scope == "path" else "表",
            "total": total,
            "filtered_total": len(filtered),
            "page": page,
            "size": size,
            "page_count": (len(filtered) + size - 1) // size if filtered else 0,
            "items": items,
            "items_count": len(items),
            "keyword": keyword,
            "keyword_applied_to_items_only": True,
            "only_unrefreshed": only_unrefreshed,
            "dependency_total_by_path": len(all_locations),
            "dependency_total_by_table": len(self._table_locations(all_locations)),
            "dependency_node_ids": dependency_node_ids,
            "unrefreshed_node_ids": unrefreshed_node_ids,
        }

    def blocking_plans_for_source(
        self, source_id: str, *, page: int = 1, size: int = 20,
    ) -> dict[str, Any]:
        """从变更源出发，汇总通向阻塞计划的定位信息。"""
        locations = self.build_impact_locations(str(source_id))
        plans: dict[int, dict[str, Any]] = {}
        for loc in locations:
            for link in loc["plan_links"]:
                if not link["blocking"]:
                    continue
                record = plans.setdefault(link["plan_id"], {
                    "plan_id": link["plan_id"],
                    "plan_name": link["plan_name"],
                    "plan_status": link["plan_status"],
                    "blocking": True,
                    "matched_paths": [],
                    "target_node_ids": [],
                    "waiting_on_this_path": False,
                })
                record["waiting_on_this_path"] = (
                    record["waiting_on_this_path"] or link["waits_on_this_path"]
                )
                if loc["target_node_id"] not in record["target_node_ids"]:
                    record["target_node_ids"].append(loc["target_node_id"])
                path_record = {
                    "target_node_id": loc["target_node_id"],
                    "target_node_name": loc["target_node_name"],
                    "path_text": loc["path_text"],
                    "path_node_ids": loc["path_node_ids"],
                    "missing_partitions": loc["missing_partitions"],
                    "stale": loc["stale"],
                    "waits_on_this_path": link["waits_on_this_path"],
                    "waiting_node_ids": link["waiting_node_ids"],
                    "relation": link["relation"],
                }
                if not any(
                    item["path_text"] == path_record["path_text"]
                    for item in record["matched_paths"]
                ):
                    record["matched_paths"].append(path_record)

        ordered = sorted(plans.values(), key=lambda item: item["plan_id"])
        start = (max(1, int(page)) - 1) * max(1, int(size))
        return {
            "ok": True,
            "source_node_id": str(source_id),
            "total": len(ordered),
            "page": max(1, int(page)),
            "size": max(1, int(size)),
            "plans": ordered[start:start + max(1, int(size))],
        }

    def add_partition(
        self, node_id: str, partition: str, *, ready_at: str | None = None,
    ) -> dict[str, Any]:
        """给节点补入一个分区，并返回按当前关系重算后的判断数据。"""
        try:
            entry = store.find(self.MODULE, int(node_id))
        except (TypeError, ValueError):
            entry = None
        if entry is None:
            return {"ok": False, "message": f"节点 {node_id} 不存在"}
        partition = str(partition or "").strip()
        if not partition:
            return {"ok": False, "message": "缺少要补入的分区"}
        if not str(entry.get("分区键") or "").strip():
            return {"ok": False, "message": "该节点未配置分区键，无法补入分区"}

        existing = split_list(entry.get("已有分区"))
        added = partition not in existing
        if added:
            existing.append(partition)
        entry["已有分区"] = ",".join(sorted(existing))
        entry["最近就绪时间"] = (
            ready_at.strip() if ready_at and ready_at.strip()
            else _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        )
        info = partition_info(entry)
        return {
            "ok": True,
            "message": (
                f"已向 {entry.get('节点名称')} 补入分区 {partition}"
                if added else f"分区 {partition} 已存在，按实际关系重算"
            ),
            "added": added,
            "partition": partition,
            "node": {
                "id": int(entry.get("id", 0)),
                "name": str(entry.get("节点名称", "")),
                "partition_key": info["partition_key"],
                "existing_partitions": info["existing_partitions"],
                "expected_partitions": info["expected_partitions"],
                "missing_partitions": info["missing_partitions"],
                "stale": info["stale"],
                "actual_ready_at": info["actual_ready_at"],
            },
            "partition_info": info,
        }

    def get_online_nodes(self) -> list[dict[str, Any]]:
        """获取所有在线节点。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("status") == "在线"
        ]

    def export_lineage_graph(self) -> dict[str, Any]:
        """导出完整血缘图结构（供数据范围消费）。"""
        graph = self.build_graph_from_store()
        adj = graph.to_adjacency()
        return {
            "ok": True,
            "node_count": len(adj["nodes"]),
            **adj,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["artifact"] = "数据血缘数据集"
        online = sum(1 for row in store.rows(self.MODULE) if row.get("status") == "在线")
        base["online_nodes"] = online
        base["stale_nodes"] = sum(
            1 for row in store.rows(self.MODULE)
            if row.get("status") == "在线" and partition_info(row)["stale"]
        )
        return base


def _matches_keyword(unit: dict[str, Any], keyword: str) -> bool:
    """展示层关键词匹配：只看条目自身描述，不参与依赖集合计算。"""
    if not keyword:
        return True
    haystack = [
        str(unit.get("source_node_name", "")),
        str(unit.get("target_node_name", "")),
        str(unit.get("match_reason", "")),
        str(unit.get("path_text", "")),
        str(unit.get("via_node_name", "")),
    ]
    for path in unit.get("matched_paths", []):
        haystack.extend([
            str(path.get("path_text", "")),
            str(path.get("match_reason", "")),
            str(path.get("via_node_name", "")),
        ])
    for link in unit.get("plan_links", []):
        haystack.extend([
            str(link.get("plan_name", "")),
            str(link.get("plan_status", "")),
        ])
    joined = " ".join(haystack).casefold()
    return keyword in joined
