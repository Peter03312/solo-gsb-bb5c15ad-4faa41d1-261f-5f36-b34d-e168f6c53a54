#!/usr/bin/env python3
"""调用示例（仅用标准库）：演示查询、加封闭、推进时间与冲突拒绝。

用法：
    python examples/demo_client.py [BASE_URL]
默认 BASE_URL=http://127.0.0.1:8000
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, body=None, expect: int = 200):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    print(f"\n>>> {method} {path}" + (f"  body={json.dumps(body, ensure_ascii=False)}" if body else ""))
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode())
            print(f"<<< HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        print(f"<<< HTTP {e.code}（预期的拒绝）")
        try:
            print(json.dumps(json.loads(raw), ensure_ascii=False, indent=2))
            return json.loads(raw)
        except json.JSONDecodeError:
            print(raw)
            return {"error": raw}
    if expect != 200 or path.endswith("/status"):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("message:", payload.get("message"))
        for g in payload.get("groups", []):
            if g["status"] == "unscheduled":
                print(f"  组{g['id']}: 无法安排（{g['reason']}）")
            else:
                print(
                    f"  组{g['id']}: [{g['status']}] 出发{g['depart']} "
                    f"到达{g['arrival']} 出口{g['exit_node']} 路线{g['route']}"
                )
    return payload


def main():
    call("GET", "/health")
    call("GET", "/api/status")

    # 新增封闭：通道 5 在 [0,4) 封闭（初始状态无人出发，允许，触发重排）
    call("POST", "/api/closures", {"edge_id": 5, "start": 0, "end": 4})

    # 推进当前分钟到 4：depart < 4 的组视为已出发，路线与占用固定
    call("POST", "/api/advance", {"to": 4})

    # 组1、组2 此刻正在通道 4 上（占用 [3,8)，剩余 [4,8)）；
    # 对通道 4 加封闭触及已出发组剩余路线 => 整次拒绝（HTTP 409）
    call("POST", "/api/closures", {"edge_id": 4, "start": 4, "end": 8})

    # 通道 4 在 [8,10) 的封闭不碰剩余占用（左闭右开，占用恰止于 8）=> 允许
    call("POST", "/api/closures", {"edge_id": 4, "start": 8, "end": 10})

    # 新增一个高优先级人群组：只重排未出发组
    call("POST", "/api/groups", {
        "id": 9, "people": 7, "source": 1,
        "earliest": 4, "priority": 20, "deadline": 25,
    })

    call("GET", "/api/status")


if __name__ == "__main__":
    main()
