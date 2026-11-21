"""查询调度领域服务：任务编排、依赖链、超时控制、分区就绪诊断。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.data_lineage import (
    BLOCKING_PLAN_STATUSES,
    partition_info,
    split_list,
)
from app.store import store


class QuerySchedulerService(BaseModuleService):
    MODULE = "query_scheduler"
    ENTITY = "调度计划"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["计划名称", "查询语句", "执行周期", "依赖任务", "超时分钟", "失败重试", "优先级"]
    REQUIRED_FIELDS = ["计划名称", "查询语句", "执行周期"]
    STATUS_ORDER = ["待调度", "执行中", "已完成", "调度失败", "已暂停"]
    ACTION_RULES = {"开始执行": "执行中", "完成调度": "已完成", "标记失败": "调度失败", "暂停计划": "已暂停"}
    NEGATIVE_ACTIONS = ["标记失败"]

    # ---- 业务方法 ----

    def build_dependency_graph(self) -> dict[str, Any]:
        """构建调度计划的依赖图（数据血缘→查询调度消费）。"""
        rows = store.rows(self.MODULE)
        graph: dict[str, list[str]] = {}  # plan_name -> [dependent_plan_names]
        for row in rows:
            name = str(row.get("计划名称", ""))
            deps_raw = str(row.get("依赖任务", ""))
            deps = [d.strip() for d in deps_raw.split(",") if d.strip()]
            graph[name] = deps
        return {"ok": True, "nodes": len(rows), "graph": graph}

    def topological_order(self) -> dict[str, Any]:
        """按依赖拓扑排序调度计划（血缘关系→调度顺序）。"""
        deps = self.build_dependency_graph().get("graph", {})
        in_degree: dict[str, int] = {name: 0 for name in deps}
        for name, dep_list in deps.items():
            for dep in dep_list:
                if dep in in_degree:
                    in_degree[dep] = in_degree.get(dep, 0) + 1

        from collections import deque
        queue: deque[str] = deque(name for name, deg in in_degree.items() if deg == 0)
        order: list[str] = []
        while queue:
            node = queue.popleft()
            order.append(node)
            if node in deps:
                for dep in deps[node]:
                    in_degree[dep] -= 1
                    if in_degree[dep] == 0:
                        queue.append(dep)

        has_cycle = len(order) != len(deps)
        return {
            "ok": True,
            "plan_count": len(deps),
            "execution_order": order,
            "has_cycle": has_cycle,
            "cycle_nodes": [n for n, d in in_degree.items() if d > 0],
        }

    def get_ready_plans(self) -> list[dict[str, Any]]:
        """获取待调度且无依赖的计划。"""
        rows = store.rows(self.MODULE)
        ready: list[dict[str, Any]] = []
        for row in rows:
            if row.get("status") != "待调度":
                continue
            deps_raw = str(row.get("依赖任务", ""))
            if not deps_raw.strip():
                ready.append(row)
                continue
            dep_names = [d.strip() for d in deps_raw.split(",")]
            all_completed = all(
                _is_completed(r) for r in rows
                if str(r.get("计划名称", "")) in dep_names
            )
            if all_completed:
                ready.append(row)
        return ready

    # ---- 影响定位消费：计划完整前驱与分区诊断 ----

    def _plan_index(self) -> dict[str, dict[str, Any]]:
        return {str(row.get("计划名称", "")): row for row in store.rows(self.MODULE)}

    def _plan_target_node(self, plan: dict[str, Any]) -> str:
        return str(plan.get("_目标节点") or "").strip()

    def predecessor_plans(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        """计划的完整前驱计划（沿依赖任务递归，按拓扑从前到后）。"""
        index = self._plan_index()
        visited: set[str] = set()
        ordered: list[dict[str, Any]] = []

        def walk(name: str) -> None:
            if name in visited:
                return
            visited.add(name)
            row = index.get(name)
            if row is None:
                return
            for dep in split_list(row.get("依赖任务")):
                walk(dep)
            ordered.append(row)

        for dep in split_list(plan.get("依赖任务")):
            walk(dep)
        return ordered

    def plan_predecessors(self, plan_id: int) -> dict[str, Any]:
        """返回计划的完整前驱：前驱计划、其目标节点，以及目标节点的血缘上游闭包。"""
        plan = store.find(self.MODULE, plan_id)
        if plan is None:
            return {"ok": False, "message": f"调度计划 {plan_id} 不存在"}
        lineage = _lineage_service()
        graph = lineage.build_graph_from_store()
        target = self._plan_target_node(plan)
        predecessors = self.predecessor_plans(plan)
        plan_nodes: list[str] = []
        for pred in predecessors:
            node = self._plan_target_node(pred)
            if node and node not in plan_nodes:
                plan_nodes.append(node)
        lineage_upstream = [
            node for node in graph.upstream_closure(target)
        ] if target in graph.nodes else []
        complete_node_ids: list[str] = []
        for node in [*lineage_upstream, *plan_nodes, target]:
            if node and node not in complete_node_ids:
                complete_node_ids.append(node)
        return {
            "ok": True,
            "plan_id": plan_id,
            "plan_name": str(plan.get("计划名称", "")),
            "plan_status": str(plan.get("status", "")),
            "target_node_id": target,
            "predecessor_plans": [
                {
                    "plan_id": int(pred.get("id", 0)),
                    "plan_name": str(pred.get("计划名称", "")),
                    "plan_status": str(pred.get("status", "")),
                    "target_node_id": self._plan_target_node(pred),
                }
                for pred in predecessors
            ],
            "predecessor_node_ids": sorted(plan_nodes, key=_node_sort_key),
            "lineage_upstream_node_ids": sorted(lineage_upstream, key=_node_sort_key),
            "complete_predecessor_node_ids": complete_node_ids,
        }

    def plan_impact_locations(
        self, plan_id: int, *, page: int = 1, size: int = 20, source_id: str | None = None,
    ) -> dict[str, Any]:
        """读取影响定位数据，只保留与指定计划关联的记录。

        默认从计划每一侧上游的根变更源分别检索：同一张派生表从两个中间表都能被找到时，
        两条命中路径会同时返回，并标明计划等待的是哪一侧上游。
        """
        plan = store.find(self.MODULE, plan_id)
        if plan is None:
            return {"ok": False, "message": f"调度计划 {plan_id} 不存在"}
        lineage = _lineage_service()
        target = self._plan_target_node(plan)
        if not target:
            return {"ok": False, "message": f"调度计划 {plan_id} 未配置目标节点"}

        if source_id:
            route_sources = [(str(source_id), None)]
        else:
            route_sources = _side_roots(plan, target)
            if not route_sources:
                route_sources = [(root, None) for root in _upstream_roots(target)]

        # 每一侧直接依赖 → 其目标节点，用于判定一条命中路径究竟走的是哪一侧上游。
        index = self._plan_index()
        side_targets: list[tuple[str, str]] = []
        for dep_name in split_list(plan.get("依赖任务")):
            dep_target = self._plan_target_node(index.get(dep_name, {}))
            if dep_target:
                side_targets.append((dep_name, dep_target))

        matched: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for source, side_name in route_sources:
            payload = lineage.impact_locations(
                source, scope="path", page=1, size=10**6)
            if not payload.get("ok"):
                continue
            for location in payload["items"]:
                if location["target_node_id"] != target:
                    continue
                links = [
                    link for link in location["plan_links"]
                    if link["plan_id"] == plan_id
                ]
                if not links:
                    continue
                key = (source, location["path_text"])
                if key in seen:
                    continue
                seen.add(key)
                # 命中路径上出现的直接依赖目标，就是这次追溯所走的侧路。
                path_ids = set(location["path_node_ids"])
                matched_sides = [
                    dep_name for dep_name, dep_target in side_targets
                    if dep_target in path_ids
                ]
                enriched = dict(location)
                enriched["plan_links"] = links
                enriched["recalled_from_source_id"] = source
                enriched["recalled_from_source_name"] = payload.get("source_node_name", source)
                enriched["waits_on_this_path"] = any(
                    link["waits_on_this_path"] for link in links
                )
                enriched["matched_dependency_sides"] = matched_sides
                if matched_sides:
                    enriched["side_label"] = f"经依赖任务「{'、'.join(matched_sides)}」"
                elif enriched["waits_on_this_path"]:
                    enriched["side_label"] = "计划等待侧"
                else:
                    enriched["side_label"] = "另一侧上游（非计划等待侧）"
                enriched["is_waited_side"] = enriched["waits_on_this_path"]
                matched.append(enriched)

        matched.sort(key=lambda item: (
            _node_sort_key(item["target_node_id"]),
            item["recalled_from_source_id"],
            item["path_text"],
        ))
        page = max(1, int(page))
        size = max(1, int(size))
        start = (page - 1) * size
        return {
            "ok": True,
            "plan_id": plan_id,
            "plan_name": str(plan.get("计划名称", "")),
            "target_node_id": target,
            "target_node_name": str((_lineage_rows().get(target) or {}).get("节点名称", target)),
            "sources": sorted({source for source, _ in route_sources}, key=_node_sort_key),
            "routes": [
                {"route_id": index + 1, "source_node_id": source, "side_label": side_name}
                for index, (source, side_name) in enumerate(route_sources)
            ],
            "route_count": len(route_sources),
            "dependency_sides": [
                {"dependency_plan_name": dep_name, "dependency_target_node_id": dep_target}
                for dep_name, dep_target in side_targets
            ],
            "count_by": "path",
            "total": len(matched),
            "page": page,
            "size": size,
            "items": matched[start:start + size],
        }

    def plan_partition_diagnosis(self, plan_id: int) -> dict[str, Any]:
        """利用计划完整前驱解释本次缺少哪些分区，并核对等待数据和实际就绪时间。

        判断依据全部来自血缘的分区就绪数据和影响定位，不依赖关键词筛选：
        1. 计划目标节点的完整血缘上游里，哪些节点缺分区；
        2. 计划直接依赖的任务各自对应哪一侧上游节点；
        3. 每一侧的实际就绪时间和该侧是否已经命中目标。
        """
        plan = store.find(self.MODULE, plan_id)
        if plan is None:
            return {"ok": False, "message": f"调度计划 {plan_id} 不存在"}
        lineage = _lineage_service()
        graph = lineage.build_graph_from_store()
        rows = _lineage_rows()
        target = self._plan_target_node(plan)
        if not target:
            return {"ok": False, "message": f"调度计划 {plan_id} 未配置目标节点"}

        predecessor_info = self.plan_predecessors(plan_id)
        complete_ids = predecessor_info["complete_predecessor_node_ids"]
        upstream_ids = [
            node for node in predecessor_info["lineage_upstream_node_ids"]
        ] if target in graph.nodes else []

        upstream_details: list[dict[str, Any]] = []
        for node_id in [*upstream_ids, target]:
            row = rows.get(node_id)
            if row is None:
                continue
            info = partition_info(row)
            upstream_details.append({
                "node_id": node_id,
                "node_name": str(row.get("节点名称", node_id)),
                "node_type": str(row.get("节点类型", "")),
                "partition_key": info["partition_key"],
                "tracked": info["tracked"],
                "expected_partitions": info["expected_partitions"],
                "existing_partitions": info["existing_partitions"],
                "missing_partitions": info["missing_partitions"],
                "stale": info["stale"],
                "actual_ready_at": info["actual_ready_at"],
                "is_target": node_id == target,
            })

        missing_nodes = [item for item in upstream_details if item["missing_partitions"]]
        missing_partitions = sorted({
            part for item in missing_nodes for part in item["missing_partitions"]
        })

        # 计划直接依赖任务 → 目标节点，对应“等待的是哪一侧上游”。
        dependency_sides: list[dict[str, Any]] = []
        index = self._plan_index()
        for dep_name in split_list(plan.get("依赖任务")):
            dep = index.get(dep_name, {})
            dep_node = self._plan_target_node(dep)
            dep_info = partition_info(rows.get(dep_node)) if dep_node else None
            matches_target = bool(dep_node) and (
                dep_node == target or dep_node in set(upstream_ids)
            )
            dependency_sides.append({
                "dependency_plan_name": dep_name,
                "dependency_plan_id": int(dep.get("id", 0)) if dep else None,
                "dependency_plan_status": str(dep.get("status", "")) if dep else "",
                "waiting_node_id": dep_node,
                "waiting_node_name": str(
                    (rows.get(dep_node) or {}).get("节点名称", dep_node)
                ),
                "on_target_path": matches_target,
                "stale": bool(dep_info and dep_info["stale"]),
                "missing_partitions": dep_info["missing_partitions"] if dep_info else [],
                "actual_ready_at": dep_info["actual_ready_at"] if dep_info else "",
            })

        ready = not missing_partitions and all(
            not side["stale"] for side in dependency_sides
        ) and str(plan.get("status", "")) != "已暂停"

        return {
            "ok": True,
            "plan_id": plan_id,
            "plan_name": str(plan.get("计划名称", "")),
            "plan_status": str(plan.get("status", "")),
            "blocking": str(plan.get("status", "")) in BLOCKING_PLAN_STATUSES,
            "target_node_id": target,
            "target_node_name": str((rows.get(target) or {}).get("节点名称", target)),
            "complete_predecessor_node_ids": complete_ids,
            "missing_partitions": missing_partitions,
            "missing_node_ids": [item["node_id"] for item in missing_nodes],
            "missing_node_names": [item["node_name"] for item in missing_nodes],
            "explanation": _explain_missing(
                str(plan.get("计划名称", "")), missing_nodes, dependency_sides),
            "upstream_nodes": upstream_details,
            "dependency_sides": dependency_sides,
            "ready": ready,
            "ready_reason": (
                "所有前驱分区已就绪" if ready
                else f"仍缺少分区：{'、'.join(missing_partitions) or '无'}；"
                     f"未就绪前驱：{'、'.join(item['node_name'] for item in missing_nodes) or '无'}"
            ),
        }

    def retry_failed(self) -> dict[str, Any]:
        """重试失败的调度计划。"""
        rows = store.rows(self.MODULE)
        retried = 0
        for row in rows:
            if row.get("status") == "调度失败":
                max_retries = _parse_int(row.get("失败重试"), 3)
                retry_count = row.get("_retry_count", 0)
                if retry_count < max_retries:
                    row["_retry_count"] = retry_count + 1
                    self.run_action(int(row["id"]), "开始执行")
                    retried += 1
        return {"ok": True, "retried_count": retried}

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["artifact"] = "查询调度数据集"
        base["ready_count"] = len(self.get_ready_plans())
        base["blocking_count"] = sum(
            1 for row in store.rows(self.MODULE)
            if row.get("status") in BLOCKING_PLAN_STATUSES
        )
        return base


def _lineage_service():
    from app.services import service_for
    return service_for("data_lineage")


def _lineage_rows() -> dict[str, dict[str, Any]]:
    return {str(row.get("id", "")): row for row in store.rows("data_lineage")}


def _upstream_roots(target_id: str) -> list[str]:
    """目标节点的最上游根节点（没有上游的节点），作为影响定位的检索入口。"""
    lineage = _lineage_service()
    graph = lineage.build_graph_from_store()
    if target_id not in graph.nodes:
        return []
    closure = graph.upstream_closure(target_id)
    roots = [
        node_id for node_id in closure
        if not graph.nodes[node_id].upstream_ids
    ]
    if not roots and target_id in graph.nodes:
        roots = [target_id]
    return sorted(roots, key=_node_sort_key)


def _side_roots(plan: dict[str, Any], target_id: str) -> list[tuple[str, str | None]]:
    """按计划每一侧直接依赖，取各自上游的根变更源。

    返回 [(根节点ID, 侧标签), ...]，侧标签说明“计划等待的是哪一侧上游”。
    两条侧路都通向同一张派生表时，会得到两个不同根源、同一目标的检索入口。
    """
    index = {str(row.get("计划名称", "")): row for row in store.rows("query_scheduler")}
    lineage = _lineage_service()
    graph = lineage.build_graph_from_store()
    routes: list[tuple[str, str | None]] = []
    for dep_name in split_list(plan.get("依赖任务")):
        dep = index.get(dep_name, {})
        dep_target = str(dep.get("_目标节点") or "").strip()
        if not dep_target or dep_target not in graph.nodes:
            continue
        roots = _upstream_roots(dep_target) or [dep_target]
        for root in roots:
            routes.append((root, f"经依赖任务「{dep_name}」→ {dep_target}"))
    # 去重但保留顺序
    unique: list[tuple[str, str | None]] = []
    for route in routes:
        if route not in unique:
            unique.append(route)
    return unique


def _explain_missing(
    plan_name: str,
    missing_nodes: list[dict[str, Any]],
    dependency_sides: list[dict[str, Any]],
) -> str:
    if not missing_nodes:
        return f"计划「{plan_name}」的完整前驱分区均已就绪"
    parts: list[str] = []
    for node in missing_nodes:
        side = next(
            (s for s in dependency_sides if s["waiting_node_id"] == node["node_id"]),
            None,
        )
        segment = (
            f"{node['node_name']} 缺少分区 "
            f"{'、'.join(node['missing_partitions'])}"
        )
        if side:
            segment += f"（计划等待依赖任务「{side['dependency_plan_name']}」这一侧）"
        parts.append(segment)
    return f"计划「{plan_name}」本次缺少分区：" + "；".join(parts)


def _is_completed(row: dict[str, Any]) -> bool:
    return row.get("status") == "已完成"


def _parse_int(val: Any, default: int) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _node_sort_key(node_id: str) -> int:
    try:
        return int(node_id)
    except (ValueError, TypeError):
        return 10**9
