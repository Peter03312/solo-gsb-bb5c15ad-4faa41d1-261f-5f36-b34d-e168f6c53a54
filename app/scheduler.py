"""疏散通道编排核心算法（与存储解耦的纯逻辑）。

时间语义
========
* 所有时刻均为整数分钟，当前分钟 ``now`` 从 0 起。
* 通道边 ``u -> v``，通行分钟数 ``t``、每分钟容量 ``cap``。
* 某组在分钟 ``e`` 进入通道，则它在分钟 ``e, e+1, ..., e+t-1`` 占用该通道
  （"通行期间每分钟"），并在分钟 ``e+t`` 到达下一个节点。
* 封闭时段为左闭右开区间 ``[start, end)``：组在任何占用分钟内都不得落入封闭时段。
* 每条边、每分钟内，所有组人数之和不得超过该边容量。

规则
====
* 组不可拆分。
* 按 优先级(大优先) -> 最早出发分钟(小优先) -> 组编号(小优先) 的顺序逐组贪心安排。
* 每组在其规划截止分钟前（arrival <= deadline，详见 README）选最早到达的方案；
  到达相同则依次按 出口编号、通道编号序列字典序、出发分钟 择优。
* 推进当前分钟后，``depart < now`` 的组视为已出发，其路线/出发/占用全部固定；
  新增组或封闭只重排未出发组；若新封闭触及已出发组剩余路线，整次拒绝。
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple


class Reason(str, Enum):
    """无法安排的原因。"""

    NO_ROUTE = "no_route"          # 不考虑容量/封闭时也没有任何通往出口的路径
    NO_CAPACITY = "no_capacity"    # 理论上有路，但容量/封闭约束下截止前无法安排
    MISSING_NODE = "missing_node"  # 起点不存在或不是任何通道的端点
    NO_EXIT = "no_exit"            # 当前场景没有出口


@dataclass(frozen=True)
class Edge:
    """一条有向通道。"""

    id: int
    src: int
    dst: int
    minutes: int
    capacity: int


@dataclass(frozen=True)
class Closure:
    """通道封闭时段 [start, end)。"""

    id: Optional[int]
    edge_id: int
    start: int
    end: int


@dataclass
class Group:
    """人群组。"""

    id: int
    people: int
    source: int
    earliest: int      # 最早出发分钟
    priority: int      # 越大越优先
    deadline: int      # 规划截止分钟（到达分钟须 <= deadline）


@dataclass
class Assignment:
    """某组的安排结果。"""

    group_id: int
    depart: int
    arrival: int
    edge_ids: List[int]
    # 逐段 (edge_id, 进入分钟)；进入分钟 e 占用 [e, e+minutes)
    hops: List[Tuple[int, int]]
    exit_node: Optional[int] = None


@dataclass
class Model:
    """某一时刻的完整场景快照。"""

    now: int
    edges: Dict[int, Edge]
    closures: List[Closure]
    exits: List[int]
    groups: Dict[int, Group]
    fixed: Dict[int, Assignment]  # 已出发、固定不动的安排
    # 每个组已有的旧安排（无论是否固定），用于状态展示与清理
    old_assignments: Dict[int, Assignment] = field(default_factory=dict)

    def adj(self) -> Dict[int, List[Edge]]:
        """出边表（按通道编号排序，保证并列时空扩展顺序稳定）。"""
        out: Dict[int, List[Edge]] = {}
        for e in self.edges.values():
            out.setdefault(e.src, []).append(e)
            out.setdefault(e.dst, [])
        for es in out.values():
            es.sort(key=lambda x: x.id)
        return out

    def nodes(self) -> set:
        """场景中真实存在的节点：通道端点 + 出口（不包含孤立的组起点）。"""
        ns: set = set(self.exits)
        for e in self.edges.values():
            ns.add(e.src)
            ns.add(e.dst)
        return ns


def _merge_intervals(intervals: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """合并重叠/相接的 [l, r) 区间。"""
    items = sorted((l, r) for l, r in intervals if r > l)
    merged: List[Tuple[int, int]] = []
    for l, r in items:
        if merged and l <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], r))
        else:
            merged.append((l, r))
    return merged


def _runs(values: List[int]) -> List[Tuple[int, int]]:
    """把排序后的整数分钟列表转为连续区间 [start, end) 列表。"""
    out: List[Tuple[int, int]] = []
    run_start = prev = None
    for m in values:
        if run_start is None:
            run_start = prev = m
        elif m == prev + 1:
            prev = m
        else:
            out.append((run_start, prev + 1))
            run_start = prev = m
    if run_start is not None:
        out.append((run_start, prev + 1))
    return out


def _blocked_for_edge(
    edge: Edge,
    closures_by_edge: Dict[int, List[Tuple[int, int]]],
    load: Dict[int, int],
    people: int,
) -> List[Tuple[int, int]]:
    """组 ``people`` 人进入通道时不可用的分钟区间（封闭 或 容量不足）。

    封闭区间按边预先索引；容量上不可用分钟为 used + people > capacity。
    注意：当本组人数本身就超过通道容量（即使该分钟空着）时，饱和分钟
    集合虽为空，但含义是"任意分钟都不可用"，需用一个覆盖整个规划时域
    的区间表达（此处用足够大的有界区间，earliest_entry 只关心 >= 到达
    且 <= deadline 的部分）。
    """
    blocked = list(closures_by_edge.get(edge.id, ()))
    sat = [m for m, used in load.items() if used + people > edge.capacity]
    if people > edge.capacity:
        # 任何分钟都放不下本组：用覆盖非负时间轴的区间表示
        blocked.append((0, 10 ** 12))
    blocked.extend(_runs(sat))
    return _merge_intervals(blocked)


def _exit_lower_bounds(
    adj: Dict[int, List[Edge]],
    edges: Dict[int, Edge],
    exits: List[int],
) -> Dict[int, int]:
    """各节点到最近出口的纯通行时间下界（忽略封闭/容量），反图多源 Dijkstra。

    不可达出口的节点不在结果中（视为无穷大，不参与剪枝）。
    """
    rev: Dict[int, List[Tuple[int, int]]] = {}
    nodes = set()
    for e in edges.values():
        rev.setdefault(e.dst, []).append((e.src, e.minutes))
        nodes.add(e.src)
        nodes.add(e.dst)
    dist: Dict[int, int] = {x: 0 for x in exits}
    pq: List[Tuple[int, int]] = [(0, x) for x in exits]
    while pq:
        d, u = heapq.heappop(pq)
        if d != dist.get(u):
            continue
        for v, w in rev.get(u, ()):
            nd = d + w
            if nd < dist.get(v, 10 ** 18):
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist


def earliest_entry(
    edge: Edge,
    not_before: int,
    deadline: int,
    blocked: List[Tuple[int, int]],
) -> Optional[int]:
    """最早可行进入分钟。

    要求存在连续窗口 [e, e+t) 不与任何阻塞区间相交，且 e+t <= deadline。
    blocked 为已合并、裁剪好的区间列表。扫描区间之间的空隙。
    """
    t = edge.minutes
    if not_before + t > deadline:
        return None

    # 把阻塞区间裁剪到 [not_before, deadline]
    clipped: List[Tuple[int, int]] = []
    for l, r in blocked:
        if r <= not_before or l > deadline:
            continue
        clipped.append((max(l, not_before), min(r, deadline + 1)))
    clipped = _merge_intervals(clipped)

    cursor = not_before
    for l, r in clipped:
        if cursor + t <= l:
            return cursor
        cursor = max(cursor, r)
    if cursor + t <= deadline:
        return cursor
    return None


@dataclass
class _Label:
    """Dijkstra 标签：到达节点 node、到达分钟 arrival 时的一个候选方案。"""

    arrival: int
    node: int
    seq: Tuple[int, ...]        # 已走过的通道编号序列（字典序择优用）
    depart: int                 # 实际出发分钟 = 第一段通道的进入分钟；
                                # 起点空路径标签取 INF（尚未承诺出发）
    hops: Tuple[Tuple[int, int], ...]  # (edge_id, entry_minute)
    lid: int = 0                # 本次求解内的唯一编号，用于 O(1) 判活


# 起点空路径标签的出发分钟占位：尚未出发，最早只能等于下一条边的进入分钟，
# 因而不能用它支配任何已经提前出发（哪怕经过自环绕回起点）的标签。
_DEPART_UNCOMMITTED = 10 ** 15


def _dominates(a: _Label, b: _Label) -> bool:
    """a 是否在 (到达, 通道序列, 出发) 三维分量支配 b，且至少一项严格。

    三个属性沿路径扩展均不减小（到达严格增、序列只追加、出发一旦确定
    不再变），而终点恰按这三者（连同出口编号）定序，故可安全地做
    Pareto 剪枝；封闭会迫使等待，使任一维都可能成为独立优势，
    不能用单一字典序互相淘汰。
    """
    if a.arrival > b.arrival or a.seq > b.seq or a.depart > b.depart:
        return False
    return (
        a.arrival < b.arrival
        or a.seq < b.seq
        or a.depart < b.depart
    )


def _try_insert(
    fronts: Dict[int, List[_Label]],
    active: Dict[int, set],
    lab: _Label,
) -> bool:
    """把标签加入节点的 Pareto 前沿；被支配则丢弃，返回是否加入。

    fronts 保存有序前沿用于支配比较，active 保存存活标签编号集合用于
    堆弹出时 O(1) 判断标签是否已被淘汰。
    """
    front = fronts.setdefault(lab.node, [])
    alive = active.setdefault(lab.node, set())
    for other in front:
        if _dominates(other, lab):
            return False
    removed_ids = {other.lid for other in front if _dominates(lab, other)}
    for rid in removed_ids:
        alive.discard(rid)
    if removed_ids:
        front[:] = [other for other in front if other.lid not in removed_ids]
    front.append(lab)
    alive.add(lab.lid)
    return True


def _reachable(adj: Dict[int, List[Edge]], source: int, exits: set) -> bool:
    """不考虑封闭/容量，是否存在 source -> 任意出口的有向路径（含 source 即出口）。"""
    if source in exits:
        return True
    seen = {source}
    stack = [source]
    while stack:
        u = stack.pop()
        for e in adj.get(u, ()):  # type: ignore[union-attr]
            if e.dst not in seen:
                if e.dst in exits:
                    return True
                seen.add(e.dst)
                stack.append(e.dst)
    return False


def schedule_group(
    model: Model,
    group: Group,
    load: Dict[int, Dict[int, int]],
    closures_by_edge: Optional[Dict[int, List[Tuple[int, int]]]] = None,
    h_bounds: Optional[Dict[int, int]] = None,
) -> Optional[Assignment]:
    """为单个组求满足约束的最优安排；无法在截止前安排返回 None。

    时间依赖最早到达的多标签 Dijkstra：边的最早可行进入时间关于"可出发时刻"
    单调（FIFO），故标签按到达分钟弹出即为定标；每个中间节点保留
    (到达, 通道序列) 两维 Pareto 前沿以正确处理等待反超，出发分钟不参与
    中间剪枝。终点按 到达 -> 出口编号 -> 通道序列字典序 -> 出发 字典序取最优。

    closures_by_edge：封闭区间按边的索引（可在多组求解间复用）；
    h_bounds：节点到最近出口的纯通行时间下界，用于安全的 A* 剪枝。
    """
    adj = model.adj()
    exit_set = set(model.exits)
    if closures_by_edge is None:
        closures_by_edge = {}
        for c in model.closures:
            closures_by_edge.setdefault(c.edge_id, []).append((c.start, c.end))
    blocked_cache: Dict[int, List[Tuple[int, int]]] = {}
    next_lid = itertools.count(1)

    if group.source not in model.nodes():
        return None
    if not model.exits:
        return None
    if not _reachable(adj, group.source, exit_set):
        return None

    # 起点即出口：零通道路线，到达 = max(now, earliest)，但出发不得晚于截止
    start_depart = max(model.now, group.earliest)
    if group.source in exit_set:
        if start_depart <= group.deadline:
            return Assignment(
                group_id=group.id,
                depart=start_depart,
                arrival=start_depart,
                edge_ids=[],
                hops=[],
                exit_node=group.source,
            )
        return None

    start = _Label(
        start_depart, group.source, (), _DEPART_UNCOMMITTED, (),
        lid=next(next_lid),
    )
    fronts: Dict[int, List[_Label]] = {group.source: [start]}
    active: Dict[int, set] = {group.source: {start.lid}}
    # 堆项: (arrival, seq, depart, node, label)
    pq: List[Tuple] = []
    heapq.heappush(pq, (start.arrival, start.seq, start_depart, start.node, start))

    best_goal: Optional[_Label] = None
    goal_exit: Optional[int] = None

    while pq:
        arrival, _seq, _depart, node, lab = heapq.heappop(pq)
        if lab.lid not in active.get(node, ()):
            continue  # 已从 Pareto 前沿淘汰的过期标签
        if best_goal is not None:
            # 严格更晚到达才可能安全停止：等于最优到达时，仍可能通过额外的边
            # （含自环）走出同到达但出口编号/通道序列更小的方案。
            if arrival > best_goal.arrival:
                break
            # 下界剪枝：即使后续一路无等待也无法在最优到达之前/同时到出口。
            # 用严格大于：相等时保留，因为还可能以更小出口编号/序列同分胜出。
            if h_bounds is not None:
                bound = h_bounds.get(node, 10 ** 18)
                if arrival + bound > best_goal.arrival:
                    continue

        # 该标签在截止分钟内根本到不了任何出口 => 扩展无意义（对无解情形
        # 的关键剪枝，避免穷举到 deadline）。
        if h_bounds is not None and arrival + h_bounds.get(node, 10 ** 18) > group.deadline:
            continue

        for edge in adj.get(node, ()):
            blocked = blocked_cache.get(edge.id)
            if blocked is None:
                blocked = _blocked_for_edge(
                    edge, closures_by_edge,
                    load.get(edge.id, {}), group.people,
                )
                blocked_cache[edge.id] = blocked
            entry = earliest_entry(edge, arrival, group.deadline, blocked)
            if entry is None:
                continue
            nxt_arrival = entry + edge.minutes
            # 已经存在同到达更优候选时，严格更晚到达的任何方案都不可能胜出
            # （含出口边），提前丢弃；这也保证堆最终收敛。
            if best_goal is not None and nxt_arrival > best_goal.arrival:
                continue
            nxt = _Label(
                arrival=nxt_arrival,
                node=edge.dst,
                seq=lab.seq + (edge.id,),
                depart=entry if lab.depart == _DEPART_UNCOMMITTED else lab.depart,
                hops=lab.hops + ((edge.id, entry),),
                lid=next(next_lid),
            )

            if edge.dst in exit_set:
                # 终点择优键：到达 -> 出口编号 -> 通道序列字典序 -> 出发
                candidate_key = (
                    nxt.arrival,
                    edge.dst,
                    nxt.seq,
                    nxt.depart,
                )
                current = None if best_goal is None else (
                    best_goal.arrival, goal_exit, best_goal.seq, best_goal.depart
                )
                if current is None or candidate_key < current:
                    best_goal = nxt
                    goal_exit = edge.dst
                continue

            if _try_insert(fronts, active, nxt):
                heapq.heappush(
                    pq, (nxt.arrival, nxt.seq, nxt.depart, nxt.node, nxt)
                )

    if best_goal is None:
        return None
    return Assignment(
        group_id=group.id,
        depart=best_goal.depart,
        arrival=best_goal.arrival,
        edge_ids=list(best_goal.seq),
        hops=list(best_goal.hops),
        exit_node=goal_exit,
    )


def add_load(
    load: Dict[int, Dict[int, int]],
    asn: Assignment,
    edges: Dict[int, Edge],
    people: int,
) -> None:
    for edge_id, entry in asn.hops:
        minutes = edges[edge_id].minutes
        bucket = load.setdefault(edge_id, {})
        for m in range(entry, entry + minutes):
            bucket[m] = bucket.get(m, 0) + people


def closure_conflicts_fixed(model: Model, new_closures: List[Closure]) -> List[int]:
    """检查（追加的）封闭是否触及已出发组的剩余路线。

    剩余路线：组在当前分钟 model.now 及之后仍然占用的通行分钟
    （depart < now 的组才是"已出发"）。返回冲突的组编号列表。
    """
    # edge_id -> [(剩余占用起点, 剩余占用终点, 组编号)]
    hop_index: Dict[int, List[Tuple[int, int, int]]] = {}
    for gid, asn in model.fixed.items():
        for edge_id, entry in asn.hops:
            t = model.edges[edge_id].minutes
            rem_start = max(entry, model.now)
            rem_end = entry + t
            if rem_start < rem_end:
                hop_index.setdefault(edge_id, []).append((rem_start, rem_end, gid))

    conflicts: set = set()
    for c in new_closures:
        for rem_start, rem_end, gid in hop_index.get(c.edge_id, ()):
            if c.start < rem_end and rem_start < c.end:  # [a,b) ∩ [l,r) 非空
                conflicts.add(gid)
    return sorted(conflicts)


def replan(
    model: Model,
) -> Tuple[Dict[int, Assignment], Dict[int, Reason]]:
    """执行（重）编排。

    固定组（depart < now）保留原安排并先占用容量；其余组按
    优先级 -> 最早出发 -> 组编号 排序后逐组贪心安排。
    返回 (assignments, reasons)：assignments 含全部已安排组（固定+新排），
    reasons 给出未安排组的原因。
    """
    assignments: Dict[int, Assignment] = {}
    reasons: Dict[int, Reason] = {}
    load: Dict[int, Dict[int, int]] = {}

    # 本轮复用：封闭按边索引、节点到最近出口的纯通行时间下界
    closures_by_edge: Dict[int, List[Tuple[int, int]]] = {}
    for c in model.closures:
        closures_by_edge.setdefault(c.edge_id, []).append((c.start, c.end))
    adj_for_bounds = model.adj()
    h_bounds = _exit_lower_bounds(adj_for_bounds, model.edges, model.exits)

    # 1) 已出发组固定（其剩余占用才会影响未来，但整段计入也无妨：
    #    未出发组 depart >= now，永远不可能进入 < now 的分钟）
    for gid in sorted(model.fixed):
        asn = model.old_assignments[gid]
        g = model.groups[gid]
        assignments[gid] = asn
        add_load(load, asn, model.edges, g.people)

    # 2) 未出发组按规则排序
    pending = [
        g for g in model.groups.values() if g.id not in assignments
    ]
    pending.sort(key=lambda g: (-g.priority, g.earliest, g.id))

    for g in pending:
        # 明显的前置判定，给出精确原因
        if not model.exits:
            reasons[g.id] = Reason.NO_EXIT
            continue
        if g.source not in model.nodes():
            reasons[g.id] = Reason.MISSING_NODE
            continue
        if not _reachable(adj_for_bounds, g.source, set(model.exits)):
            reasons[g.id] = Reason.NO_ROUTE
            continue
        asn = schedule_group(model, g, load, closures_by_edge, h_bounds)
        if asn is None:
            reasons[g.id] = Reason.NO_CAPACITY
            continue
        assignments[g.id] = asn
        add_load(load, asn, model.edges, g.people)

    return assignments, reasons


def ordering(model: Model, assignments: Dict[int, Assignment]) -> List[int]:
    """状态展示用的编排顺序：固定组按组编号，其后按调度优先级。"""
    fixed = sorted(gid for gid, a in assignments.items() if a.depart < model.now)
    rest = sorted(
        (g for g in model.groups.values() if g.id not in fixed),
        key=lambda g: (-g.priority, g.earliest, g.id),
    )
    return fixed + [g.id for g in rest]
