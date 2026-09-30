"""场馆疏散通道编排服务 —— FastAPI 入口。

并发安全：进程内 asyncio 锁串行化全部写操作；每个写操作在单个 SQLite
事务中完成"改数据 -> 整轮重排 -> 落库 -> 提交"，任何一步失败即回滚，
因此不会出现超用容量或只重排了一部分的中间状态。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException

from . import db as dbmod
from . import seed
from .scheduler import Assignment, Closure, Reason, closure_conflicts_fixed, ordering, replan
from .schemas import (
    AdvanceIn,
    ClosureIn,
    CorridorIn,
    GroupIn,
    MessageOut,
    ScenarioIn,
    StatusOut,
)

DB_PATH = os.getenv("EVAC_DB_PATH", "/data/evacuation.db")

REASON_DETAIL = {
    Reason.NO_ROUTE: "即使不考虑容量与封闭，也没有从起点通往任何出口的有向路径",
    Reason.NO_CAPACITY: "在容量与封闭时段约束下，无法在规划截止分钟前到达出口",
    Reason.MISSING_NODE: "起点节点不存在（不是任何通道的端点，也不是出口）",
    Reason.NO_EXIT: "当前场景未定义出口",
}

_write_lock = asyncio.Lock()
_conn: Optional[sqlite3.Connection] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _conn
    _conn = dbmod.connect(DB_PATH)
    dbmod.init_db(_conn)
    seed.seed_if_empty(_conn)
    try:
        yield
    finally:
        _conn.close()


app = FastAPI(
    title="场馆疏散通道编排服务",
    version="1.0.0",
    description=(
        "输入带编号的有向通道（通行分钟数、每分钟容量、封闭时段）、出口与人群组，"
        "按优先级逐组编排截止前最早到达的疏散路线。详见 README。"
    ),
    lifespan=lifespan,
)


def conn() -> sqlite3.Connection:
    assert _conn is not None
    return _conn


# ---------------------------------------------------------------------------
# 编排与状态组装
# ---------------------------------------------------------------------------

def _compute() -> Tuple[Any, Dict[int, Assignment], Dict[int, Reason]]:
    model = dbmod.load_model(conn())
    assignments, reasons = replan(model)
    return model, assignments, reasons


def _status_payload(model, assignments: Dict[int, Assignment],
                    reasons: Dict[int, Reason]) -> Dict[str, Any]:
    edge_map = model.edges

    corridors = [
        {
            "id": e.id, "src": e.src, "dst": e.dst,
            "minutes": e.minutes, "capacity": e.capacity,
        }
        for e in sorted(edge_map.values(), key=lambda x: x.id)
    ]
    closures = [
        {"id": c.id, "edge_id": c.edge_id, "start": c.start, "end": c.end}
        for c in sorted(model.closures, key=lambda x: (x.id if x.id is not None else 0))
    ]

    groups_out: List[Dict[str, Any]] = []
    for gid in ordering(model, assignments):
        g = model.groups[gid]
        item: Dict[str, Any] = {
            "id": g.id, "people": g.people, "source": g.source,
            "earliest": g.earliest, "priority": g.priority,
            "deadline": g.deadline,
        }
        a = assignments.get(gid)
        if a is not None:
            hops = [
                {
                    "edge_id": edge_id,
                    "entry": entry,
                    "arrival": entry + edge_map[edge_id].minutes,
                }
                for edge_id, entry in a.hops
            ]
            item.update(
                status="departed" if a.depart < model.now else "scheduled",
                depart=a.depart, arrival=a.arrival,
                exit_node=a.exit_node, route=list(a.edge_ids), hops=hops,
                reason=None, reason_detail=None,
            )
        else:
            reason = reasons[gid]
            # now 已越过截止分钟：错过机会，不再可能安排（重排仍同样失败）
            missed = model.now > g.deadline
            item.update(
                status="missed" if missed else "unscheduled",
                depart=None, arrival=None,
                exit_node=None, route=[], hops=[],
                reason=reason.value,
                reason_detail=("已错过规划截止分钟（now > deadline）"
                               if missed else REASON_DETAIL[reason]),
            )
        groups_out.append(item)

    return {
        "now": model.now,
        "corridors": corridors,
        "exits": sorted(model.exits),
        "closures": closures,
        "order": ordering(model, assignments),
        "groups": groups_out,
    }


def _current_status() -> Dict[str, Any]:
    """只读：在内存中重算（确定性算法，结果与落库一致），不写库。"""
    model, assignments, reasons = _compute()
    return _status_payload(model, assignments, reasons)


async def _mutate(action) -> Dict[str, Any]:
    """在锁 + 单事务中执行 action(db) -> (message)，失败整体回滚。"""
    async with _write_lock:
        db = conn()
        try:
            db.execute("BEGIN IMMEDIATE")
            message = action(db)
            model, assignments, reasons = _compute()
            dbmod.save_plan(db, model.edges, assignments)
            db.commit()
        except HTTPException:
            db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - 任何意外都不得留下半成品
            db.rollback()
            raise HTTPException(status_code=400, detail=f"变更被拒绝，已回滚: {exc}")
        payload = _status_payload(model, assignments, reasons)
        payload["message"] = message
        return payload


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------

@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/api/status", response_model=StatusOut, tags=["query"],
         summary="查询全部组的路线、出发/到达分钟或无法安排原因")
def get_status():
    return _current_status()


@app.put("/api/scenario", response_model=MessageOut, tags=["mutation"],
         summary="整场景替换（通道/出口/封闭/组），当前分钟归零")
async def replace_scenario(scenario: ScenarioIn):
    def action(db: sqlite3.Connection) -> str:
        ids = [c.id for c in scenario.corridors]
        if len(set(ids)) != len(ids):
            raise HTTPException(422, "通道编号重复")
        gids = [g.id for g in scenario.groups]
        if len(set(gids)) != len(gids):
            raise HTTPException(422, "人群组编号重复")
        edge_ids = set(ids)
        for c in scenario.closures:
            if c.edge_id not in edge_ids:
                raise HTTPException(422, f"封闭引用了不存在的通道 {c.edge_id}")

        for table in ("assignment_hops", "assignments", "closures",
                      "groups", "exits", "corridors"):
            db.execute(f"DELETE FROM {table}")
        dbmod.set_now(db, 0)

        for c in scenario.corridors:
            db.execute(
                "INSERT INTO corridors(id, src, dst, minutes, capacity) VALUES (?,?,?,?,?)",
                (c.id, c.src, c.dst, c.minutes, c.capacity),
            )
        for node in dict.fromkeys(scenario.exits):  # 去重保序
            db.execute("INSERT INTO exits(node) VALUES (?)", (node,))
        for c in scenario.closures:
            db.execute(
                "INSERT INTO closures(edge_id, start, \"end\") VALUES (?,?,?)",
                (c.edge_id, c.start, c.end),
            )
        for g in scenario.groups:
            db.execute(
                "INSERT INTO groups(id, people, source, earliest, priority, deadline)"
                " VALUES (?,?,?,?,?,?)",
                (g.id, g.people, g.source, g.earliest, g.priority, g.deadline),
            )
        return "场景已整体替换并完成编排"

    return await _mutate(action)


@app.post("/api/corridors", response_model=MessageOut, tags=["mutation"],
          status_code=201, summary="新增一条通道")
async def add_corridor(c: CorridorIn):
    def action(db: sqlite3.Connection) -> str:
        if db.execute("SELECT 1 FROM corridors WHERE id=?", (c.id,)).fetchone():
            raise HTTPException(409, f"通道 {c.id} 已存在")
        db.execute(
            "INSERT INTO corridors(id, src, dst, minutes, capacity) VALUES (?,?,?,?,?)",
            (c.id, c.src, c.dst, c.minutes, c.capacity),
        )
        return f"通道 {c.id} 已新增，并对未出发组完成重排"

    return await _mutate(action)


@app.post("/api/groups", response_model=MessageOut, tags=["mutation"],
          status_code=201, summary="新增一个人群组（仅重排未出发组）")
async def add_group(g: GroupIn):
    def action(db: sqlite3.Connection) -> str:
        if db.execute("SELECT 1 FROM groups WHERE id=?", (g.id,)).fetchone():
            raise HTTPException(409, f"人群组 {g.id} 已存在")
        db.execute(
            "INSERT INTO groups(id, people, source, earliest, priority, deadline)"
            " VALUES (?,?,?,?,?,?)",
            (g.id, g.people, g.source, g.earliest, g.priority, g.deadline),
        )
        return f"人群组 {g.id} 已加入，并对未出发组完成重排"

    return await _mutate(action)


@app.post("/api/closures", response_model=MessageOut, tags=["mutation"],
          status_code=201, summary="新增一个左闭右开封闭时段")
async def add_closure(c: ClosureIn):
    def action(db: sqlite3.Connection) -> str:
        if not db.execute("SELECT 1 FROM corridors WHERE id=?", (c.edge_id,)).fetchone():
            raise HTTPException(404, f"通道 {c.edge_id} 不存在")

        # 触及已出发组剩余路线 => 整次拒绝（在插入前判定）
        model = dbmod.load_model(db)
        probe = [Closure(id=None, edge_id=c.edge_id, start=c.start, end=c.end)]
        conflicts = closure_conflicts_fixed(model, probe)
        if conflicts:
            raise HTTPException(
                409,
                f"封闭被拒绝：触及已出发组的剩余路线（冲突组: {conflicts}）",
            )

        db.execute(
            "INSERT INTO closures(edge_id, start, \"end\") VALUES (?,?,?)",
            (c.edge_id, c.start, c.end),
        )
        return (
            f"通道 {c.edge_id} 的封闭 [{c.start}, {c.end}) 已生效，"
            "已出发组不变，未出发组已重排"
        )

    return await _mutate(action)


@app.post("/api/advance", response_model=MessageOut, tags=["mutation"],
          summary="推进当前分钟（已出发组的路线与占用随之固定）")
async def advance(body: AdvanceIn):
    def action(db: sqlite3.Connection) -> str:
        current = dbmod.get_now(db)
        if body.to < current:
            raise HTTPException(
                422, f"当前分钟只能向前推进：now={current}，请求 to={body.to}"
            )
        dbmod.set_now(db, body.to)
        if body.to == current:
            return f"当前分钟仍为 {current}，无变化"
        return (
            f"当前分钟已推进到 {body.to}；depart < {body.to} 的组视为已出发，"
            "路线与占用固定，其余组重排"
        )

    return await _mutate(action)
