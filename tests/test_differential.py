"""优化后求解器 vs 朴素参考求解器的随机差分测试。

参考实现枚举每个 (节点, 到达分钟) 状态下的每个可行进入分钟，
不做任何剪枝；目标全序键与正式实现一致。
"""

from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.scheduler as S  # noqa: E402
from app.scheduler import Closure, Edge, Group, Model  # noqa: E402


def brute(model, group, load):
    adj = model.adj()
    exits = set(model.exits)
    if group.source not in model.nodes() or not exits:
        return None
    start = max(model.now, group.earliest)
    if group.source in exits:
        return (start, group.source, (), start) if start <= group.deadline else None
    states = {(group.source, start): ((), None)}
    best = None
    frontier = [(group.source, start)]
    while frontier:
        nf = []
        for node, arrival in frontier:
            seq, depart = states[(node, arrival)]
            for e in adj.get(node, ()):
                blocked = [(c.start, c.end) for c in model.closures if c.edge_id == e.id]
                sat = [m for m, u in load.get(e.id, {}).items()
                       if u + group.people > e.capacity]
                oversized = group.people > e.capacity
                hi = group.deadline - e.minutes
                for ent in range(arrival, hi + 1):
                    if oversized:
                        continue
                    if any(ent <= m < ent + e.minutes for m in sat):
                        continue
                    if any(not (ent + e.minutes <= l or ent >= r) for l, r in blocked):
                        continue
                    na = ent + e.minutes
                    nseq = seq + (e.id,)
                    ndep = ent if depart is None else depart
                    if e.dst in exits:
                        key = (na, e.dst, nseq, ndep)
                        if best is None or key < best:
                            best = key
                    else:
                        st = (e.dst, na)
                        old = states.get(st)
                        if old is None or (nseq, ndep) < (old[0], old[1]):
                            states[st] = (nseq, ndep)
                            nf.append(st)
        frontier = nf
    return best


def random_case(rng):
    V = rng.randint(2, 5)
    E = rng.randint(0, 9)
    edges = {}
    for eid in range(1, E + 1):
        edges[eid] = Edge(
            eid, rng.randint(1, V), rng.randint(1, V),
            rng.randint(1, 4), rng.randint(1, 6),
        )
    exits = list({rng.randint(1, V) for _ in range(rng.randint(1, 2))})
    closures = []
    for eid in rng.sample(list(edges), rng.randint(0, min(2, len(edges)))):
        a = rng.randint(0, 8)
        closures.append(Closure(None, eid, a, a + rng.randint(1, 4)))
    g = Group(1, rng.randint(1, 7), rng.randint(1, V),
              rng.randint(0, 3), 1, rng.randint(4, 14))
    # 随机既有负载（模拟前面已排组）
    load = {}
    for eid in rng.sample(list(edges), rng.randint(0, len(edges))):
        cap = edges[eid].capacity
        for m in rng.sample(range(15), rng.randint(0, 4)):
            load.setdefault(eid, {})[m] = rng.randint(1, cap + 2)
    return Model(0, edges, closures, exits, {g.id: g}, {}, {}), g, load


def test_random_vs_brute(seed=123, trials=1500):
    rng = random.Random(seed)
    for it in range(trials):
        model, g, load = random_case(rng)
        cbi = {}
        for c in model.closures:
            cbi.setdefault(c.edge_id, []).append((c.start, c.end))
        h = S._exit_lower_bounds(model.adj(), model.edges, model.exits)
        got = S.schedule_group(model, g, load, cbi, h)
        want = brute(model, g, load)
        got_key = None if got is None else (
            got.arrival, got.exit_node, tuple(got.edge_ids), got.depart)
        assert got_key == want, (
            f"trial {it} seed {seed}\n edges={list(model.edges.values())}\n"
            f" exits={model.exits}\n closures={model.closures}\n group={g}\n"
            f" load={load}\n fast={got_key}\n brute={want}"
        )
    print(f"PASS test_random_vs_brute（{trials} 个随机场景一致）")


if __name__ == "__main__":
    test_random_vs_brute()
