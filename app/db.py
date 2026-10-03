"""数据库连接池与建表脚本。"""
from __future__ import annotations

import os
import time

import psycopg
import psycopg_pool
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://calib:calib@localhost:5432/calib"
)

_pool: psycopg_pool.ConnectionPool | None = None


SCHEMA_SQL = """
-- 全局修订号：所有写请求原子推进，初始为 0
CREATE TABLE IF NOT EXISTS revision (
    id         INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    value      BIGINT NOT NULL DEFAULT 0
);
INSERT INTO revision (id, value) VALUES (1, 0) ON CONFLICT DO NOTHING;

CREATE EXTENSION IF NOT EXISTS btree_gist;

-- 器具证书
CREATE TABLE IF NOT EXISTS certificate (
    id                    BIGSERIAL PRIMARY KEY,
    instrument_id         TEXT NOT NULL,
    serial                TEXT,
    calibrated_at         TIMESTAMPTZ NOT NULL,   -- 校准时刻
    valid_from            TIMESTAMPTZ NOT NULL,   -- 半开有效期起点（含）
    valid_to              TIMESTAMPTZ NOT NULL,   -- 半开有效期终点（不含）
    error_value           DOUBLE PRECISION NOT NULL,
    tolerance             DOUBLE PRECISION NOT NULL,
    parent_id             BIGINT REFERENCES certificate(id),
    is_root               BOOLEAN NOT NULL,
    revoked_at            TIMESTAMPTZ,            -- 撤销生效时刻
    revoked_revision      BIGINT,                 -- 撤销发生时的修订号
    invalidated_revision  BIGINT,                 -- 因上级失效被整证递归判废的修订号
    created_revision      BIGINT NOT NULL,
    CONSTRAINT chk_half_open CHECK (valid_from < valid_to),
    CONSTRAINT chk_tolerance_nonneg CHECK (tolerance >= 0),
    CONSTRAINT chk_root_parent CHECK (
        (is_root AND parent_id IS NULL) OR (NOT is_root AND parent_id IS NOT NULL)
    )
);

-- 同一器具证书半开有效期 [valid_from, valid_to) 不得重叠
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'excl_no_overlap') THEN
        ALTER TABLE certificate
            ADD CONSTRAINT excl_no_overlap
            EXCLUDE USING gist (
                instrument_id WITH =,
                tstzrange(valid_from, valid_to, '[)') WITH &&
            );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_cert_parent ON certificate(parent_id);
CREATE INDEX IF NOT EXISTS idx_cert_instrument ON certificate(instrument_id);

-- 测量记录
CREATE TABLE IF NOT EXISTS measurement (
    id                    BIGSERIAL PRIMARY KEY,
    instrument_id         TEXT NOT NULL,
    value                 DOUBLE PRECISION NOT NULL,
    measured_at           TIMESTAMPTZ NOT NULL,
    certificate_id        BIGINT NOT NULL REFERENCES certificate(id),
    created_revision      BIGINT NOT NULL,
    invalidated_revision  BIGINT                  -- 转为无效的修订号；NULL 表示始终未失效
);

CREATE INDEX IF NOT EXISTS idx_meas_instrument ON measurement(instrument_id);
CREATE INDEX IF NOT EXISTS idx_meas_cert ON measurement(certificate_id);
"""


def init_pool() -> None:
    """启动连接池；数据库尚未就绪时重试等待。"""
    global _pool
    last_err: Exception | None = None
    for _ in range(60):
        try:
            _pool = psycopg_pool.ConnectionPool(
                DATABASE_URL,
                min_size=1,
                max_size=10,
                kwargs={"row_factory": dict_row},
                open=True,
            )
            with _pool.connection() as conn:
                conn.execute(SCHEMA_SQL)
                conn.commit()
            return
        except Exception as exc:  # 数据库容器可能还没起来
            last_err = exc
            time.sleep(1)
    raise RuntimeError(f"无法连接数据库: {last_err}")


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


from typing import Iterator


def get_conn() -> Iterator:
    """FastAPI 依赖：借出一条连接；正常退出自动提交，抛异常自动回滚并归还。"""
    assert _pool is not None
    with _pool.connection() as conn:
        yield conn
