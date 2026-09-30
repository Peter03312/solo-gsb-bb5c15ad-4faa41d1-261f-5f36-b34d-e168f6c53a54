"""HTTP 层端到端测试：并发原子性、整场景替换、校验、回滚、OpenAPI。"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request

BASE = os.getenv("EVAC_BASE", "http://127.0.0.1:8011")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except json.JSONDecodeError:
            return e.code, {}


def assert_capacity_invariant(status):
    """逐边逐分钟校验：同时占用人数之和不超过容量，且无人占用封闭分钟。"""
    edges = {c["id"]: c for c in status["corridors"]}
    closures = status["closures"]
    load = {}
    for g in status["groups"]:
        if g["status"] == "unscheduled":
            continue
        for h in g["hops"]:
            e = edges[h["edge_id"]]
            for m in range(h["entry"], h["arrival"]):
                key = (h["edge_id"], m)
                load[key] = load.get(key, 0) + g["people"]
    for (eid, m), used in load.items():
        assert used <= edges[eid]["capacity"], (eid, m, used, edges[eid]["capacity"])
    for c in closures:
        for (eid, m) in load:
            if eid == c["edge_id"] and c["start"] <= m < c["end"]:
                raise AssertionError(f"边 {eid} 分钟 {m} 被封闭却被占用")


def test_put_scenario():
    scenario = {
        "corridors": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 2, "capacity": 3},
            {"id": 2, "src": 2, "dst": 3, "minutes": 2, "capacity": 3},
        ],
        "exits": [3],
        "closures": [],
        "groups": [
            {"id": 1, "people": 3, "source": 1, "earliest": 0, "priority": 5, "deadline": 20},
            {"id": 2, "people": 3, "source": 1, "earliest": 0, "priority": 5, "deadline": 20},
            {"id": 3, "people": 3, "source": 1, "earliest": 0, "priority": 5, "deadline": 20},
        ],
    }
    code, body = call("PUT", "/api/scenario", scenario)
    assert code == 200, body
    assert body["now"] == 0
    # 容量 3 的通道每分钟只能过一组：出发应严格错开 2 分钟
    departs = sorted(g["depart"] for g in body["groups"])
    assert departs == [0, 2, 4], departs
    assert_capacity_invariant(body)
    print("PASS test_put_scenario（错开出发:", departs, "）")


def test_concurrent_add_groups_no_oversell():
    # 干净场景：容量 3 的 2 分钟通道 1->2(出口)
    call("PUT", "/api/scenario", {
        "corridors": [{"id": 1, "src": 1, "dst": 2, "minutes": 2, "capacity": 3}],
        "exits": [2], "closures": [], "groups": [],
    })

    # 并发加入 5 个人数为 2 的组：任意两组并发提交，最终状态必须仍然
    # 满足容量（写被串行化，出发分钟依次错开）
    errors = []

    def add(gid):
        code, body = call("POST", "/api/groups", {
            "id": gid, "people": 2, "source": 1,
            "earliest": 0, "priority": 1, "deadline": 100,
        })
        if code not in (201, 409):  # 409 = 并发下同编号竞争，理论不会出现
            errors.append((gid, code, body))

    threads = [threading.Thread(target=add, args=(100 + i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    code, status = call("GET", "/api/status")
    assert code == 200
    scheduled = [g for g in status["groups"] if g["id"] >= 100 and g["status"] != "unscheduled"]
    assert len(scheduled) == 5, [g["id"] for g in scheduled]
    # 每分钟容量 3：同分钟最多 1 组（2 人），5 组须排在 0,2,4,6,8 出发
    departs = sorted(g["depart"] for g in scheduled)
    assert departs == [0, 2, 4, 6, 8], departs
    assert_capacity_invariant(status)
    print("PASS test_concurrent_add_groups_no_oversell（串行化错开:", departs, "）")


def test_validation_errors():
    call("PUT", "/api/scenario", {
        "corridors": [{"id": 1, "src": 1, "dst": 2, "minutes": 2, "capacity": 3}],
        "exits": [2], "closures": [], "groups": [],
    })
    code, body = call("POST", "/api/closures", {"edge_id": 999, "start": 0, "end": 3})
    assert code == 404, body
    code, body = call("POST", "/api/closures", {"edge_id": 1, "start": 5, "end": 5})
    assert code == 422, body  # 左闭右开 end>start
    code, body = call("POST", "/api/corridors",
                      {"id": 2, "src": 1, "dst": 2, "minutes": 0, "capacity": 3})
    assert code == 422, body  # minutes 必须正整数
    code, body = call("POST", "/api/groups",
                      {"id": 777, "people": 1, "source": 1,
                       "earliest": 10, "priority": 1, "deadline": 5})
    assert code == 422, body  # deadline < earliest
    print("PASS test_validation_errors")


def test_unschedulable_and_advance():
    scenario = {
        "corridors": [{"id": 1, "src": 1, "dst": 2, "minutes": 5, "capacity": 10}],
        "exits": [2],
        "closures": [{"edge_id": 1, "start": 0, "end": 50}],
        "groups": [
            {"id": 1, "people": 1, "source": 1, "earliest": 0, "priority": 1, "deadline": 10},
            {"id": 2, "people": 1, "source": 88, "earliest": 0, "priority": 1, "deadline": 10},
        ],
    }
    code, body = call("PUT", "/api/scenario", scenario)
    by = {g["id"]: g for g in body["groups"]}
    assert by[1]["status"] == "unscheduled" and by[1]["reason"] == "no_capacity", by[1]
    assert by[2]["reason"] == "missing_node", by[2]

    # 推进到 11（> 组1的截止 10）：组1 错过截止 => missed；不存在的起点仍是 missing_node
    code, body = call("POST", "/api/advance", {"to": 11})
    assert code == 200
    assert body["now"] == 11
    by = {g["id"]: g for g in body["groups"]}
    assert by[1]["status"] == "missed" and by[1]["reason"] == "no_capacity"
    assert by[2]["status"] == "missed" and by[2]["reason"] == "missing_node"
    print("PASS test_unschedulable_and_advance")


def test_no_partial_replan_on_rejected_closure():
    # 准备：一条会让组已出发的场景
    scenario = {
        "corridors": [
            {"id": 1, "src": 1, "dst": 2, "minutes": 2, "capacity": 5},
            {"id": 2, "src": 2, "dst": 3, "minutes": 8, "capacity": 5},
        ],
        "exits": [3],
        "closures": [],
        "groups": [
            {"id": 1, "people": 5, "source": 1, "earliest": 0, "priority": 1, "deadline": 50},
        ],
    }
    call("PUT", "/api/scenario", scenario)
    call("POST", "/api/advance", {"to": 3})  # 组1 正在 edge2 上，占用 [2,10)
    before = call("GET", "/api/status")[1]
    code, _ = call("POST", "/api/closures", {"edge_id": 2, "start": 3, "end": 9})
    assert code == 409
    after = call("GET", "/api/status")[1]
    assert len(after["closures"]) == len(before["closures"])  # 封闭没有写入
    g_before = before["groups"][0]
    g_after = after["groups"][0]
    assert g_before["hops"] == g_after["hops"] and g_before["depart"] == g_after["depart"]
    assert_capacity_invariant(after)
    print("PASS test_no_partial_replan_on_rejected_closure")


if __name__ == "__main__":
    test_put_scenario()
    test_concurrent_add_groups_no_oversell()
    test_validation_errors()
    test_unschedulable_and_advance()
    test_no_partial_replan_on_rejected_closure()
    print("\n全部 HTTP 端到端测试通过")
