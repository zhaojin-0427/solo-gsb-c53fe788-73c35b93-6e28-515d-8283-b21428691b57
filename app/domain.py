"""领域视图：按指定修订计算证书与测量的历史状态。"""
from __future__ import annotations

from typing import Any


def iso(ts) -> str | None:
    """UTC 时间统一序列化为 ...Z 形式。"""
    if ts is None:
        return None
    return ts.isoformat().replace("+00:00", "Z")


def cert_status(cert: dict[str, Any], revision: int) -> str:
    """证书在某一修订下的状态。

    - invalid：因上级失效区间覆盖其校准时刻，被递归判定为整证无效
    - revoked：已撤销（撤销时刻之后的区间失效，之前仍可信）
    - valid：当前有效
    """
    inv = cert["invalidated_revision"]
    if inv is not None and inv <= revision:
        return "invalid"
    rev = cert["revoked_revision"]
    if rev is not None and rev <= revision:
        # 撤销时刻之后的区间失效，之前仍可信
        return "revoked"
    return "valid"


def cert_failure_window(cert: dict[str, Any], revision: int):
    """返回 (失效起点, 失效终点)；整证无效为整条有效期，撤销为 [revoked_at, valid_to)。"""
    status = cert_status(cert, revision)
    if status == "invalid":
        return cert["valid_from"], cert["valid_to"]
    if status == "revoked":
        return cert["revoked_at"], cert["valid_to"]
    return None, None


def serialize_cert(cert: dict[str, Any], revision: int) -> dict[str, Any]:
    fail_from, fail_to = cert_failure_window(cert, revision)
    return {
        "id": cert["id"],
        "instrument_id": cert["instrument_id"],
        "serial": cert["serial"],
        "calibrated_at": iso(cert["calibrated_at"]),
        "valid_from": iso(cert["valid_from"]),
        "valid_to": iso(cert["valid_to"]),
        "error_value": cert["error_value"],
        "tolerance": cert["tolerance"],
        "parent_id": cert["parent_id"],
        "is_root": cert["is_root"],
        "revoked_at": iso(cert["revoked_at"]),
        "revoked_revision": cert["revoked_revision"],
        "invalidated_revision": cert["invalidated_revision"],
        "created_revision": cert["created_revision"],
        "status": cert_status(cert, revision),
        "failure_window": {
            "from": iso(fail_from),
            "to": iso(fail_to),
        } if fail_from is not None else None,
    }


def causal_failure_chain(
    chain: list[dict[str, Any]], revision: int
) -> list[dict[str, Any]]:
    """按每级子证书的校准时刻向上追溯失效因果链，返回 chain 的前缀。

    从绑定证书开始逐级向上：
    - 本级未失效（valid）：不属于失效链，终止；
    - 本级已失效（revoked / invalid）：计入失效链；仅当本级校准时刻落入
      上级失效区间时，上级失效才是本级失效的原因（递归判废），继续向上；
      否则本级系自身撤销导致失效——上级晚撤销与本次失效无因果，不计入，
      本级即根因，链到此为止。

    返回列表的最后一个元素即根因证书。
    """
    links: list[dict[str, Any]] = []
    for idx, cert in enumerate(chain):
        if cert_status(cert, revision) not in ("revoked", "invalid"):
            break
        links.append(cert)
        parent = chain[idx + 1] if idx + 1 < len(chain) else None
        if parent is None:
            break
        fail_from, fail_to = cert_failure_window(parent, revision)
        if fail_from is None or not (fail_from <= cert["calibrated_at"] < fail_to):
            break
    return links


def load_cert_chain(conn, cert_id: int, revision: int) -> list[dict[str, Any]]:
    """沿 parent_id 向上取完整证书链（测量证书 -> ... -> 根证书）。"""
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    current = cert_id
    while current is not None and current not in seen:
        seen.add(current)
        row = conn.execute(
            "SELECT * FROM certificate WHERE id = %s", (current,)
        ).fetchone()
        if row is None:
            break
        chain.append(row)
        current = row["parent_id"]
    return chain


def measurement_valid(meas: dict[str, Any], revision: int) -> bool:
    """测量在某一修订下是否有效（invalidated_revision 为 None 即始终未失效）。"""
    inv = meas["invalidated_revision"]
    return inv is None or inv > revision


def compute_drift(
    measurements: list[dict[str, Any]], revision: int, max_delta: float
) -> tuple[int, list[dict[str, Any]]]:
    """对已按 (measured_at, id) 升序排列的测量做相邻比较，返回 (比较次数, 跃变告警)。

    比较基线规则：
    - 无效测量（沿用 measurement_valid 的历史有效性判定）不参与比较，
      且重置基线——不得跳过它拼接两侧有效测量；
    - 相邻有效测量绑定不同证书（证书切换）同样重置基线，仅同证书的
      相邻有效测量产生一次比较；
    - 每次比较后，当前有效测量即成为新的基线（即使本次触发了跃变告警）。

    绝对差严格大于 max_delta 才告警，delta 为后值减前值（带方向）。
    """
    comparisons = 0
    drifts: list[dict[str, Any]] = []
    baseline: dict[str, Any] | None = None
    for m in measurements:
        if not measurement_valid(m, revision):
            # 无效测量打断可比性：两侧不得跨它拼接
            baseline = None
            continue
        if baseline is not None and m["certificate_id"] == baseline["certificate_id"]:
            comparisons += 1
            delta = m["value"] - baseline["value"]
            if abs(delta) > max_delta:
                drifts.append({
                    "previous_measurement_id": baseline["id"],
                    "measurement_id": m["id"],
                    "delta": delta,
                    "abs_delta": abs(delta),
                })
        # 证书切换（certificate_id 不同）不比较，当前测量直接成为新基线
        baseline = m
    return comparisons, drifts


def serialize_measurement(
    meas: dict[str, Any],
    revision: int,
    cert: dict[str, Any] | None = None,
    chain: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = {
        "id": meas["id"],
        "instrument_id": meas["instrument_id"],
        "value": meas["value"],
        "measured_at": iso(meas["measured_at"]),
        "certificate_id": meas["certificate_id"],
        "created_revision": meas["created_revision"],
        "invalidated_revision": meas["invalidated_revision"],
        "revision": revision,
        "valid": measurement_valid(meas, revision),
    }
    if cert is not None:
        result["certificate_status"] = cert_status(cert, revision)
    if chain is not None:
        result["certificate_chain"] = [
            {
                "certificate_id": c["id"],
                "instrument_id": c["instrument_id"],
                "is_root": c["is_root"],
                "parent_id": c["parent_id"],
                "status": cert_status(c, revision),
            }
            for c in chain
        ]
    return result
