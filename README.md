# 场馆疏散通道编排服务

基于 **Python 3.11+ / FastAPI / SQLite** 的疏散编排服务：输入带编号的有向通道
（通行分钟数、每分钟容量、左闭右开封闭时段）、出口位置与人群组
（人数、起点、最早出发分钟、优先级、规划截止分钟），按优先级逐组编排
**截止前最早到达**的路线与出发时间，并在推进时间、新增组、新增封闭时
只重排未出发组；触及已出发组剩余路线的封闭被整次拒绝。

---

## 1. 一键启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

首次启动会自动：

1. 构建镜像并安装 `requirements.txt` 中的全部依赖；
2. 在容器内初始化 SQLite 数据库（持久化卷 `evac_data`）；
3. 写入一份演示场景（`app/seed.py`）并完成初始编排。

服务地址：**http://localhost:8000**

- 健康检查：`GET http://localhost:8000/health`
- 交互式 API 文档（Swagger UI）：**http://localhost:8000/docs**
- OpenAPI JSON：`http://localhost:8000/openapi.json`

自定义宿主端口：

```bash
EVAC_PORT=18080 docker compose up --build     # => http://localhost:18080
```

### 不用 Docker（本地 Python）

```bash
pip install -r requirements.txt
export EVAC_DB_PATH=./data/evacuation.db      # 可选，默认 /data/evacuation.db
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

重置为初始演示数据：删除数据库文件后重启（容器场景执行
`docker compose down -v` 清卷）。

### 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EVAC_DB_PATH` | `/data/evacuation.db` | SQLite 数据库文件路径；目录不存在会自动创建 |
| `EVAC_PORT`（仅 compose） | `8000` | 宿主机映射端口，容器内固定监听 8000 |

---

## 2. 时间与约束语义

- 所有时刻均为**整数分钟**，当前分钟 `now` 从 **0** 起。
- 通道边 `u -> v`，通行分钟数 `t > 0`、每分钟容量 `cap > 0`。
- 某组在分钟 `e` 进入通道，则在分钟
  `e, e+1, …, e+t-1` 占用该通道（"通行期间每分钟"），分钟 `e+t` 到达下一节点。
- **容量约束**：同一条通道、同一个分钟内，所有占用它的组的人数之和
  `≤ capacity`。组不可拆分，因此若加上本组会超容，则该分钟（及与之连续的
  不可行分钟）不能进入。
- **封闭时段**为左闭右开区间 `[start, end)`：组占用通道的任何一分钟都不得
  落入封闭区间；一条长度 `t` 的通道必须存在连续 `t` 分钟的可行窗口才能进入。
- **编排顺序**：优先级数值从大到小；同级按最早出发分钟从小到大；
  再相同按组编号从小到大。逐组贪心，先排的组先占容量。
- **每组择优**（字典序，前者优先）：
  1. 到达出口的分钟最小（须 `arrival ≤ deadline`，且出发不得早于
     `max(now, earliest)`）；
  2. 到达相同 → 出口节点编号最小；
  3. 仍相同 → 通道编号序列字典序最小；
  4. 仍相同 → 实际出发分钟最小。
  起点本身就是出口时为零通道路线，出发/到达均为 `max(now, earliest)`。
- **推进时间**：`POST /api/advance {"to": k}` 将当前分钟推进到 `k`（只能向前）。
  此后所有 `depart < k` 的组视为**已出发**，其路线、出发时间与逐分钟占用
  完全固定，之后的重排不再改动它们。
- **新增组 / 新增封闭**：只重排未出发组；新增封闭若与任何已出发组在
  `now` 之后仍占用的"剩余路线"相交，则**整次拒绝**（HTTP 409，封闭不写入、
  编排不变）。与剩余路线恰好端点相接（如剩余占用到 8，封闭 `[8,10)`）允许。
- 无法在截止前安排时返回原因代码：
  `no_route`（图上根本无路）、`no_capacity`（有路但容量/封闭使截止前不可达）、
  `missing_node`（起点不在任何通道端点上）、`no_exit`（场景无出口）。

> 算法：对单组做时间依赖 FIFO 网络上的多标签最短路（按到达分钟定标，
> 每个中间节点保留 `(到达, 通道序列)` Pareto 前沿以正确处理"封闭等待反超"），
> 再按编排顺序逐组把占用计入逐边逐分钟负载。详见 `app/scheduler.py`。

---

## 3. HTTP API

基址 `http://localhost:8000`，除健康检查外均以 JSON 交互。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/api/status` | 查询当前 `now`、通道/出口/封闭，以及每组状态、路线、逐跳进入/到达分钟或失败原因 |
| PUT | `/api/scenario` | **整场景替换**（通道、出口、封闭、组），`now` 归零并整体重排 |
| POST | `/api/corridors` | 新增一条通道，随后重排未出发组 |
| POST | `/api/groups` | 新增一个人群组，随后只重排未出发组 |
| POST | `/api/closures` | 新增一个左闭右开封闭；触及已出发组剩余路线则 409 整次拒绝 |
| POST | `/api/advance` | 推进当前分钟 `{"to": n}`（不可倒退），冻结已出发组并重排其余 |

### 请求体字段

通道：

```json
{ "id": 1, "src": 1, "dst": 3, "minutes": 3, "capacity": 10 }
```

封闭（`end > start`，区间 `[start, end)`）：

```json
{ "edge_id": 3, "start": 2, "end": 6 }
```

人群组（`deadline ≥ earliest`，人数为正整数）：

```json
{ "id": 1, "people": 6, "source": 1, "earliest": 0, "priority": 10, "deadline": 30 }
```

整场景：

```json
{
  "corridors": [ { "id": 1, "src": 1, "dst": 3, "minutes": 3, "capacity": 10 } ],
  "exits": [4],
  "closures": [ { "edge_id": 1, "start": 0, "end": 4 } ],
  "groups": [ { "id": 1, "people": 6, "source": 1, "earliest": 0,
                "priority": 10, "deadline": 30 } ]
}
```

### 查询结果（每组）

- `status`：`departed`（已出发固定）/ `scheduled`（已安排未出发）/
  `unscheduled`（暂未安排，截止分钟尚未越过，后续变更或仍可能排上）/
  `missed`（当前分钟已越过截止分钟，确定错过；附 `reason` 与 `reason_detail`）
- `depart` / `arrival`：出发与到达分钟
- `exit_node`：到达的出口编号
- `route`：通道编号序列（如 `[1, 4, 5]`）
- `hops`：逐跳明细 `[{"edge_id": 1, "entry": 0, "arrival": 3}, …]`
- 顶层 `order`：本轮编排考虑的组顺序（已出发组在前，其后按优先级规则）

---

## 4. 调用示例

仓库内置仅依赖标准库的示例脚本，覆盖查询、加封闭、推进时间、
**409 冲突拒绝**与新增组重排：

```bash
# 服务已在 localhost:8000 运行时
python3 examples/demo_client.py
# 或指定地址
python3 examples/demo_client.py http://127.0.0.1:18080
```

### curl 速览

```bash
# 查询编排结果
curl -s http://localhost:8000/api/status | python3 -m json.tool

# 新增封闭：通道 3 在分钟 [10, 14) 封闭
curl -s -X POST http://localhost:8000/api/closures \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 3, "start": 10, "end": 14}'

# 推进当前分钟到 6（depart < 6 的组随即冻结）
curl -s -X POST http://localhost:8000/api/advance \
  -H 'Content-Type: application/json' -d '{"to": 6}'

# 新增一个人群组（只重排未出发组）
curl -s -X POST http://localhost:8000/api/groups \
  -H 'Content-Type: application/json' \
  -d '{"id": 9, "people": 7, "source": 1, "earliest": 6,
       "priority": 20, "deadline": 25}'

# 整体替换场景
curl -s -X PUT http://localhost:8000/api/scenario \
  -H 'Content-Type: application/json' \
  -d '{"corridors":[{"id":1,"src":1,"dst":2,"minutes":2,"capacity":3}],
       "exits":[2],"closures":[],"groups":[]}'
```

### 初始演示场景（自动写入）

节点：`1` 北门、`2` 东门、`3` 中庭、`4` 南广场（唯一出口）、`5` 西侧连廊。

| 通道 | 走向 | 分钟 | 容量 |
| --- | --- | --- | --- |
| 1 | 1 → 3 | 3 | 10 |
| 2 | 2 → 3 | 2 | 10 |
| 3 | 3 → 4 | 4 | 8 |
| 4 | 3 → 5 | 5 | 20 |
| 5 | 5 → 4 | 1 | 20 |
| 6 | 1 → 2 | 2 | 10 |

封闭：通道 3 的 `[2, 6)`。组：组 1（6 人，起点 1，优先级 10）、
组 2（4 人，起点 1，优先级 5）、组 3（5 人，起点 2，最早 2 出发，优先级 8），
截止均为 30/30/20。

初始编排（`order = [1, 3, 2]`）：

- 组 1：出发 0，`1→3→5→4`（通道 `[1,4,5]`），到达 **9**
  （直走通道 3 受封闭影响要到 10，绕行 9 更早）；
- 组 3：出发 2，`2→3→4`（通道 `[2,3]`，在中庭等到分钟 6 进入通道 3），
  到达 **10**；与绕行同样到 10 时，通道序列 `[2,3] < [4,5]`；
- 组 2：与组 1 同路线，6+4=10 不超容量，出发 0，到达 **9**。

---

## 5. 并发与一致性

- 全部写接口由**进程内 asyncio 锁**串行化；每个写请求在单个 SQLite
  事务（`BEGIN IMMEDIATE`）中完成"改数据 → 整轮重排 → 结果落库 → 提交"，
  任何一步失败立即回滚。因此并发的变更既不会超用容量，也不会留下
  只重排了一部分的中间状态。
- `GET /api/status` 为只读：用当前数据在内存中确定性重算后返回，
  与已落库的编排一致。
- 本服务按**单实例**部署设计（uvicorn 单 worker，SQLite 位于卷内）。

---

## 6. 测试

```bash
# 核心算法单测（无外部依赖，可直接运行）
python3 tests/test_scheduler.py

# HTTP 端到端测试（需先启动服务）
EVAC_BASE=http://127.0.0.1:8000 python3 tests/test_http.py
```

覆盖：逐分钟容量共用/错峰、封闭绕行与截止失败、优先级与同级编号顺序、
同到达时出口/通道序列/出发择优、封闭等待反超（Pareto 多标签）、
推进冻结、触及剩余路线的冲突拒绝与端点相接放行、起点即出口、
并发加组不超卖、参数校验与拒绝后无部分重排。

## 项目结构

```
app/
  main.py        FastAPI 路由、事务/锁、状态组装
  scheduler.py   编排核心：时间依赖多标签最短路 + 逐组贪心 + 冻结/冲突判定
  db.py          SQLite 连接、建表、场景装载、结果落库
  schemas.py     Pydantic 请求/响应模型与校验
  seed.py        首次启动演示数据与初始编排
examples/
  demo_client.py 标准库调用示例
tests/
  test_scheduler.py  算法单测
  test_http.py       HTTP 端到端测试
Dockerfile / docker-compose.yml / requirements.txt
```
