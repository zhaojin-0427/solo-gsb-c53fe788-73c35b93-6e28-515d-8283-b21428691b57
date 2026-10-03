"""器具跃变（drift）查询样例：构造跨两张证书的测量序列并撤销证书，
展示相邻比较、证书切换重置、无效记录不拼接与历史修订重算。

用法：先在“重置后的空库”上启动服务（docker compose down -v && docker compose up，
或本地 uvicorn），再运行
    BASE_URL=http://localhost:8000 python3 scripts/demo_drift.py
"""
import json
import os

import httpx

BASE = os.environ.get("BASE_URL", "http://localhost:8000")
c = httpx.Client(base_url=BASE, timeout=10)


def call(title, method, path, body=None, params=None, allow_fail=False):
    print(f"\n### {title}")
    print(f"请求：{method} {path}")
    if params is not None:
        print(f"查询参数：{params}")
    if body is not None:
        print("请求体：")
        print(json.dumps(body, ensure_ascii=False, indent=2))
    r = c.request(method, path, json=body, params=params)
    print(f"响应：HTTP {r.status_code}")
    print(json.dumps(r.json(), ensure_ascii=False, indent=2))
    if not allow_fail:
        r.raise_for_status()
    return r.json()


rev = c.get("/api/revision").json()["revision"]

# 1) 标准器具根证书
r = call("登记根证书（STD-DRIFT）", "POST", "/api/certificates", {
    "expected_revision": rev,
    "instrument_id": "STD-DRIFT",
    "serial": "STD-SN-DRIFT",
    "calibrated_at": "2026-01-01T00:00:00Z",
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to": "2027-01-01T00:00:00Z",
    "error_value": 0.0,
    "tolerance": 0.5,
    "is_root": True,
})
root_id, rev = r["certificate"]["id"], r["revision"]

# 2) DMM-D 两张半开相接、互不重叠的证书
r = call("登记第一张器具证书 C1（[2026-01-01, 2026-04-01)）",
         "POST", "/api/certificates", {
             "expected_revision": rev,
             "instrument_id": "DMM-D",
             "serial": "DMM-D-C1",
             "calibrated_at": "2026-01-05T00:00:00Z",
             "valid_from": "2026-01-01T00:00:00Z",
             "valid_to": "2026-04-01T00:00:00Z",
             "error_value": 0.0,
             "tolerance": 0.2,
             "parent_id": root_id,
         })
c1, rev = r["certificate"]["id"], r["revision"]

r = call("登记第二张器具证书 C2（[2026-04-01, 2026-10-01)，与 C1 端点相接不重叠）",
         "POST", "/api/certificates", {
             "expected_revision": rev,
             "instrument_id": "DMM-D",
             "serial": "DMM-D-C2",
             "calibrated_at": "2026-04-02T00:00:00Z",
             "valid_from": "2026-04-01T00:00:00Z",
             "valid_to": "2026-10-01T00:00:00Z",
             "error_value": 0.0,
             "tolerance": 0.2,
             "parent_id": root_id,
         })
c2, rev = r["certificate"]["id"], r["revision"]

# 3) 五条测量：C1 两条（差 0.25），C2 三条（各差 0.75）
plan = [
    (c1, "2026-02-01T08:00:00Z", 1.0),
    (c1, "2026-02-10T08:00:00Z", 1.25),
    (c2, "2026-04-10T08:00:00Z", 1.0),
    (c2, "2026-05-10T08:00:00Z", 1.75),
    (c2, "2026-05-20T08:00:00Z", 2.5),
]
ids = []
for cert_id, at, value in plan:
    r = call(f"登记测量（certificate_id={cert_id}, {at}, value={value}）",
             "POST", "/api/measurements", {
                 "expected_revision": rev,
                 "instrument_id": "DMM-D",
                 "value": value,
                 "measured_at": at,
                 "certificate_id": cert_id,
             })
    ids.append(r["measurement"]["id"])
    rev = r["revision"]
rev_before_revoke = rev  # 8

# 4) 当前修订下的跃变查询
window = {"start": "2026-01-01T00:00:00Z", "end": "2027-01-01T00:00:00Z"}
call("跃变查询（阈值 0.5，当前修订）", "GET", "/api/instruments/DMM-D/drift",
     params={**window, "max_delta": "0.5"})

# 5) 阈值边界：0.75 恰好等于差值时不告警
call("阈值取 0.75：绝对差须严格大于阈值，恰好相等不告警",
     "GET", "/api/instruments/DMM-D/drift",
     params={**window, "max_delta": "0.75"})

# 6) 撤销 C1 于 02-05（d1 仍有效、d2 失效），撤销 C2 于 05-15（d4 仍有效、d5 失效）
r = call("撤销 C1 于 2026-02-05（d2 落入失效区间）", "POST",
         f"/api/certificates/{c1}/revoke", {
             "expected_revision": rev,
             "revoked_at": "2026-02-05T00:00:00Z",
         })
rev = r["revision"]
r = call("撤销 C2 于 2026-05-15（d5 落入失效区间，d4 在撤销前仍可信）", "POST",
         f"/api/certificates/{c2}/revoke", {
             "expected_revision": rev,
             "revoked_at": "2026-05-15T00:00:00Z",
         })
rev = r["revision"]

# 7) 撤销后按当前修订重算
call("撤销后按当前修订重算：无效记录与证书切换均重置基线，不跨无效拼接",
     "GET", "/api/instruments/DMM-D/drift",
     params={**window, "max_delta": "0.5"})

# 8) 按撤销前的历史修订重算：早期告警保留
call(f"按历史修订 {rev_before_revoke} 重算：撤销前的两条告警原样保留",
     "GET", "/api/instruments/DMM-D/drift",
     params={**window, "max_delta": "0.5", "revision": str(rev_before_revoke)})

# 9) 错误样例
call("start 不早于 end -> 422", "GET", "/api/instruments/DMM-D/drift",
     params={"start": "2026-06-01T00:00:00Z",
             "end": "2026-06-01T00:00:00Z", "max_delta": "0.5"},
     allow_fail=True)
call("阈值为负 -> 422", "GET", "/api/instruments/DMM-D/drift",
     params={**window, "max_delta": "-0.1"}, allow_fail=True)
call("未来修订 -> 404", "GET", "/api/instruments/DMM-D/drift",
     params={**window, "max_delta": "0.5", "revision": "9999"},
     allow_fail=True)
