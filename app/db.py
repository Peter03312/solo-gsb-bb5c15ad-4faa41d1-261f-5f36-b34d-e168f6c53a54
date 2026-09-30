"""SQLite 持久化层：场景数据 + 编排结果。

所有变更由 API 层在单一连接、单一事务中完成（配合进程内 asyncio 锁
串行化），保证"并发变更不得超用容量或留下部分重排"。
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Dict, List

from .scheduler import Assignment, Closure, Edge, Group, Model

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corridors (
    id       INTEGER PRIMARY KEY,
    src      INTEGER NOT NULL,
    dst      INTEGER NOT NULL,
    minutes  INTEGER NOT NULL,
    capacity INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS exits (
    node INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS closures (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id INTEGER NOT NULL,
    start   INTEGER NOT NULL,
    "end"   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    id        INTEGER PRIMARY KEY,
    people    INTEGER NOT NULL,
    source    INTEGER NOT NULL,
    earliest  INTEGER NOT NULL,
    priority  INTEGER NOT NULL,
    deadline  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    group_id  INTEGER PRIMARY KEY,
    depart    INTEGER,
    arrival   INTEGER,
    edge_ids  TEXT NOT NULL,
    exit_node INTEGER
);

CREATE TABLE IF NOT EXISTS assignment_hops (
    group_id INTEGER NOT NULL,
    seq      INTEGER NOT NULL,
    edge_id  INTEGER NOT NULL,
    entry    INTEGER NOT NULL,
    arrival  INTEGER NOT NULL,
    PRIMARY KEY (group_id, seq)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('now', '0')"
    )
    conn.commit()


def get_now(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='now'").fetchone()
    return int(row["value"])


def set_now(conn: sqlite3.Connection, now: int) -> None:
    conn.execute("UPDATE meta SET value=? WHERE key='now'", (str(now),))


def load_model(conn: sqlite3.Connection) -> Model:
    """从数据库组装当前场景快照（含历史安排 old_assignments）。"""
    now = get_now(conn)

    edges: Dict[int, Edge] = {}
    for r in conn.execute("SELECT * FROM corridors"):
        edges[r["id"]] = Edge(
            id=r["id"], src=r["src"], dst=r["dst"],
            minutes=r["minutes"], capacity=r["capacity"],
        )

    closures: List[Closure] = []
    for r in conn.execute("SELECT * FROM closures ORDER BY id"):
        closures.append(
            Closure(id=r["id"], edge_id=r["edge_id"],
                    start=r["start"], end=r["end"])
        )

    exits = [r["node"] for r in conn.execute("SELECT node FROM exits ORDER BY node")]

    groups: Dict[int, Group] = {}
    for r in conn.execute("SELECT * FROM groups"):
        groups[r["id"]] = Group(
            id=r["id"], people=r["people"], source=r["source"],
            earliest=r["earliest"], priority=r["priority"],
            deadline=r["deadline"],
        )

    old: Dict[int, Assignment] = {}
    for r in conn.execute("SELECT * FROM assignments"):
        hops_rows = conn.execute(
            "SELECT edge_id, entry FROM assignment_hops "
            "WHERE group_id=? ORDER BY seq", (r["group_id"],)
        ).fetchall()
        hops = [(h["edge_id"], h["entry"]) for h in hops_rows]
        old[r["group_id"]] = Assignment(
            group_id=r["group_id"],
            depart=r["depart"],
            arrival=r["arrival"],
            edge_ids=json.loads(r["edge_ids"]),
            hops=hops,
            exit_node=r["exit_node"],
        )

    fixed = {gid: a for gid, a in old.items() if a.depart < now}
    return Model(
        now=now, edges=edges, closures=closures, exits=exits,
        groups=groups, fixed=fixed, old_assignments=old,
    )


def save_plan(
    conn: sqlite3.Connection,
    edges: Dict[int, Edge],
    assignments: Dict[int, Assignment],
) -> None:
    """用新一轮编排结果整体替换 assignments / hops（事务内调用）。"""
    conn.execute("DELETE FROM assignment_hops")
    conn.execute("DELETE FROM assignments")
    for gid, a in assignments.items():
        conn.execute(
            "INSERT INTO assignments(group_id, depart, arrival, edge_ids, exit_node)"
            " VALUES (?,?,?,?,?)",
            (gid, a.depart, a.arrival, json.dumps(a.edge_ids), a.exit_node),
        )
        for seq, (edge_id, entry) in enumerate(a.hops):
            hop_arrival = entry + edges[edge_id].minutes
            conn.execute(
                "INSERT INTO assignment_hops(group_id, seq, edge_id, entry, arrival)"
                " VALUES (?,?,?,?,?)",
                (gid, seq, edge_id, entry, hop_arrival),
            )
