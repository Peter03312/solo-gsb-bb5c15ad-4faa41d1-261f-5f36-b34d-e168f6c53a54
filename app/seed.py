"""首次启动且数据库为空时写入的演示场景。

节点：1(北门) 2(东门) 3(中庭) 4(南广场·出口) 5(西侧连廊)

通道：
  1: 1 -> 3  3 分钟  容量 10
  2: 2 -> 3  2 分钟  容量 10
  3: 3 -> 4  4 分钟  容量 8
  4: 3 -> 5  5 分钟  容量 20
  5: 5 -> 4  1 分钟  容量 20
  6: 1 -> 2  2 分钟  容量 10

封闭：通道 3 在 [2, 6) 封闭。

组：
  1: 6 人，起点 1，最早 0 出发，优先级 10，截止 30
  2: 4 人，起点 1，最早 0 出发，优先级 5， 截止 30
  3: 5 人，起点 2，最早 2 出发，优先级 8， 截止 20
"""

from __future__ import annotations

import sqlite3

from . import db as dbmod
from .scheduler import replan

DEMO_CORRIDORS = [
    # (id, src, dst, minutes, capacity)
    (1, 1, 3, 3, 10),
    (2, 2, 3, 2, 10),
    (3, 3, 4, 4, 8),
    (4, 3, 5, 5, 20),
    (5, 5, 4, 1, 20),
    (6, 1, 2, 2, 10),
]
DEMO_EXITS = [4]
DEMO_CLOSURES = [(3, 2, 6)]  # edge_id, start, end
DEMO_GROUPS = [
    # (id, people, source, earliest, priority, deadline)
    (1, 6, 1, 0, 10, 30),
    (2, 4, 1, 0, 5, 30),
    (3, 5, 2, 2, 8, 20),
]


def is_empty(conn: sqlite3.Connection) -> bool:
    n = conn.execute(
        "SELECT (SELECT COUNT(*) FROM corridors) + (SELECT COUNT(*) FROM groups)"
        " AS n"
    ).fetchone()["n"]
    return n == 0


def seed_if_empty(conn: sqlite3.Connection) -> bool:
    """写入演示场景并执行一次初始编排。返回是否写入。"""
    if not is_empty(conn):
        return False
    for cid, src, dst, minutes, cap in DEMO_CORRIDORS:
        conn.execute(
            "INSERT INTO corridors(id, src, dst, minutes, capacity) VALUES (?,?,?,?,?)",
            (cid, src, dst, minutes, cap),
        )
    for node in DEMO_EXITS:
        conn.execute("INSERT INTO exits(node) VALUES (?)", (node,))
    for edge_id, start, end in DEMO_CLOSURES:
        conn.execute(
            "INSERT INTO closures(edge_id, start, \"end\") VALUES (?,?,?)",
            (edge_id, start, end),
        )
    for gid, people, src, earliest, prio, deadline in DEMO_GROUPS:
        conn.execute(
            "INSERT INTO groups(id, people, source, earliest, priority, deadline)"
            " VALUES (?,?,?,?,?,?)",
            (gid, people, src, earliest, prio, deadline),
        )
    conn.commit()

    model = dbmod.load_model(conn)
    assignments, _reasons = replan(model)
    dbmod.save_plan(conn, model.edges, assignments)
    conn.commit()
    return True
