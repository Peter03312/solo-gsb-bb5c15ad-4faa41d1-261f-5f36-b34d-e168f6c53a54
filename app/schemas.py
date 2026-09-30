"""HTTP 请求/响应模式（Pydantic v2）。"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


class CorridorIn(BaseModel):
    id: int = Field(..., ge=0, description="通道编号（唯一非负整数）")
    src: int = Field(..., description="起点节点编号")
    dst: int = Field(..., description="终点节点编号")
    minutes: int = Field(..., gt=0, description="通行分钟数（正整数）")
    capacity: int = Field(..., gt=0, description="每分钟容量（正整数，人/分钟）")


class ClosureIn(BaseModel):
    edge_id: int = Field(..., description="被封闭的通道编号")
    start: int = Field(..., ge=0, description="封闭开始分钟（含）")
    end: int = Field(..., gt=0, description="封闭结束分钟（不含，须 > start）")

    @model_validator(mode="after")
    def _check_window(self):
        if self.end <= self.start:
            raise ValueError("封闭时段须满足 end > start（左闭右开）")
        return self


class GroupIn(BaseModel):
    id: int = Field(..., ge=0, description="人群组编号（唯一非负整数）")
    people: int = Field(..., gt=0, description="人数（正整数，组不可拆分）")
    source: int = Field(..., description="起点节点编号")
    earliest: int = Field(..., ge=0, description="最早出发分钟")
    priority: int = Field(..., ge=0, description="优先级，数值越大越优先")
    deadline: int = Field(..., ge=0, description="规划截止分钟（到达须 <= 该值）")

    @model_validator(mode="after")
    def _check_deadline(self):
        if self.deadline < self.earliest:
            raise ValueError("规划截止分钟不得早于最早出发分钟")
        return self


class ScenarioIn(BaseModel):
    """整场景替换（PUT /api/scenario）。"""

    corridors: List[CorridorIn] = Field(default_factory=list)
    exits: List[int] = Field(default_factory=list, description="出口节点编号列表")
    closures: List[ClosureIn] = Field(default_factory=list)
    groups: List[GroupIn] = Field(default_factory=list)


class AdvanceIn(BaseModel):
    to: int = Field(..., ge=0, description="推进到的当前分钟（须 >= 当前分钟）")


class HopOut(BaseModel):
    edge_id: int
    entry: int = Field(..., description="进入该通道的分钟")
    arrival: int = Field(..., description="到达该通道终点的分钟")


class GroupOut(BaseModel):
    id: int
    people: int
    source: int
    earliest: int
    priority: int
    deadline: int
    status: str = Field(
        ...,
        description="departed(已出发) / scheduled(已安排) / "
                    "unscheduled(暂未安排，仍有机会) / missed(已错过截止)",
    )
    depart: Optional[int] = None
    arrival: Optional[int] = None
    exit_node: Optional[int] = None
    route: List[int] = Field(default_factory=list, description="通道编号序列")
    hops: List[HopOut] = Field(default_factory=list)
    reason: Optional[str] = Field(None, description="unscheduled 时的原因代码")
    reason_detail: Optional[str] = None


class ClosureOut(BaseModel):
    id: int
    edge_id: int
    start: int
    end: int


class StatusOut(BaseModel):
    now: int
    corridors: List[CorridorIn]
    exits: List[int]
    closures: List[ClosureOut]
    order: List[int] = Field(..., description="本轮编排考虑顺序（已出发在前）")
    groups: List[GroupOut]


class MessageOut(StatusOut):
    """写操作返回：完整状态 + message（扁平结构）。"""

    message: str
