"""递归失效场景样例：撤销根证书 -> 子证书整证判废 -> 孙证书递归判废。"""
import json
import os

import httpx

BASE = os.environ.get("BASE_URL", "http://localhost:8000")
c = httpx.Client(base_url=BASE, timeout=10)


def call(title, method, path, body=None, params=None):
    print(f"\n### {title}")
    print(f"请求：{method} {path}")
    if body is not None:
        print("请求体：")
        print(json.dumps(body, ensure_ascii=False, indent=2))
    r = c.request(method, path, json=body, params=params)
    print(f"响应：HTTP {r.status_code}")
    print(json.dumps(r.json(), ensure_ascii=False, indent=2))
    r.raise_for_status()
    return r.json()


rev = c.get("/api/revision").json()["revision"]

r = call("登记根证书（STD-R）", "POST", "/api/certificates", {
    "expected_revision": rev,
    "instrument_id": "STD-R",
    "calibrated_at": "2026-01-01T00:00:00Z",
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to": "2027-01-01T00:00:00Z",
    "error_value": 0.0, "tolerance": 0.5, "is_root": True,
})
root_id, rev = r["certificate"]["id"], r["revision"]

r = call("登记子证书（DMM-R），校准时刻 2026-02-01", "POST", "/api/certificates", {
    "expected_revision": rev,
    "instrument_id": "DMM-R",
    "calibrated_at": "2026-02-01T00:00:00Z",
    "valid_from": "2026-02-01T00:00:00Z",
    "valid_to": "2026-12-01T00:00:00Z",
    "error_value": 0.0, "tolerance": 0.2, "parent_id": root_id,
})
child_id, rev = r["certificate"]["id"], r["revision"]

r = call("登记孙证书（DUT-R），校准时刻 2026-03-01", "POST", "/api/certificates", {
    "expected_revision": rev,
    "instrument_id": "DUT-R",
    "calibrated_at": "2026-03-01T00:00:00Z",
    "valid_from": "2026-03-01T00:00:00Z",
    "valid_to": "2026-11-01T00:00:00Z",
    "error_value": 0.0, "tolerance": 0.1, "parent_id": child_id,
})
grand_id, rev = r["certificate"]["id"], r["revision"]

r = call("在孙证书覆盖下登记测量", "POST", "/api/measurements", {
    "expected_revision": rev,
    "instrument_id": "DUT-R",
    "value": 12.34,
    "measured_at": "2026-04-01T00:00:00Z",
})
meas_id, rev = r["measurement"]["id"], r["revision"]

call(f"自 2026-01-15 起撤销根证书：子/孙证书校准时刻均落入失效区间，递归判废",
     "POST", f"/api/certificates/{root_id}/revoke", {
         "expected_revision": rev,
         "revoked_at": "2026-01-15T00:00:00Z",
     })

call(f"按历史修订 {rev} 查询测量 {meas_id}（撤销前结论保留）", "GET",
     f"/api/measurements/{meas_id}", params={"revision": rev})
