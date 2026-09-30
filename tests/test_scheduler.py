"""编排核心算法的单元测试：python -m pytest tests/ （无 pytest 时可直接运行）。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.scheduler import (  # noqa: E402
    Assignment,
    Closure,
    Edge,
    Group,
    Model,
    Reason,
    closure_conflicts_fixed,
    earliest_entry,
    replan,
    schedule_group,
)


def model(edges, groups, exits, closures=(), now=0, fixed=()):
    edge_map = {e.id: e for e in edges}
    group_map = {g.id: g for g in groups}
    old = {a.group_id: a for a in fixed}
    return Model(
        now=now, edges=edge_map, closures=list(closures), exits=list(exits),
        groups=group_map, fixed=dict(old),
        old_assignments=old,
    )


def test_earliest_entry_blocked():
    e = Edge(1, 1, 2, 3, 10)
    # [2,6) 封闭；0 进入会占用 0,1,2 -> 撞封闭；最早 6 进入，9 到达
    assert earliest_entry(e, 0, 30, [(2, 6)]) == 6
    # 容量形成的阻塞区间同样处理
    assert earliest_entry(e, 0, 8, [(2, 6)]) is None  # 6+3 > 8
    assert earliest_entry(e, 0, 9, [(2, 6)]) == 6


def test_basic_route_and_tie_exit():
    # 1 -> 2 (edge1, 2 分钟), 1 -> 3 (edge2, 3 分钟)；2 和 3 都是出口
    edges = [Edge(1, 1, 2, 2, 10), Edge(2, 1, 3, 3, 10)]
    m = model(edges, [], exits=[3, 2])
    g = Group(1, 5, 1, 0, 1, 30)
    a = schedule_group(m, g, {})
    assert (a.arrival, a.exit_node, a.edge_ids) == (2, 2, [1])

    # 两条路同到达时，出口编号小者优先
    edges = [Edge(1, 1, 2, 2, 10), Edge(2, 1, 3, 2, 10)]
    m = model(edges, [], exits=[3, 2])
    a = schedule_group(m, g, {})
    assert (a.arrival, a.exit_node, a.edge_ids) == (2, 2, [1])


def test_tie_corridor_sequence():
    # 两条并行路径同到达出口 4：edge1(1->3,1)+edge2(3->4,2) 序列[1,2]
    # edge3(1->5,1)+edge4(5->4,2) 序列[3,4]；应选 [1,2]
    edges = [
        Edge(1, 1, 3, 1, 10), Edge(2, 3, 4, 2, 10),
        Edge(3, 1, 5, 1, 10), Edge(4, 5, 4, 2, 10),
    ]
    m = model(edges, [], exits=[4])
    a = schedule_group(m, Group(1, 1, 1, 0, 1, 30), {})
    assert a.arrival == 3 and a.edge_ids == [1, 2]


def test_capacity_per_minute_sharing():
    # 通道 edge1: 1->2, 4 分钟, 容量 10
    edges = [Edge(1, 1, 2, 4, 10)]
    # A 组 6 人；B 组 6 人：A 进入分钟 0 占 [0,4)，
    # B 在分钟 0,1,2,3 任一进入都有重叠 => 容量 12 > 10，最早 4 进入
    groups = [
        Group(1, 6, 1, 0, 10, 30),
        Group(2, 6, 1, 0, 5, 30),
    ]
    m = model(edges, groups, exits=[2])
    assignments, reasons = replan(m)
    assert not reasons
    assert assignments[1].depart == 0 and assignments[1].arrival == 4
    assert assignments[2].depart == 4 and assignments[2].arrival == 8

    # 容量允许错峰共用：A 6 人 [0,4)，B 4 人可同时段
    groups2 = [Group(1, 6, 1, 0, 10, 30), Group(2, 4, 1, 0, 5, 30)]
    m2 = model(edges, groups2, exits=[2])
    a2, _ = replan(m2)
    assert a2[2].depart == 0  # 6+4=10，恰好不超


def test_waiting_underdog_label_wins():
    """关键反例：早到中间节点但序列更大的标签，可能因封闭等待被晚到者反超。

    直达 edge3: 1 -> 3 (1 分钟，序列前缀 [3])；
    绕行 edge1: 1 -> 2 (1) + edge2: 2 -> 3 (1)（前缀 [1,2]，2 分钟到 3）；
    edge4: 3 -> 4(出口, 1 分钟)，edge4 在 [1,2) 封闭。
    直达者分钟 1 到 3，最早进入 edge4 为 2，3 到出口；
    绕行者分钟 2 到 3，同样分钟 2 进入 edge4，3 到出口。
    到达相同 => 比通道序列：[1,2,4] < [3,4] => 绕行方案胜。
    两个中间标签在 (到达, 序列) 上互不可支配；若错误地用"到达早即支配"
    剪掉分钟 2 的标签，就会漏掉这个更优方案。
    """
    edges = [
        Edge(1, 1, 2, 1, 10), Edge(2, 2, 3, 1, 10),
        Edge(3, 1, 3, 1, 10), Edge(4, 3, 4, 1, 10),
    ]
    m = model(edges, [], exits=[4], closures=[Closure(1, 4, 1, 2)])
    a = schedule_group(m, Group(1, 1, 1, 0, 1, 30), {})
    assert (a.arrival, a.edge_ids) == (3, [1, 2, 4])


def test_closure_forces_detour_and_capacity_failure():
    # edge3(3->4) 在 [2,6) 封闭；直走被推迟，绕行 edge4(3->5,5)+edge5(5->4,1)
    edges = [
        Edge(1, 1, 3, 3, 10), Edge(3, 3, 4, 4, 8),
        Edge(4, 3, 5, 5, 20), Edge(5, 5, 4, 1, 20),
    ]
    m = model(edges, [], exits=[4], closures=[Closure(None, 3, 2, 6)])
    a = schedule_group(m, Group(1, 6, 1, 0, 1, 30), {})
    # 直达：到 3 在分钟 3，edge3 最早进入 6，10 到；绕行：0+3+5+1=9 到
    assert a.arrival == 9 and a.edge_ids == [1, 4, 5]

    # 截止只给到 8 => 无解
    a2 = schedule_group(m, Group(2, 6, 1, 0, 1, 8), {})
    assert a2 is None


def test_group_larger_than_edge_capacity():
    # 组人数 5：edge1 容量 3 任何分钟都不可用；只能走容量 10 的 edge2
    edges = [Edge(1, 1, 2, 1, 3), Edge(2, 1, 2, 2, 10)]
    m = model(edges, [Group(1, 5, 1, 0, 1, 30)], exits=[2])
    a, reasons = replan(m)
    assert not reasons
    assert a[1].edge_ids == [2] and a[1].arrival == 2

    # 唯一通道容量不足 => NO_CAPACITY
    m2 = model([edges[0]], [Group(2, 5, 1, 0, 1, 30)], exits=[2])
    _, r2 = replan(m2)
    assert r2[2] == Reason.NO_CAPACITY


def test_priority_order_greedy():
    # 容量 6 的 4 分钟通道：高优先级独占，低优先级被推迟
    edges = [Edge(1, 1, 2, 4, 6)]
    groups = [
        Group(1, 6, 1, 0, 1, 30),   # 低优先级
        Group(2, 6, 1, 0, 9, 30),   # 高优先级
        Group(3, 6, 1, 0, 9, 30),   # 同级、同最早出发 => 编号小者先
    ]
    m = model(edges, groups, exits=[2])
    a, reasons = replan(m)
    assert not reasons
    assert (a[2].depart, a[2].arrival) == (0, 4)
    assert (a[3].depart, a[3].arrival) == (4, 8)
    assert (a[1].depart, a[1].arrival) == (8, 12)


def test_unschedulable_reasons():
    # 有路但截止太紧 => NO_CAPACITY（封闭导致）
    edges = [Edge(1, 1, 2, 5, 10)]
    groups = [Group(1, 1, 1, 0, 1, 4)]
    m = model(edges, groups, exits=[2], closures=[Closure(None, 1, 0, 100)])
    a, reasons = replan(m)
    assert 1 not in a and reasons[1] == Reason.NO_CAPACITY

    # 起点根本不在图里 => MISSING_NODE
    m2 = model(edges, [Group(2, 1, 99, 0, 1, 30)], exits=[2])
    _, r2 = replan(m2)
    assert r2[2] == Reason.MISSING_NODE

    # 图里有起点但到不了出口 => NO_ROUTE
    edges2 = edges + [Edge(2, 1, 7, 1, 10)]
    m3 = model(edges2, [Group(3, 1, 7, 0, 1, 30)], exits=[2])
    _, r3 = replan(m3)
    assert r3[3] == Reason.NO_ROUTE


def test_advance_freezes_and_closure_conflict():
    edges = [Edge(1, 1, 2, 4, 10), Edge(2, 2, 3, 4, 10)]
    groups = [Group(1, 5, 1, 0, 10, 30)]
    m = model(edges, groups, exits=[3])
    a0, _ = replan(m)
    asn = a0[1]
    assert asn.edge_ids == [1, 2] and asn.hops == [(1, 0), (2, 4)]

    # 推进到 3：组已出发（depart 0 < 3），固定不变
    m2 = model(edges, groups, exits=[3], now=3, fixed=(asn,))
    a1, r1 = replan(m2)
    assert a1[1].hops == [(1, 0), (2, 4)]

    # edge2 剩余占用为 [4,8)；封闭 [6,9) 触及 => 冲突
    c = Closure(None, 2, 6, 9)
    assert closure_conflicts_fixed(m2, [c]) == [1]
    # 封闭 [8,10) 不碰剩余路线（edge2 占用到 8，左闭右开）=> 不冲突
    assert closure_conflicts_fixed(m2, [Closure(None, 2, 8, 10)]) == []
    # 封闭只触及已经走完的 edge1 历史部分 [0,3)（剩余占用从 3 起）=> 不冲突
    assert closure_conflicts_fixed(m2, [Closure(None, 1, 0, 3)]) == []
    # [0,4) 则会覆盖剩余占用 [3,4) => 冲突
    assert closure_conflicts_fixed(m2, [Closure(None, 1, 0, 4)]) == [1]


def test_replan_only_unfixed_after_closure():
    edges = [
        Edge(1, 1, 2, 4, 10), Edge(2, 2, 3, 4, 10),
        Edge(3, 1, 3, 9, 10),
    ]
    groups = [Group(1, 5, 1, 0, 10, 30), Group(2, 5, 1, 0, 5, 30)]
    m = model(edges, groups, exits=[3])
    a0, _ = replan(m)
    # 两组都走 edge1,edge2：各 5 人，容量 10 允许同时通行，都在 0 出发
    assert a0[1].hops == [(1, 0), (2, 4)]
    assert a0[2].hops == [(1, 0), (2, 4)]

    # 推进到 5：组1 固定（edge2 剩余占用 [5,8)）；组2 未出发需重排
    m2 = model(edges, groups, exits=[3], now=5, fixed=(a0[1],))
    # edge1 加封闭 [4,8)：不碰组1剩余路线（组1在edge1的占用已结束）
    assert closure_conflicts_fixed(m2, [Closure(None, 1, 4, 8)]) == []
    m2.closures.append(Closure(None, 1, 4, 8))
    a1, reasons = replan(m2)
    # 组1 原样固定
    assert a1[1].hops == [(1, 0), (2, 4)]
    # 组2 重排：edge1 要等到 8 才能进入 => 8+4+4=16；edge3 直达：
    # 最早出发 max(now=5, earliest=0)=5，5+9=14 => 选 edge3
    assert a1[2].edge_ids == [3] and a1[2].depart == 5 and a1[2].arrival == 14


def test_start_is_exit_empty_route():
    edges = [Edge(1, 1, 2, 3, 10)]
    m = model(edges, [Group(1, 5, 2, 0, 1, 30)], exits=[2])
    a, reasons = replan(m)
    assert not reasons
    assert a[1].edge_ids == [] and a[1].depart == 0 and a[1].arrival == 0

    # now=6 时，零通道路线的出发/到达取 max(now, earliest)
    m2 = model(edges, [Group(2, 5, 2, 0, 1, 30)], exits=[2], now=6)
    a2, _ = replan(m2)
    assert a2[2].depart == 6 and a2[2].arrival == 6


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n全部 {len(fns)} 个测试通过")
