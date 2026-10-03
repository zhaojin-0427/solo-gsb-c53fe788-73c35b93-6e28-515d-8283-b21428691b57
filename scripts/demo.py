"""生成 README 中接口文档所用的真实请求/响应样例。

用法：先启动服务（docker compose up 或 uvicorn），再运行
    BASE_URL=http://localhost:8000 python3 scripts/demo.py
脚本按固定顺序调用接口，实际响应会有细微差异（id、时间戳格式保持稳定）。
"""
import json
import os

import httpx

BASE = os.environ.get("BASE_URL", "http://localhost:8000")
c = httpx.Client(base_url=BASE, timeout=10)


def show(title, method, path, body=None, params=None):
    print(f"\n### {title}")
    print(f"请求：{method} {path}")
    if body is not None:
        print("请求体：")
        print(json.dumps(body, ensure_ascii=False, indent=2))
    r = c.request(method, path, json=body, params=params)
    print(f"响应：HTTP {r.status_code}")
    try:
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))
    except Exception:
        print(r.text)
    r.raise_for_status() if r.status_code < 400 else None
    return r.json()


# 1. 健康检查与初始修订
show("健康检查", "GET", "/health")
rev = show("查询全局修订号", "GET", "/api/revision")["revision"]

# 2. 登记根证书（标准器具 STD-001）
root = show("登记根证书", "POST", "/api/certificates", {
    "expected_revision": rev,
    "instrument_id": "STD-001",
    "serial": "STD-SN-0001",
    "calibrated_at": "2026-01-01T00:00:00Z",
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to": "2027-01-01T00:00:00Z",
    "error_value": -0.05,
    "tolerance": 0.5,
    "is_root": True,
})
root_id = root["certificate"]["id"]
rev = root["revision"]

# 3. 登记下级证书（工作器具 DMM-7788）
child = show("登记下级证书", "POST", "/api/certificates", {
    "expected_revision": rev,
    "instrument_id": "DMM-7788",
    "serial": "DMM-SN-7788",
    "calibrated_at": "2026-02-01T00:00:00Z",
    "valid_from": "2026-02-01T00:00:00Z",
    "valid_to": "2026-08-01T00:00:00Z",
    "error_value": 0.1,
    "tolerance": 0.2,
    "parent_id": root_id,
})
child_id = child["certificate"]["id"]
rev = child["revision"]

# 4. 登记测量（自动绑定覆盖时刻的证书）
m1 = show("登记测量（撤销前，仍有效）", "POST", "/api/measurements", {
    "expected_revision": rev,
    "instrument_id": "DMM-7788",
    "value": 5.012,
    "measured_at": "2026-03-10T08:00:00Z",
})
m1_id = m1["measurement"]["id"]
rev = m1["revision"]

m2 = show("登记测量（撤销后，将被重判）", "POST", "/api/measurements", {
    "expected_revision": rev,
    "instrument_id": "DMM-7788",
    "value": 5.018,
    "measured_at": "2026-07-15T08:00:00Z",
})
m2_id = m2["measurement"]["id"]
rev = m2["revision"]

# 5. 过期 expected_revision -> 409
show("并发冲突（过期修订返回 409）", "POST", "/api/certificates", {
    "expected_revision": 0,
    "instrument_id": "X",
    "calibrated_at": "2026-01-01T00:00:00Z",
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to": "2026-02-01T00:00:00Z",
    "error_value": 0.0,
    "tolerance": 0.1,
    "is_root": True,
})

# 6. 撤销下级证书（自 2026-05-01 起失效）
revocation = show("撤销证书并级联重判", "POST",
                  f"/api/certificates/{child_id}/revoke", {
                      "expected_revision": rev,
                      "revoked_at": "2026-05-01T00:00:00Z",
                  })
rev = revocation["revision"]

# 7. 按历史修订查询（撤销前 m2 仍有效）
show(f"历史修订查询：测量 {m2_id} 在撤销前的结论", "GET",
     f"/api/measurements/{m2_id}", params={"revision": rev - 1})

# 8. 失效后登记被拒
show("失效后登记测量被拒（422）", "POST", "/api/measurements", {
    "expected_revision": rev,
    "instrument_id": "DMM-7788",
    "value": 9.9,
    "measured_at": "2026-05-15T00:00:00Z",
})

# 9. 列出全部测量当前结论
show("测量结论列表", "GET", "/api/measurements")
