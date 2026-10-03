"""校准链失效追溯 API。

写接口（登记证书 / 登记测量 / 撤销证书）均须携带 expected_revision，
服务端在单个事务内校验并原子推进全局修订号，过期返回 409。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import math
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import db, domain
from .domain import (
    iso,
    load_cert_chain,
    serialize_cert,
    serialize_measurement,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_pool()
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(title="校准链失效追溯 API", version="1.0.0", lifespan=lifespan)


@app.exception_handler(psycopg.IntegrityError)
async def pg_error_handler(request: Request, exc: psycopg.IntegrityError):
    """数据库完整性约束冲突兜底为 422（排他约束等），避免裸 500。"""
    return JSONResponse(
        status_code=422,
        content={"detail": {
            "error": "database_constraint_violation",
            "message": str(exc).split("\n")[0],
        }},
    )


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class CertificateIn(BaseModel):
    expected_revision: int = Field(ge=0)
    instrument_id: str = Field(min_length=1)
    serial: str | None = None
    calibrated_at: datetime
    valid_from: datetime
    valid_to: datetime
    error_value: float
    tolerance: float = Field(ge=0)
    is_root: bool = False
    parent_id: int | None = None

    @field_validator("error_value", "tolerance")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("数值必须为有限数")
        return v


class MeasurementIn(BaseModel):
    expected_revision: int = Field(ge=0)
    instrument_id: str = Field(min_length=1)
    value: float
    measured_at: datetime
    certificate_id: int | None = None  # 可显式指定绑定证书；不指定则自动解析

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("数值必须为有限数")
        return v


class RevokeIn(BaseModel):
    expected_revision: int = Field(ge=0)
    revoked_at: datetime


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def utc(ts: datetime) -> datetime:
    """归一化为 UTC（无时区的输入按 UTC 处理）。"""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def current_revision(conn) -> int:
    row = conn.execute("SELECT value FROM revision WHERE id = 1").fetchone()
    return row["value"]


def bump_revision(conn) -> int:
    """对单行加行锁并原子推进修订号，返回新修订号。"""
    row = conn.execute(
        "UPDATE revision SET value = value + 1 WHERE id = 1 RETURNING value"
    ).fetchone()
    return row["value"]


def check_expected(conn, expected: int) -> int:
    """行锁 + 修订比对：过期的写请求返回 409 Conflict；返回当前修订。"""
    row = conn.execute(
        "SELECT value FROM revision WHERE id = 1 FOR UPDATE"
    ).fetchone()
    if row["value"] != expected:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "revision_conflict",
                "message": "expected_revision 已过期",
                "expected_revision": expected,
                "current_revision": row["value"],
            },
        )
    return row["value"]


def get_cert(conn, cert_id: int) -> dict[str, Any]:
    cert = conn.execute(
        "SELECT * FROM certificate WHERE id = %s", (cert_id,)
    ).fetchone()
    if cert is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "certificate_not_found", "certificate_id": cert_id},
        )
    return cert


# ---------------------------------------------------------------------------
# 基础接口
# ---------------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "service": "校准链失效追溯 API",
        "docs": "/docs",
        "health": "/health",
        "revision": "/api/revision",
    }


@app.get("/health")
def health(conn=Depends(db.get_conn)):
    rev = current_revision(conn)
    conn.commit()
    return {"status": "ok", "revision": rev}


@app.get("/api/revision")
def get_revision(conn=Depends(db.get_conn)):
    rev = current_revision(conn)
    conn.commit()
    return {"revision": rev}


# ---------------------------------------------------------------------------
# 证书登记
# ---------------------------------------------------------------------------
@app.post("/api/certificates", status_code=201)
def create_certificate(body: CertificateIn, conn=Depends(db.get_conn)):
    calibrated_at = utc(body.calibrated_at)
    valid_from = utc(body.valid_from)
    valid_to = utc(body.valid_to)

    # 半开有效期：起点必须早于终点
    if not valid_from < valid_to:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_validity_window",
                "message": "半开有效期要求 valid_from < valid_to",
            },
        )

    # 误差绝对值不得超过非负容差
    if abs(body.error_value) > body.tolerance:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "tolerance_exceeded",
                "message": "误差绝对值不得超过容差",
                "error_value": body.error_value,
                "tolerance": body.tolerance,
            },
        )

    # 根 / 非根约束
    if body.is_root and body.parent_id is not None:
        raise HTTPException(
            status_code=422,
            detail={"error": "root_must_have_no_parent", "message": "根证书不得引用上级证书"},
        )
    if not body.is_root and body.parent_id is None:
        raise HTTPException(
            status_code=422,
            detail={"error": "non_root_requires_parent", "message": "非根证书必须引用上级证书"},
        )

    rev = check_expected(conn, body.expected_revision)

    parent = None
    if not body.is_root:
        parent = get_cert(conn, body.parent_id)

        if domain.cert_status(parent, rev) == "invalid":
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "parent_invalid",
                    "message": "上级证书已因校准链失效递归判废",
                    "parent_id": parent["id"],
                },
            )

        # 上级证书在本证书校准时刻必须有效
        pf, pt = domain.cert_failure_window(parent, rev)
        if domain.cert_status(parent, rev) == "revoked":
            if not (calibrated_at < pf):  # 撤销时刻（含）起即失效
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "parent_not_effective_at_calibration",
                        "message": "校准时刻落入上级证书的失效区间",
                        "parent_id": parent["id"],
                        "failure_window": {"from": iso(pf), "to": iso(pt)},
                    },
                )

        if not (parent["valid_from"] <= calibrated_at < parent["valid_to"]):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "parent_not_effective_at_calibration",
                    "message": "校准时刻不在上级证书有效期内",
                    "parent_id": parent["id"],
                    "valid_from": iso(parent["valid_from"]),
                    "valid_to": iso(parent["valid_to"]),
                },
            )

    # 同一器具证书有效期不得重叠（排他约束兜底）
    overlap = conn.execute(
        """
        SELECT id FROM certificate
        WHERE instrument_id = %s
          AND tstzrange(valid_from, valid_to, '[)') && tstzrange(%s, %s, '[)')
        LIMIT 1
        """,
        (body.instrument_id, valid_from, valid_to),
    ).fetchone()
    if overlap is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validity_overlap",
                "message": "同一器具的证书有效期不得重叠",
                "conflict_certificate_id": overlap["id"],
            },
        )

    new_rev = bump_revision(conn)
    row = conn.execute(
        """
        INSERT INTO certificate (
            instrument_id, serial, calibrated_at, valid_from, valid_to,
            error_value, tolerance, parent_id, is_root,
            revoked_at, revoked_revision, invalidated_revision, created_revision
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL,NULL,%s)
        RETURNING *
        """,
        (
            body.instrument_id,
            body.serial,
            calibrated_at,
            valid_from,
            valid_to,
            body.error_value,
            body.tolerance,
            None if body.is_root else body.parent_id,
            body.is_root,
            new_rev,
        ),
    ).fetchone()

    chain = load_cert_chain(conn, row["id"], new_rev)
    return {
        "revision": new_rev,
        "certificate": serialize_cert(row, new_rev),
        "certificate_chain": [
            {
                "certificate_id": c["id"],
                "instrument_id": c["instrument_id"],
                "is_root": c["is_root"],
                "parent_id": c["parent_id"],
                "status": domain.cert_status(c, new_rev),
            }
            for c in chain
        ],
    }


# ---------------------------------------------------------------------------
# 测量登记
# ---------------------------------------------------------------------------
@app.post("/api/measurements", status_code=201)
def create_measurement(body: MeasurementIn, conn=Depends(db.get_conn)):
    measured_at = utc(body.measured_at)

    rev = check_expected(conn, body.expected_revision)

    cert = None
    if body.certificate_id is not None:
        cert = get_cert(conn, body.certificate_id)
        if cert["instrument_id"] != body.instrument_id:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "certificate_instrument_mismatch",
                    "message": "指定证书不属于该器具",
                },
            )
        # 必须覆盖测量时刻
        if not (cert["valid_from"] <= measured_at < cert["valid_to"]):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "certificate_not_covering",
                    "message": "指定证书的有效期不覆盖测量时刻",
                    "valid_from": iso(cert["valid_from"]),
                    "valid_to": iso(cert["valid_to"]),
                },
            )
        # 递归判废 / 撤销时刻后的区间失效
        status = domain.cert_status(cert, rev)
        if status == "invalid":
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "certificate_invalid",
                    "message": "绑定证书已因校准链递归失效",
                    "certificate_id": cert["id"],
                },
            )
        if status == "revoked" and measured_at >= cert["revoked_at"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "certificate_expired",
                    "message": "绑定证书在测量时刻已撤销失效",
                    "certificate_id": cert["id"],
                    "revoked_at": iso(cert["revoked_at"]),
                },
            )
    else:
        # 自动解析：覆盖该时刻、当前仍有效（或撤销前区间仍可信）的证书
        cert = conn.execute(
            """
            SELECT * FROM certificate
            WHERE instrument_id = %s
              AND valid_from <= %s AND %s < valid_to
            ORDER BY id
            """,
            (body.instrument_id, measured_at, measured_at),
        ).fetchone()
        if cert is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "no_certificate",
                    "message": "不存在覆盖该测量时刻的证书，登记被拒绝",
                    "instrument_id": body.instrument_id,
                    "measured_at": iso(measured_at),
                },
            )
        status = domain.cert_status(cert, rev)
        if status == "invalid":
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "certificate_invalid",
                    "message": "唯一覆盖该时刻的证书已递归失效，登记被拒绝",
                    "certificate_id": cert["id"],
                },
            )
        if status == "revoked" and measured_at >= cert["revoked_at"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "certificate_expired",
                    "message": "覆盖该时刻的证书在测量时刻已撤销失效，登记被拒绝",
                    "certificate_id": cert["id"],
                    "revoked_at": iso(cert["revoked_at"]),
                },
            )

    new_rev = bump_revision(conn)
    row = conn.execute(
        """
        INSERT INTO measurement (
            instrument_id, value, measured_at, certificate_id,
            created_revision, invalidated_revision
        ) VALUES (%s,%s,%s,%s,%s,NULL)
        RETURNING *
        """,
        (body.instrument_id, body.value, measured_at, cert["id"], new_rev),
    ).fetchone()
    chain = load_cert_chain(conn, cert["id"], new_rev)
    return {
        "revision": new_rev,
        "measurement": serialize_measurement(row, new_rev, cert, chain),
    }


# ---------------------------------------------------------------------------
# 证书撤销（级联重判）
# ---------------------------------------------------------------------------
@app.post("/api/certificates/{cert_id}/revoke")
def revoke_certificate(cert_id: int, body: RevokeIn, conn=Depends(db.get_conn)):
    revoked_at = utc(body.revoked_at)

    rev = check_expected(conn, body.expected_revision)

    cert = get_cert(conn, cert_id)

    if domain.cert_status(cert, rev) != "valid":
        raise HTTPException(
            status_code=422,
            detail={
                "error": "certificate_not_revocable",
                "message": "仅当前有效的证书可撤销",
                "certificate_id": cert_id,
                "status": domain.cert_status(cert, rev),
            },
        )

    # 撤销时刻必须位于有效期内（半开区间）
    if not (cert["valid_from"] <= revoked_at < cert["valid_to"]):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "revocation_outside_validity",
                "message": "撤销时刻须位于证书有效期内",
                "valid_from": iso(cert["valid_from"]),
                "valid_to": iso(cert["valid_to"]),
            },
        )

    new_rev = bump_revision(conn)

    # 1) 写入撤销
    conn.execute(
        """
        UPDATE certificate
        SET revoked_at = %s, revoked_revision = %s
        WHERE id = %s
        """,
        (revoked_at, new_rev, cert_id),
    )

    invalid_certs: list[dict[str, Any]] = []

    def mark_wholesale(c: dict[str, Any]) -> None:
        """整证递归判废：标记证书 + 翻转其此前仍有效的全部测量。"""
        conn.execute(
            "UPDATE certificate SET invalidated_revision = %s WHERE id = %s",
            (new_rev, c["id"]),
        )
        conn.execute(
            """
            UPDATE measurement SET invalidated_revision = %s
            WHERE certificate_id = %s AND invalidated_revision IS NULL
            """,
            (new_rev, c["id"]),
        )
        c = dict(c)
        c["invalidated_revision"] = new_rev
        invalid_certs.append(c)

    # 2) 直接落在撤销证书失效区间内的测量立即重判为无效
    conn.execute(
        """
        UPDATE measurement SET invalidated_revision = %s
        WHERE certificate_id = %s
          AND measured_at >= %s AND invalidated_revision IS NULL
        """,
        (new_rev, cert_id, revoked_at),
    )

    # 3) 递归失效传播（BFS）：
    #    子证书仅当其校准时刻落入上级失效区间时整证失效；
    #    整证失效证书的失效区间即其整条有效期，继续向下传播。
    frontier = [(cert_id, revoked_at, cert["valid_to"])]
    while frontier:
        parent_cid, fail_from, fail_to = frontier.pop(0)
        children = conn.execute(
            "SELECT * FROM certificate WHERE parent_id = %s ORDER BY id",
            (parent_cid,),
        ).fetchall()
        for child in children:
            if not (fail_from <= child["calibrated_at"] < fail_to):
                continue
            status = domain.cert_status(child, new_rev)
            if status == "invalid":
                # 已整证判废的子树此前必已完整传播，无需再处理
                continue
            # 已撤销子证书校准时刻落入上级失效区间时同样整证判废，
            # 翻转其撤销前仍有效的测量；有效证书亦然。
            mark_wholesale(child)
            frontier.append((child["id"], child["valid_from"], child["valid_to"]))

    # 4) 汇总本次转为无效的测量（直接失效 + 递归失效），按 id 排序
    rows = conn.execute(
        """
        SELECT * FROM measurement
        WHERE invalidated_revision = %s
        ORDER BY id
        """,
        (new_rev,),
    ).fetchall()

    invalid_measurements: list[dict[str, Any]] = []
    for r in rows:
        meas = {
            "id": r["id"],
            "instrument_id": r["instrument_id"],
            "value": r["value"],
            "measured_at": r["measured_at"],
            "certificate_id": r["certificate_id"],
            "created_revision": r["created_revision"],
            "invalidated_revision": r["invalidated_revision"],
        }
        chain_rows = load_cert_chain(conn, r["certificate_id"], new_rev)
        chain = [
            {
                "certificate_id": c["id"],
                "instrument_id": c["instrument_id"],
                "is_root": c["is_root"],
                "parent_id": c["parent_id"],
                "status": domain.cert_status(c, new_rev),
            }
            for c in chain_rows
        ]
        # 失效因果链：按每级子证书的校准时刻向上追溯（causal_failure_chain
        # 返回 chain_rows 的前缀）。上级晚撤销、未覆盖本级校准时刻的环节不计入，
        # 链尾即根因证书（自身撤销或递归判废的最近一级）。
        causal = domain.causal_failure_chain(chain_rows, new_rev)
        failure_chain = chain[: len(causal)]
        invalid_measurements.append(
            {
                "measurement": serialize_measurement(
                    meas, new_rev, chain_rows[0], chain_rows
                ),
                "failure_chain": failure_chain,
                "root_cause_certificate_id": failure_chain[-1]["certificate_id"],
            }
        )

    revoked_cert = conn.execute(
        "SELECT * FROM certificate WHERE id = %s", (cert_id,)
    ).fetchone()

    return {
        "revision": new_rev,
        "revoked_certificate": serialize_cert(revoked_cert, new_rev),
        "invalidated_certificates": [
            serialize_cert(c, new_rev) for c in sorted(invalid_certs, key=lambda x: x["id"])
        ],
        "invalidated_measurements": invalid_measurements,
        "invalidated_measurement_count": len(invalid_measurements),
    }


# ---------------------------------------------------------------------------
# 查询接口（支持按历史修订查询）
# ---------------------------------------------------------------------------
@app.get("/api/certificates")
def list_certificates(
    instrument_id: str | None = None,
    revision: int | None = Query(default=None, ge=0),
    conn=Depends(db.get_conn),
):
    rev = current_revision(conn) if revision is None else revision
    if rev > current_revision(conn):
        conn.rollback()
        raise HTTPException(
            status_code=404,
            detail={"error": "revision_not_found", "revision": rev},
        )
    sql = "SELECT * FROM certificate WHERE created_revision <= %s"
    params: list[Any] = [rev]
    if instrument_id is not None:
        sql += " AND instrument_id = %s"
        params.append(instrument_id)
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()
    conn.commit()
    return {
        "revision": rev,
        "certificates": [serialize_cert(r, rev) for r in rows],
    }


@app.get("/api/certificates/{cert_id}")
def get_certificate(
    cert_id: int,
    revision: int | None = Query(default=None, ge=0),
    conn=Depends(db.get_conn),
):
    cur = current_revision(conn)
    rev = cur if revision is None else revision
    if rev > cur:
        conn.rollback()
        raise HTTPException(
            status_code=404,
            detail={"error": "revision_not_found", "revision": rev},
        )
    cert = get_cert(conn, cert_id)
    if cert["created_revision"] > rev:
        raise HTTPException(
            status_code=404,
            detail={"error": "certificate_not_found_at_revision", "certificate_id": cert_id},
        )
    chain = load_cert_chain(conn, cert_id, rev)
    conn.commit()
    return {
        "revision": rev,
        "certificate": serialize_cert(cert, rev),
        "certificate_chain": [
            {
                "certificate_id": c["id"],
                "instrument_id": c["instrument_id"],
                "is_root": c["is_root"],
                "parent_id": c["parent_id"],
                "status": domain.cert_status(c, rev),
            }
            for c in chain
        ],
    }


@app.get("/api/measurements")
def list_measurements(
    instrument_id: str | None = None,
    revision: int | None = Query(default=None, ge=0),
    conn=Depends(db.get_conn),
):
    cur = current_revision(conn)
    rev = cur if revision is None else revision
    if rev > cur:
        conn.rollback()
        raise HTTPException(
            status_code=404,
            detail={"error": "revision_not_found", "revision": rev},
        )
    sql = "SELECT * FROM measurement WHERE created_revision <= %s"
    params: list[Any] = [rev]
    if instrument_id is not None:
        sql += " AND instrument_id = %s"
        params.append(instrument_id)
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()
    conn.commit()
    return {
        "revision": rev,
        "measurements": [
            serialize_measurement(r, rev) for r in rows
        ],
    }


@app.get("/api/measurements/{measurement_id}")
def get_measurement(
    measurement_id: int,
    revision: int | None = Query(default=None, ge=0),
    conn=Depends(db.get_conn),
):
    """按指定修订查询单条测量结论（历史结果保留）。"""
    cur = current_revision(conn)
    rev = cur if revision is None else revision
    if rev > cur:
        conn.rollback()
        raise HTTPException(
            status_code=404,
            detail={"error": "revision_not_found", "revision": rev},
        )
    meas = conn.execute(
        "SELECT * FROM measurement WHERE id = %s", (measurement_id,)
    ).fetchone()
    if meas is None or meas["created_revision"] > rev:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "measurement_not_found",
                "measurement_id": measurement_id,
            },
        )
    chain = load_cert_chain(conn, meas["certificate_id"], rev)
    cert = chain[0]
    conn.commit()
    return {
        "revision": rev,
        "measurement": serialize_measurement(meas, rev, cert, chain),
    }
