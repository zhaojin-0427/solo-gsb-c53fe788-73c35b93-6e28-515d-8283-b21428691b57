"""端到端业务规则验证（ASGI 直连，需本地 PostgreSQL）。

层级设计：根证书属于标准器具 STD，下级证书属于不同器具
（同一器具的证书有效期互不重叠；校准时刻落在上级证书有效期内）。
"""
import os

os.environ["DATABASE_URL"] = "postgresql://calib@localhost:5432/calib"

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app).__enter__()

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {extra}")


def reset_db():
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute("TRUNCATE measurement, certificate RESTART IDENTITY;")
        conn.execute("UPDATE revision SET value = 0 WHERE id = 1;")


def rev():
    return client.get("/api/revision").json()["revision"]


def expect_error(resp, code, error_name):
    return resp.status_code == code and resp.json()["detail"]["error"] == error_name


def mkcert(expected, inst, cal, vf, vt, err, tol, parent=None, root=False):
    return client.post("/api/certificates", json={
        "expected_revision": expected,
        "instrument_id": inst,
        "calibrated_at": cal, "valid_from": vf, "valid_to": vt,
        "error_value": err, "tolerance": tol,
        "parent_id": parent, "is_root": root,
    })


def post_meas(expected, at, cert_id=None, value=1.0, inst="I"):
    body = {"expected_revision": expected, "instrument_id": inst,
            "value": value, "measured_at": at}
    if cert_id is not None:
        body["certificate_id"] = cert_id
    return client.post("/api/measurements", json=body)


print("== 场景 1：撤销截止时刻与测量重判 ==")
reset_db()

# 根证书（标准器具 STD）
r = mkcert(0, "STD", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
           "2027-01-01T00:00:00Z", -0.05, 0.5, root=True)
check("根证书登记成功", r.status_code == 201, r.text)
root_id = r.json()["certificate"]["id"]
check("根证书状态 valid", r.json()["certificate"]["status"] == "valid")

# 非根证书 C（器具 A），校准于 2026-02-01（在 R 有效期内）
r = mkcert(1, "A", "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z",
           "2026-08-01T00:00:00Z", 0.1, 0.2, parent=root_id)
check("子证书登记成功", r.status_code == 201, r.text)
c_id = r.json()["certificate"]["id"]
check("返回的链为 C -> R",
      [x["certificate_id"] for x in r.json()["certificate_chain"]] == [c_id, root_id])

# 孙证书 G（器具 B），校准于 2026-03-01（在 C 有效期内）
r = mkcert(2, "B", "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z",
           "2026-06-01T00:00:00Z", 0.0, 0.1, parent=c_id)
check("孙证书登记成功", r.status_code == 201, r.text)
g_id = r.json()["certificate"]["id"]

r = post_meas(3, "2026-03-10T08:00:00Z", inst="B")  # 自动绑定 G
m1 = r.json()["measurement"]
check("m1 自动绑定 G", r.status_code == 201 and m1["certificate_id"] == g_id, r.text)
check("m1 valid", m1["valid"] is True)

r = post_meas(4, "2026-04-10T08:00:00Z", inst="B")
m2 = r.json()["measurement"]
check("m2 绑定 G 且有效", r.status_code == 201 and m2["valid"])

r = post_meas(5, "2026-07-15T08:00:00Z", inst="A")  # 绑定 C
m3 = r.json()["measurement"]
check("m3 自动绑定 C", r.status_code == 201 and m3["certificate_id"] == c_id, r.text)

r = post_meas(6, "2026-01-15T08:00:00Z", inst="STD")  # 绑定 R
m4 = r.json()["measurement"]
check("m4 自动绑定 R", r.status_code == 201 and m4["certificate_id"] == root_id, r.text)

rev_before_c_revoke = rev()  # 7

# 撤销 R 于 2026-02-15：C 校准 02-01 早于失效起点 -> C 不递归失效
r = client.post(f"/api/certificates/{root_id}/revoke", json={
    "expected_revision": 7,
    "revoked_at": "2026-02-15T00:00:00Z",
})
check("撤销 R 成功", r.status_code == 200, r.text)
data = r.json()
check("R 撤销后无测量失效（m4 在撤销前；下级未判废）",
      data["invalidated_measurement_count"] == 0, str(data["invalidated_measurement_count"]))
check("无证书递归判废", data["invalidated_certificates"] == [])

# 既有测量重判：m4 仍有效（撤销时刻之前），但证书状态 revoked
r = client.get(f"/api/measurements/{m4['id']}").json()
check("m4 撤销时刻前仍有效", r["measurement"]["valid"] is True
      and r["measurement"]["certificate_status"] == "revoked")

# 撤销 C 于 2026-05-01
r = client.post(f"/api/certificates/{c_id}/revoke", json={
    "expected_revision": 8,
    "revoked_at": "2026-05-01T00:00:00Z",
})
check("撤销 C 成功", r.status_code == 200, r.text)
data = r.json()
inv = data["invalidated_measurements"]
check("仅 m3(07-15) 本次失效，数量=1", len(inv) == 1, str(len(inv)))
check("失效的是 m3", bool(inv) and inv[0]["measurement"]["id"] == m3["id"])
fc = inv[0]["failure_chain"]
# R 在 C 校准时刻（02-01）之后才撤销（02-15），其失效区间未覆盖 C 的校准时刻，
# 与 m3 的失效无因果：失效链仅含 C，根因为 C 自身撤销，R 不得误列入链
check("m3 失效链仅含 C（R 晚撤销不计入）", [x["certificate_id"] for x in fc] == [c_id],
      str([x["certificate_id"] for x in fc]))
check("根因证书为 C（自身撤销）", inv[0]["root_cause_certificate_id"] == c_id)
check("失效链状态 C=revoked", [x["status"] for x in fc] == ["revoked"])

# G 校准 03-01 早于 C 失效起点 05-01 -> G 不判废；m1/m2 仍有效
r = client.get(f"/api/measurements/{m1['id']}").json()
check("m1 仍有效", r["measurement"]["valid"] is True)
r = client.get(f"/api/measurements/{m2['id']}").json()
check("m2 仍有效", r["measurement"]["valid"] is True)
r = client.get(f"/api/measurements/{m3['id']}").json()
check("m3 当前结论无效", r["measurement"]["valid"] is False
      and r["measurement"]["invalidated_revision"] == 9)

# 历史修订查询：撤销 C(rev=9) 之前 m3 结论保留为有效
r = client.get(f"/api/measurements/{m3['id']}?revision={rev_before_c_revoke}").json()
check("历史修订 m3 仍为有效（结果保留）",
      r["revision"] == 7 and r["measurement"]["valid"] is True, str(r))
r = client.get(f"/api/measurements/{m1['id']}?revision=4").json()
check("m1 在 rev=4 可查且有效", r["measurement"]["valid"] is True)
r = client.get("/api/measurements/1?revision=9999")
check("未来修订返回 404", r.status_code == 404)
r = client.get(f"/api/measurements/{m3['id']}?revision=8").json()
check("rev=8（撤销 R 后、撤销 C 前）m3 有效", r["measurement"]["valid"] is True)

# 失效后拒绝登记
r = post_meas(9, "2026-05-15T00:00:00Z", inst="A")
check("C 失效区间内登记被拒 certificate_expired",
      expect_error(r, 422, "certificate_expired"), r.text)
r = post_meas(9, "2026-09-01T00:00:00Z", inst="A")
check("无覆盖证书登记被拒 no_certificate",
      expect_error(r, 422, "no_certificate"), r.text)
r = post_meas(9, "2026-03-10T00:00:00Z", inst="B")  # G 仍有效
check("G 区间新登记仍成功（G 未判废）", r.status_code == 201, r.text)

print()
print("== 场景 2：递归整证失效 ==")
reset_db()

r = mkcert(0, "STD2", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
           "2027-01-01T00:00:00Z", 0.0, 0.5, root=True)
R2 = r.json()["certificate"]["id"]
r = mkcert(1, "A2", "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z",
           "2026-12-01T00:00:00Z", 0.0, 0.5, parent=R2)
C2 = r.json()["certificate"]["id"]
r = mkcert(2, "B2", "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z",
           "2026-11-01T00:00:00Z", 0.0, 0.5, parent=C2)
G2 = r.json()["certificate"]["id"]

r = post_meas(3, "2026-02-15T00:00:00Z", inst="A2")  # 绑定 C2
cm = r.json()["measurement"]
check("cm 绑定 C2", cm["certificate_id"] == C2, r.text)
r = post_meas(4, "2026-04-01T00:00:00Z", inst="B2")  # 绑定 G2
gm = r.json()["measurement"]
check("gm 绑定 G2", gm["certificate_id"] == G2, r.text)

rev_before = rev()  # 5
# 撤销 R2 于 01-15：C2(校准02-01) 落入 -> 整证失效；G2(校准03-01) 落入 C2 整条有效期 -> 递归失效
r = client.post(f"/api/certificates/{R2}/revoke", json={
    "expected_revision": 5,
    "revoked_at": "2026-01-15T00:00:00Z",
})
check("撤销 R2 成功", r.status_code == 200, r.text)
data = r.json()
inv_certs = [c["id"] for c in data["invalidated_certificates"]]
check("C2、G2 均递归判废", inv_certs == [C2, G2], str(inv_certs))
statuses = {c["id"]: c["status"] for c in data["invalidated_certificates"]}
check("判废证书 status=invalid", all(s == "invalid" for s in statuses.values()))
inv = data["invalidated_measurements"]
ids = sorted(x["measurement"]["id"] for x in inv)
check("cm、gm 均失效", ids == sorted([cm["id"], gm["id"]]), str(ids))
gm_item = [x for x in inv if x["measurement"]["id"] == gm["id"]][0]
chain_ids = [x["certificate_id"] for x in gm_item["failure_chain"]]
check("gm 失效链 G2(invalid)->C2(invalid)->R2(revoked)",
      chain_ids == [G2, C2, R2], str(chain_ids))
chain_status = [x["status"] for x in gm_item["failure_chain"]]
check("链状态 [invalid, invalid, revoked]", chain_status == ["invalid", "invalid", "revoked"])
check("根因 R2", gm_item["root_cause_certificate_id"] == R2)

# 历史结论保留：rev=5 时两条都有效
r = client.get(f"/api/measurements/{gm['id']}?revision={rev_before}").json()
check("历史 rev=5 gm 有效", r["measurement"]["valid"] is True)
r = client.get(f"/api/measurements/{cm['id']}?revision={rev_before}").json()
check("历史 rev=5 cm 有效", r["measurement"]["valid"] is True)
r = client.get(f"/api/measurements/{gm['id']}").json()
check("当前 gm 无效", r["measurement"]["valid"] is False)
r = client.get(f"/api/certificates/{G2}").json()
check("G2 当前 status=invalid", r["certificate"]["status"] == "invalid")
r = client.get(f"/api/certificates/{G2}?revision=5").json()
check("G2 在 rev=5 仍 valid", r["certificate"]["status"] == "valid")

# 判废链上不得再挂新证书
r = mkcert(6, "D2", "2026-04-01T00:00:00Z", "2026-04-01T00:00:00Z",
           "2026-10-01T00:00:00Z", 0.0, 0.5, parent=G2)
check("引用已判废上级被拒 parent_invalid",
      expect_error(r, 422, "parent_invalid"), r.text)
r = post_meas(6, "2026-05-01T00:00:00Z", inst="B2")
check("判废证书区间登记被拒 certificate_invalid",
      expect_error(r, 422, "certificate_invalid"), r.text)

print()
print("== 场景 3：登记校验 ==")
reset_db()

r = mkcert(0, "K", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
           "2026-12-31T00:00:00Z", 0.6, 0.5, root=True)
check("误差超容差拒绝", expect_error(r, 422, "tolerance_exceeded"), r.text)

r = client.post("/api/certificates", json={
    "expected_revision": 0, "instrument_id": "K",
    "calibrated_at": "2026-01-01T00:00:00Z",
    "valid_from": "2026-06-01T00:00:00Z",
    "valid_to": "2026-01-01T00:00:00Z",
    "error_value": 0.0, "tolerance": 0.0, "is_root": True})
check("起点晚于终点拒绝", expect_error(r, 422, "invalid_validity_window"), r.text)

r = client.post("/api/certificates", json={
    "expected_revision": 0, "instrument_id": "K",
    "calibrated_at": "2026-01-01T00:00:00Z",
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to": "2026-01-01T00:00:00Z",
    "error_value": 0.0, "tolerance": 0.0, "is_root": True})
check("起点等于终点拒绝", expect_error(r, 422, "invalid_validity_window"), r.text)

r = client.post("/api/certificates", json={
    "expected_revision": 0, "instrument_id": "K",
    "calibrated_at": "2026-01-01T00:00:00Z",
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to": "2026-02-01T00:00:00Z",
    "error_value": 0.0, "tolerance": -0.1, "is_root": True})
check("负容差拒绝(422)", r.status_code == 422, r.text)

r = mkcert(0, "K", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
           "2026-12-01T00:00:00Z", 0.0, 0.5)
check("非根无 parent 拒绝", expect_error(r, 422, "non_root_requires_parent"), r.text)

# 建立根 K_root [01-01, 12-01)
r = mkcert(0, "K", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
           "2026-12-01T00:00:00Z", 0.0, 0.5, root=True)
check("K_root 登记成功", r.status_code == 201, r.text)
K_root = r.json()["certificate"]["id"]

r = client.post("/api/certificates", json={
    "expected_revision": 1, "instrument_id": "K",
    "calibrated_at": "2026-02-01T00:00:00Z",
    "valid_from": "2026-02-01T00:00:00Z",
    "valid_to": "2026-11-01T00:00:00Z",
    "error_value": 0.0, "tolerance": 0.5,
    "parent_id": K_root, "is_root": True})
check("根证书带 parent 拒绝", expect_error(r, 422, "root_must_have_no_parent"), r.text)

r = mkcert(1, "K2", "2025-12-31T00:00:00Z", "2025-12-31T00:00:00Z",
           "2026-06-01T00:00:00Z", 0.0, 0.5, parent=K_root)
check("校准时刻不在上级有效期拒绝",
      expect_error(r, 422, "parent_not_effective_at_calibration"), r.text)

# 半开区间：K_root [01-01,12-01)，新证书（器具 K）起点 12-01 不重叠 -> 成功
r = mkcert(1, "K", "2026-02-01T00:00:00Z", "2026-12-01T00:00:00Z",
           "2026-12-31T00:00:00Z", 0.0, 0.5, parent=K_root)
check("半开相邻有效期不重叠 -> 成功", r.status_code == 201, r.text)
K_child = r.json()["certificate"]["id"]

# 重叠拒绝
r = mkcert(2, "K", "2026-02-01T00:00:00Z", "2026-06-01T00:00:00Z",
           "2026-07-01T00:00:00Z", 0.0, 0.5, parent=K_root)
check("同器具有效期重叠拒绝", expect_error(r, 422, "validity_overlap"), r.text)

# 不同器具重叠允许
r = mkcert(2, "L", "2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z",
           "2026-12-01T00:00:00Z", 0.0, 0.5, root=True)
check("不同器具重叠允许", r.status_code == 201, r.text)

print()
print("== 场景 4：乐观修订并发控制 ==")
cur = rev()  # 3
r = mkcert(cur - 1, "M", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
           "2027-01-01T00:00:00Z", 0.0, 0.5, root=True)
check("过期 expected_revision -> 409", r.status_code == 409, r.text)
body = r.json()["detail"]
check("409 体含 current_revision", body.get("current_revision") == cur, str(body))
check("409 后修订号未被推进", rev() == cur)

r = post_meas(cur - 1, "2026-03-01T00:00:00Z", inst="K")
check("测量登记过期修订 -> 409", r.status_code == 409, r.text)
r = client.post(f"/api/certificates/{K_root}/revoke", json={
    "expected_revision": cur - 1, "revoked_at": "2026-06-01T00:00:00Z"})
check("撤销过期修订 -> 409", r.status_code == 409, r.text)

r = client.post(f"/api/certificates/{K_root}/revoke", json={
    "expected_revision": cur, "revoked_at": "2027-06-01T00:00:00Z"})
check("撤销时刻超出有效期 -> 422",
      expect_error(r, 422, "revocation_outside_validity"), r.text)

# 正常撤销：K_child 校准 02-01 早于 06-01 -> 不判废
r = client.post(f"/api/certificates/{K_root}/revoke", json={
    "expected_revision": cur, "revoked_at": "2026-06-01T00:00:00Z"})
check("首次撤销成功", r.status_code == 200, r.text)
new_cur = rev()
r = client.post(f"/api/certificates/{K_root}/revoke", json={
    "expected_revision": new_cur, "revoked_at": "2026-07-01T00:00:00Z"})
check("重复撤销 -> 422", expect_error(r, 422, "certificate_not_revocable"), r.text)

print()
print("== 场景 5：显式绑定校验与列表 ==")
r = client.post("/api/measurements", json={
    "expected_revision": new_cur, "instrument_id": "ZZZ", "value": 1.0,
    "measured_at": "2026-03-01T00:00:00Z", "certificate_id": K_root})
check("器具不匹配 -> 422",
      expect_error(r, 422, "certificate_instrument_mismatch"), r.text)
r = client.post("/api/measurements", json={
    "expected_revision": new_cur, "instrument_id": "K", "value": 1.0,
    "measured_at": "2025-01-01T00:00:00Z", "certificate_id": K_root})
check("证书不覆盖测量时刻 -> 422",
      expect_error(r, 422, "certificate_not_covering"), r.text)
r = client.post("/api/measurements", json={
    "expected_revision": new_cur, "instrument_id": "K", "value": 1.0,
    "measured_at": "2026-06-02T00:00:00Z", "certificate_id": K_root})
check("撤销后时刻显式绑定 -> 422 certificate_expired",
      expect_error(r, 422, "certificate_expired"), r.text)
r = client.post("/api/measurements", json={
    "expected_revision": new_cur, "instrument_id": "K", "value": 1.0,
    "measured_at": "2026-03-01T00:00:00Z", "certificate_id": 999999})
check("不存在的证书 -> 404", r.status_code == 404, r.text)
# 撤销时刻之前显式绑定仍允许
r = client.post("/api/measurements", json={
    "expected_revision": new_cur, "instrument_id": "K", "value": 2.5,
    "measured_at": "2026-03-01T00:00:00Z", "certificate_id": K_root})
check("撤销时刻之前显式绑定 -> 成功", r.status_code == 201, r.text)
check_m = r.json()["measurement"]
check("新测量 valid=True", check_m["valid"] is True)

r = client.get("/api/certificates?instrument_id=L").json()
check("按器具过滤证书", len(r["certificates"]) == 1
      and all(c["instrument_id"] == "L" for c in r["certificates"]))
r = client.get("/api/measurements?instrument_id=K").json()
check("按器具过滤测量", len(r["measurements"]) == 1
      and all(m["instrument_id"] == "K" for m in r["measurements"]))

# 健康检查
r = client.get("/health")
check("健康检查", r.status_code == 200 and r.json()["status"] == "ok")

print()
print("== 场景 6：已撤销子证书被上级判废（混合传播） ==")
reset_db()

r = mkcert(0, "STD3", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
           "2027-01-01T00:00:00Z", 0.0, 0.5, root=True)
R3 = r.json()["certificate"]["id"]
r = mkcert(1, "A3", "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z",
           "2026-12-01T00:00:00Z", 0.0, 0.5, parent=R3)
C3 = r.json()["certificate"]["id"]
r = mkcert(2, "B3", "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z",
           "2026-11-01T00:00:00Z", 0.0, 0.5, parent=C3)
G3 = r.json()["certificate"]["id"]

r = post_meas(3, "2026-02-15T00:00:00Z", inst="A3")
m_pre = r.json()["measurement"]["id"]  # C 撤销前区间
r = post_meas(4, "2026-06-15T00:00:00Z", inst="A3")
m_post = r.json()["measurement"]["id"]  # C 撤销后区间
r = post_meas(5, "2026-04-01T00:00:00Z", inst="B3")
m_g = r.json()["measurement"]["id"]

# 先撤销 C3 于 05-01：m_post 失效，m_pre、m_g 保留
r = client.post(f"/api/certificates/{C3}/revoke", json={
    "expected_revision": 6, "revoked_at": "2026-05-01T00:00:00Z"})
check("先撤销 C3 成功", r.status_code == 200, r.text)
ids = sorted(x["measurement"]["id"] for x in r.json()["invalidated_measurements"])
check("首轮仅 m_post 失效", ids == [m_post], str(ids))

# 再撤销 R3 于 01-15：C3 校准落入 -> C3 整证判废（翻转撤销前的 m_pre），
# C3 整条有效期失效 -> G3 递归判废（翻转 m_g）；m_post 已失效不再列出
r = client.post(f"/api/certificates/{R3}/revoke", json={
    "expected_revision": 7, "revoked_at": "2026-01-15T00:00:00Z"})
check("再撤销 R3 成功", r.status_code == 200, r.text)
data = r.json()
ids = sorted(x["measurement"]["id"] for x in data["invalidated_measurements"])
check("次轮新失效为 m_pre 与 m_g", ids == sorted([m_pre, m_g]), str(ids))
inv_certs = [c["id"] for c in data["invalidated_certificates"]]
check("C3、G3 判废", inv_certs == [C3, G3], str(inv_certs))
g_item = [x for x in data["invalidated_measurements"] if x["measurement"]["id"] == m_g][0]
check("m_g 失效链 G3->C3->R3",
      [x["certificate_id"] for x in g_item["failure_chain"]] == [G3, C3, R3])

# 历史结论：rev=6（两次撤销前）m_pre、m_post 均有效；
# rev=7（撤销 C3 后）m_post 无效、m_pre 仍有效；rev=8 时全部无效
r = client.get(f"/api/measurements/{m_pre}?revision=6").json()
check("历史 rev=6 m_pre 有效", r["measurement"]["valid"] is True)
r = client.get(f"/api/measurements/{m_post}?revision=6").json()
check("历史 rev=6 m_post 有效（撤销尚未发生）", r["measurement"]["valid"] is True)
r = client.get(f"/api/measurements/{m_post}?revision=7").json()
check("历史 rev=7 m_post 无效", r["measurement"]["valid"] is False)
r = client.get(f"/api/measurements/{m_pre}?revision=7").json()
check("历史 rev=7 m_pre 仍有效", r["measurement"]["valid"] is True)
for mid in (m_pre, m_post, m_g):
    r = client.get(f"/api/measurements/{mid}").json()
    check(f"当前 rev 测量 {mid} 无效", r["measurement"]["valid"] is False)

print()
print("== 场景 7：撤销因果追溯——上级晚撤销不列为根因 ==")
reset_db()

# R -> C -> G 三级链，每级校准时上级均有效
r = mkcert(0, "STD7", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
           "2027-01-01T00:00:00Z", 0.0, 0.5, root=True)
R7 = r.json()["certificate"]["id"]
r = mkcert(1, "A7", "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z",
           "2026-12-01T00:00:00Z", 0.0, 0.5, parent=R7)
C7 = r.json()["certificate"]["id"]
r = mkcert(2, "B7", "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z",
           "2026-11-01T00:00:00Z", 0.0, 0.5, parent=C7)
G7 = r.json()["certificate"]["id"]

r = post_meas(3, "2026-07-15T08:00:00Z", inst="A7")  # 绑定 C7，落在 C7 撤销后区间
m_c = r.json()["measurement"]["id"]
r = post_meas(4, "2026-10-15T08:00:00Z", inst="B7")  # 绑定 G7，落在 G7 撤销后区间
m_g7 = r.json()["measurement"]["id"]
rev_pre = rev()  # 5

# 先在 C 校准时刻之后撤销 R（02-15 > 02-01）：C、G 均不判废，测量不失效
r = client.post(f"/api/certificates/{R7}/revoke", json={
    "expected_revision": 5, "revoked_at": "2026-02-15T00:00:00Z"})
check("撤销 R7 成功", r.status_code == 200, r.text)
data = r.json()
check("R7 晚撤销无证书判废", data["invalidated_certificates"] == [])
check("R7 晚撤销无测量失效", data["invalidated_measurement_count"] == 0)

# 再撤销 C（05-01，晚于 G 校准 03-01）：m_c 因 C 自身撤销首次失效
r = client.post(f"/api/certificates/{C7}/revoke", json={
    "expected_revision": 6, "revoked_at": "2026-05-01T00:00:00Z"})
check("撤销 C7 成功", r.status_code == 200, r.text)
data = r.json()
inv = data["invalidated_measurements"]
check("仅 m_c 本次失效", [x["measurement"]["id"] for x in inv] == [m_c], str(inv))
fc = inv[0]["failure_chain"]
check("m_c 失效链仅含 C7（R7 晚撤销不计入）",
      [x["certificate_id"] for x in fc] == [C7],
      str([x["certificate_id"] for x in fc]))
check("m_c 根因为 C7", inv[0]["root_cause_certificate_id"] == C7)
check("G7 未判废（校准 03-01 早于 C7 失效起点）",
      data["invalidated_certificates"] == [])

# 最后撤销 G（09-01）：m_g7 因 G 自身撤销失效；链上 C、R 虽均已撤销，
# 但失效区间均未覆盖 G 的校准时刻，根因仍为 G
r = client.post(f"/api/certificates/{G7}/revoke", json={
    "expected_revision": 7, "revoked_at": "2026-09-01T00:00:00Z"})
check("撤销 G7 成功", r.status_code == 200, r.text)
data = r.json()
inv = data["invalidated_measurements"]
check("仅 m_g7 本次失效", [x["measurement"]["id"] for x in inv] == [m_g7], str(inv))
fc = inv[0]["failure_chain"]
check("m_g7 失效链仅含 G7（C7/R7 晚撤销均不计入）",
      [x["certificate_id"] for x in fc] == [G7],
      str([x["certificate_id"] for x in fc]))
check("m_g7 根因为 G7", inv[0]["root_cause_certificate_id"] == G7)

# 历史修订查询不回归：rev=5（全部撤销前）两条测量均有效
r = client.get(f"/api/measurements/{m_c}?revision={rev_pre}").json()
check("历史 rev=5 m_c 有效", r["measurement"]["valid"] is True)
r = client.get(f"/api/measurements/{m_g7}?revision={rev_pre}").json()
check("历史 rev=5 m_g7 有效", r["measurement"]["valid"] is True)
r = client.get(f"/api/measurements/{m_c}?revision=6").json()
check("rev=6（R7 已撤销）m_c 仍有效", r["measurement"]["valid"] is True)
r = client.get(f"/api/measurements/{m_c}").json()
check("当前 m_c 无效", r["measurement"]["valid"] is False)
r = client.get(f"/api/measurements/{m_g7}").json()
check("当前 m_g7 无效", r["measurement"]["valid"] is False)

print()
print(f"==== 结果：{PASS} 通过，{FAIL} 失败 ====")
import sys
sys.exit(1 if FAIL else 0)
