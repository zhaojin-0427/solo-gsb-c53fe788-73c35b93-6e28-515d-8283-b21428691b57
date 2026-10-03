# 校准链失效追溯 API

基于 **Python 3.11 + FastAPI + PostgreSQL 16** 实现的校准证书链登记、测量绑定与撤销追溯服务。

- 器具证书记录**校准时刻**、**半开有效期 `[valid_from, valid_to)`**（起点必须早于终点）、误差值与非负容差
- **根证书**作为信任锚；所有证书须满足 `|error_value| ≤ tolerance`
- 非根证书必须引用一个**在其校准时刻仍然有效**的上级证书
- **同一器具**的证书有效期不得重叠（数据库 GiST 排他约束强保证，半开区间端点相接不算重叠）
- 登记测量（数值 + 发生时刻）时绑定覆盖该时刻的有效证书；**缺证书或已失效一律拒绝**
- 证书可从其有效期内指定时刻起**撤销**：既有测量随之重判；子证书仅当其**校准时刻落入上级失效区间**时递归整证失效
- 撤销响应列出本次转为无效的测量及其**失效因果链**：按每级子证书的**校准时刻**逐级向上追溯，仅保留对本次失效有因果贡献的环节——上级**晚撤销**（失效起点晚于下级校准时刻）不计入，链尾即**根因证书**（自身撤销或递归判废的最近一级）
- 所有写请求携带 `expected_revision`，在单事务内原子推进**全局修订号**；过期返回 **409**
- 查询接口支持 `?revision=N`，**任意历史修订下的测量结论均保留**

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由、事务与级联重判逻辑
│   ├── db.py          # 连接池与建表脚本（含排他约束）
│   └── domain.py      # 按修订计算证书状态 / 失效区间 / 序列化
├── scripts/
│   ├── demo.py            # 基础流程演示（生成 README 样例）
│   └── demo_recursive.py  # 递归失效演示
├── tests_e2e.py       # 端到端业务规则测试（104 项断言）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

首次启动会自动建表（含 `btree_gist` 扩展与排他约束）。

访问地址：

| 用途 | 地址 |
| --- | --- |
| API 根路径 | http://localhost:8000/ |
| 健康检查 | http://localhost:8000/health |
| 当前全局修订号 | http://localhost:8000/api/revision |
| Swagger 交互文档 | http://localhost:8000/docs |
| OpenAPI Schema | http://localhost:8000/openapi.json |

停止并保留数据：`docker compose stop`；清空数据重来：`docker compose down -v`。

### 快速验证：撤销接口（curl）

服务启动后，可按下面的顺序实际调用撤销接口，验证失效因果链行为（时间均为 UTC）：

```bash
# 1) 登记根证书 R（信任锚）
curl -sX POST http://localhost:8000/api/certificates -H 'Content-Type: application/json' -d '{
  "expected_revision": 0, "instrument_id": "STD-001", "is_root": true,
  "calibrated_at": "2026-01-01T00:00:00Z", "valid_from": "2026-01-01T00:00:00Z",
  "valid_to": "2027-01-01T00:00:00Z", "error_value": 0.0, "tolerance": 0.5}'

# 2) 登记子证书 C（parent_id=1，校准时刻 2026-02-01 在 R 有效期内）
curl -sX POST http://localhost:8000/api/certificates -H 'Content-Type: application/json' -d '{
  "expected_revision": 1, "instrument_id": "DMM-7788", "parent_id": 1,
  "calibrated_at": "2026-02-01T00:00:00Z", "valid_from": "2026-02-01T00:00:00Z",
  "valid_to": "2026-08-01T00:00:00Z", "error_value": 0.1, "tolerance": 0.2}'

# 3) 登记一条绑定 C 的测量（id=1）
curl -sX POST http://localhost:8000/api/measurements -H 'Content-Type: application/json' -d '{
  "expected_revision": 2, "instrument_id": "DMM-7788",
  "value": 5.018, "measured_at": "2026-07-15T08:00:00Z"}'

# 4) 先在 C 校准时刻之后撤销 R：C 不判废，无测量失效
curl -sX POST http://localhost:8000/api/certificates/1/revoke -H 'Content-Type: application/json' -d '{
  "expected_revision": 3, "revoked_at": "2026-02-15T00:00:00Z"}'
#   -> invalidated_measurement_count = 0

# 5) 再撤销 C：测量因 C 自身撤销首次失效
curl -sX POST http://localhost:8000/api/certificates/2/revoke -H 'Content-Type: application/json' -d '{
  "expected_revision": 4, "revoked_at": "2026-05-01T00:00:00Z"}'
#   -> invalidated_measurements[0].failure_chain 仅含 C（id=2），
#      root_cause_certificate_id = 2；R 晚撤销，不误列入链

# 6) 历史修订查询：撤销前的结论保留（测量 1 在修订 4 时仍 valid=true）
curl -s 'http://localhost:8000/api/measurements/1?revision=4'
```

各步 `expected_revision` 需与当前全局修订号一致（可用 `curl -s http://localhost:8000/api/revision` 查询），过期返回 409。

### 本地非 Docker 运行（可选）

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://calib:calib@localhost:5432/calib
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/api/revision` | 当前全局修订号 |
| POST | `/api/certificates` | 登记证书（写，需 `expected_revision`） |
| POST | `/api/certificates/{id}/revoke` | 撤销证书并级联重判（写） |
| GET | `/api/certificates` | 证书列表，可按 `instrument_id`、`revision` 过滤 |
| GET | `/api/certificates/{id}` | 证书详情 + 证书链 |
| POST | `/api/measurements` | 登记测量（写；不传 `certificate_id` 则自动绑定） |
| GET | `/api/measurements` | 测量结论列表，可按 `instrument_id`、`revision` 过滤 |
| GET | `/api/measurements/{id}` | 单条测量在指定修订下的结论 |

写请求的时间字段均为 ISO 8601（建议带 `Z` 时区；不带时区按 UTC 处理）。

### 证书状态语义

- `valid`：当前有效
- `revoked`：已撤销，失效区间为 `[revoked_at, valid_to)`；撤销时刻之前产生的结论仍有效
- `invalid`：校准时刻落入上级失效区间而被**递归整证判废**，整条有效期均不可信

### 失效因果链与根因判定

撤销接口响应中的 `invalidated_measurements[*].failure_chain` 不是"链上当前所有已失效证书"的简单罗列，而是**按每级子证书的校准时刻逐级追溯**出的因果链：

- 从测量绑定的证书开始，若本级已失效（`revoked` / `invalid`）则计入链中；
- 仅当**本级校准时刻落入上级失效区间**时，上级失效才是本级失效的原因，继续向上追溯；
- 否则本级系**自身撤销**导致失效——上级即使后来也撤销（**晚撤销**），与本次失效无因果，**不计入链中**；
- 链尾（`root_cause_certificate_id`）即根因证书：自身撤销或递归判废的最近一级。

示例：根证书 R、子证书 C（在 R 有效时校准）、一条绑定 C 的测量。先在 C 校准时刻**之后**撤销 R（C 不判废、测量不失效），再撤销 C——该测量因 **C 自身撤销**才首次失效，故失效链为 `[C]`、根因为 C，R 不出现在链中。反之若 R 的失效区间覆盖了 C 的校准时刻，C 被递归判废，失效链为 `[C, R]`、根因为 R（见下文"递归整证失效"样例）。

## 实际请求 / 响应样例

以下样例均由 `scripts/demo.py` 对运行中的服务真实调用产生（数据库重置后执行，id 从 1 开始）。

### 1. 健康检查

```
GET /health
```
```json
{
  "status": "ok",
  "revision": 0
}
```

### 2. 登记根证书（信任锚）

```
POST /api/certificates
```
```json
{
  "expected_revision": 0,
  "instrument_id": "STD-001",
  "serial": "STD-SN-0001",
  "calibrated_at": "2026-01-01T00:00:00Z",
  "valid_from": "2026-01-01T00:00:00Z",
  "valid_to": "2027-01-01T00:00:00Z",
  "error_value": -0.05,
  "tolerance": 0.5,
  "is_root": true
}
```
```json
{
  "revision": 1,
  "certificate": {
    "id": 1,
    "instrument_id": "STD-001",
    "serial": "STD-SN-0001",
    "calibrated_at": "2026-01-01T00:00:00Z",
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to": "2027-01-01T00:00:00Z",
    "error_value": -0.05,
    "tolerance": 0.5,
    "parent_id": null,
    "is_root": true,
    "revoked_at": null,
    "revoked_revision": null,
    "invalidated_revision": null,
    "created_revision": 1,
    "status": "valid",
    "failure_window": null
  },
  "certificate_chain": [
    {
      "certificate_id": 1,
      "instrument_id": "STD-001",
      "is_root": true,
      "parent_id": null,
      "status": "valid"
    }
  ]
}
```

### 3. 登记下级证书（校准时刻 2026-02-01 落在上级有效期内）

```
POST /api/certificates
```
```json
{
  "expected_revision": 1,
  "instrument_id": "DMM-7788",
  "serial": "DMM-SN-7788",
  "calibrated_at": "2026-02-01T00:00:00Z",
  "valid_from": "2026-02-01T00:00:00Z",
  "valid_to": "2026-08-01T00:00:00Z",
  "error_value": 0.1,
  "tolerance": 0.2,
  "parent_id": 1
}
```
```json
{
  "revision": 2,
  "certificate": {
    "id": 2,
    "instrument_id": "DMM-7788",
    "serial": "DMM-SN-7788",
    "calibrated_at": "2026-02-01T00:00:00Z",
    "valid_from": "2026-02-01T00:00:00Z",
    "valid_to": "2026-08-01T00:00:00Z",
    "error_value": 0.1,
    "tolerance": 0.2,
    "parent_id": 1,
    "is_root": false,
    "revoked_at": null,
    "revoked_revision": null,
    "invalidated_revision": null,
    "created_revision": 2,
    "status": "valid",
    "failure_window": null
  },
  "certificate_chain": [
    {
      "certificate_id": 2,
      "instrument_id": "DMM-7788",
      "is_root": false,
      "parent_id": 1,
      "status": "valid"
    },
    {
      "certificate_id": 1,
      "instrument_id": "STD-001",
      "is_root": true,
      "parent_id": null,
      "status": "valid"
    }
  ]
}
```

### 4. 登记测量（不传 certificate_id，自动绑定覆盖时刻的证书）

```
POST /api/measurements
```
```json
{
  "expected_revision": 2,
  "instrument_id": "DMM-7788",
  "value": 5.012,
  "measured_at": "2026-03-10T08:00:00Z"
}
```
```json
{
  "revision": 3,
  "measurement": {
    "id": 1,
    "instrument_id": "DMM-7788",
    "value": 5.012,
    "measured_at": "2026-03-10T08:00:00Z",
    "certificate_id": 2,
    "created_revision": 3,
    "invalidated_revision": null,
    "revision": 3,
    "valid": true,
    "certificate_status": "valid",
    "certificate_chain": [
      {
        "certificate_id": 2,
        "instrument_id": "DMM-7788",
        "is_root": false,
        "parent_id": 1,
        "status": "valid"
      },
      {
        "certificate_id": 1,
        "instrument_id": "STD-001",
        "is_root": true,
        "parent_id": null,
        "status": "valid"
      }
    ]
  }
}
```

再登记一条落在证书有效期**后部**的测量（之后会被撤销重判）：

```json
{
  "expected_revision": 3,
  "instrument_id": "DMM-7788",
  "value": 5.018,
  "measured_at": "2026-07-15T08:00:00Z"
}
```
返回 `201`，测量 `id=2`、`revision=4`、`valid=true`。

### 5. 过期的 expected_revision → 409 Conflict

当前修订号已推进到 4，仍用旧值提交：

```
POST /api/certificates
```
```json
{
  "expected_revision": 0,
  "instrument_id": "X",
  "calibrated_at": "2026-01-01T00:00:00Z",
  "valid_from": "2026-01-01T00:00:00Z",
  "valid_to": "2026-02-01T00:00:00Z",
  "error_value": 0.0,
  "tolerance": 0.1,
  "is_root": true
}
```
```
HTTP 409
```
```json
{
  "detail": {
    "error": "revision_conflict",
    "message": "expected_revision 已过期",
    "expected_revision": 0,
    "current_revision": 4
  }
}
```

冲突时事务回滚，修订号**不会**被推进。

### 6. 撤销证书并级联重判

自 `2026-05-01T00:00:00Z` 起撤销证书 2。该时刻位于证书有效期 `[2026-02-01, 2026-08-01)` 内：

```
POST /api/certificates/2/revoke
```
```json
{
  "expected_revision": 4,
  "revoked_at": "2026-05-01T00:00:00Z"
}
```
```json
{
  "revision": 5,
  "revoked_certificate": {
    "id": 2,
    "instrument_id": "DMM-7788",
    "serial": "DMM-SN-7788",
    "calibrated_at": "2026-02-01T00:00:00Z",
    "valid_from": "2026-02-01T00:00:00Z",
    "valid_to": "2026-08-01T00:00:00Z",
    "error_value": 0.1,
    "tolerance": 0.2,
    "parent_id": 1,
    "is_root": false,
    "revoked_at": "2026-05-01T00:00:00Z",
    "revoked_revision": 5,
    "invalidated_revision": null,
    "created_revision": 2,
    "status": "revoked",
    "failure_window": {
      "from": "2026-05-01T00:00:00Z",
      "to": "2026-08-01T00:00:00Z"
    }
  },
  "invalidated_certificates": [],
  "invalidated_measurements": [
    {
      "measurement": {
        "id": 2,
        "instrument_id": "DMM-7788",
        "value": 5.018,
        "measured_at": "2026-07-15T08:00:00Z",
        "certificate_id": 2,
        "created_revision": 4,
        "invalidated_revision": 5,
        "revision": 5,
        "valid": false,
        "certificate_status": "revoked",
        "certificate_chain": [
          {
            "certificate_id": 2,
            "instrument_id": "DMM-7788",
            "is_root": false,
            "parent_id": 1,
            "status": "revoked"
          },
          {
            "certificate_id": 1,
            "instrument_id": "STD-001",
            "is_root": true,
            "parent_id": null,
            "status": "valid"
          }
        ]
      },
      "failure_chain": [
        {
          "certificate_id": 2,
          "instrument_id": "DMM-7788",
          "is_root": false,
          "parent_id": 1,
          "status": "revoked"
        }
      ],
      "root_cause_certificate_id": 2
    }
  ],
  "invalidated_measurement_count": 1
}
```

要点：测量 1 发生在撤销时刻之前，**仍然有效**（`invalidated_revision` 保持 `null`）；测量 2 落入失效区间 `[revoked_at, valid_to)`，被重判为无效并出现在 `invalidated_measurements` 中，同时给出失效链与根因证书。本例无子证书的校准时刻落入失效区间，因此 `invalidated_certificates` 为空。

### 7. 按历史修订查询（历史结论保留）

撤销发生在修订 5。按修订 4 查询测量 2，仍返回撤销前的结论 `valid=true`，且链上各证书显示为当时的 `valid` 状态：

```
GET /api/measurements/2?revision=4
```
```json
{
  "revision": 4,
  "measurement": {
    "id": 2,
    "instrument_id": "DMM-7788",
    "value": 5.018,
    "measured_at": "2026-07-15T08:00:00Z",
    "certificate_id": 2,
    "created_revision": 4,
    "invalidated_revision": 5,
    "revision": 4,
    "valid": true,
    "certificate_status": "valid",
    "certificate_chain": [
      {
        "certificate_id": 2,
        "instrument_id": "DMM-7788",
        "is_root": false,
        "parent_id": 1,
        "status": "valid"
      },
      {
        "certificate_id": 1,
        "instrument_id": "STD-001",
        "is_root": true,
        "parent_id": null,
        "status": "valid"
      }
    ]
  }
}
```

不带 `revision` 时返回当前修订下的结论（测量 2 为 `valid=false`）。查询尚不存在的未来修订返回 `404 revision_not_found`。

### 8. 证书失效后登记测量 → 422 拒绝

```
POST /api/measurements
```
```json
{
  "expected_revision": 5,
  "instrument_id": "DMM-7788",
  "value": 9.9,
  "measured_at": "2026-05-15T00:00:00Z"
}
```
```
HTTP 422
```
```json
{
  "detail": {
    "error": "certificate_expired",
    "message": "覆盖该时刻的证书在测量时刻已撤销失效，登记被拒绝",
    "certificate_id": 2,
    "revoked_at": "2026-05-01T00:00:00Z"
  }
}
```

若不存在任何覆盖该时刻的证书，则返回 `error=no_certificate`；若覆盖证书已被递归判废，则返回 `error=certificate_invalid`。

### 9. 递归整证失效（撤销根证书）

由 `scripts/demo_recursive.py` 真实调用产生：三级链 `STD-R(根) → DMM-R → DUT-R`，各级校准时刻分别为 2026-01-01 / 02-01 / 03-01，并有一条发生于 2026-04-01 的测量绑定在孙证书上。自 **2026-01-15** 起撤销根证书：

```
POST /api/certificates/1/revoke
```
```json
{
  "expected_revision": 4,
  "revoked_at": "2026-01-15T00:00:00Z"
}
```
```json
{
  "revision": 5,
  "revoked_certificate": {
    "id": 1,
    "instrument_id": "STD-R",
    "calibrated_at": "2026-01-01T00:00:00Z",
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to": "2027-01-01T00:00:00Z",
    "error_value": 0.0,
    "tolerance": 0.5,
    "parent_id": null,
    "is_root": true,
    "revoked_at": "2026-01-15T00:00:00Z",
    "revoked_revision": 5,
    "invalidated_revision": null,
    "created_revision": 1,
    "status": "revoked",
    "failure_window": {
      "from": "2026-01-15T00:00:00Z",
      "to": "2027-01-01T00:00:00Z"
    }
  },
  "invalidated_certificates": [
    {
      "id": 2,
      "instrument_id": "DMM-R",
      "calibrated_at": "2026-02-01T00:00:00Z",
      "valid_from": "2026-02-01T00:00:00Z",
      "valid_to": "2026-12-01T00:00:00Z",
      "error_value": 0.0,
      "tolerance": 0.2,
      "parent_id": 1,
      "is_root": false,
      "revoked_at": null,
      "revoked_revision": null,
      "invalidated_revision": 5,
      "created_revision": 2,
      "status": "invalid",
      "failure_window": {
        "from": "2026-02-01T00:00:00Z",
        "to": "2026-12-01T00:00:00Z"
      }
    },
    {
      "id": 3,
      "instrument_id": "DUT-R",
      "calibrated_at": "2026-03-01T00:00:00Z",
      "valid_from": "2026-03-01T00:00:00Z",
      "valid_to": "2026-11-01T00:00:00Z",
      "error_value": 0.0,
      "tolerance": 0.1,
      "parent_id": 2,
      "is_root": false,
      "revoked_at": null,
      "revoked_revision": null,
      "invalidated_revision": 5,
      "created_revision": 3,
      "status": "invalid",
      "failure_window": {
        "from": "2026-03-01T00:00:00Z",
        "to": "2026-11-01T00:00:00Z"
      }
    }
  ],
  "invalidated_measurements": [
    {
      "measurement": {
        "id": 1,
        "instrument_id": "DUT-R",
        "value": 12.34,
        "measured_at": "2026-04-01T00:00:00Z",
        "certificate_id": 3,
        "created_revision": 4,
        "invalidated_revision": 5,
        "revision": 5,
        "valid": false,
        "certificate_status": "invalid",
        "certificate_chain": [
          {
            "certificate_id": 3,
            "instrument_id": "DUT-R",
            "is_root": false,
            "parent_id": 2,
            "status": "invalid"
          },
          {
            "certificate_id": 2,
            "instrument_id": "DMM-R",
            "is_root": false,
            "parent_id": 1,
            "status": "invalid"
          },
          {
            "certificate_id": 1,
            "instrument_id": "STD-R",
            "is_root": true,
            "parent_id": null,
            "status": "revoked"
          }
        ]
      },
      "failure_chain": [
        {
          "certificate_id": 3,
          "instrument_id": "DUT-R",
          "is_root": false,
          "parent_id": 2,
          "status": "invalid"
        },
        {
          "certificate_id": 2,
          "instrument_id": "DMM-R",
          "is_root": false,
          "parent_id": 1,
          "status": "invalid"
        },
        {
          "certificate_id": 1,
          "instrument_id": "STD-R",
          "is_root": true,
          "parent_id": null,
          "status": "revoked"
        }
      ],
      "root_cause_certificate_id": 1
    }
  ],
  "invalidated_measurement_count": 1
}
```

传播规则：子证书 2 的校准时刻 02-01 落入根证书失效区间 `[01-15, 明年01-01)`，故**整证失效**，其失效区间变为整条有效期 `[2026-02-01, 2026-12-01)`；孙证书 3 的校准时刻 03-01 又落入该区间，递归判废。若子证书校准时刻早于上级失效起点（如根 02-15 才撤销、子证书 02-01 校准），则子证书不判废、其子树也不传播。

### 10. 其他常见错误（均为 HTTP 422，请求体结构问题为 FastAPI 标准 422）

| 场景 | `detail.error` |
| --- | --- |
| `|error_value| > tolerance` | `tolerance_exceeded` |
| `valid_from >= valid_to` | `invalid_validity_window` |
| 非根证书未给 `parent_id` | `non_root_requires_parent` |
| 根证书携带 `parent_id` | `root_must_have_no_parent` |
| 上级在校准时刻已超出有效期 / 已撤销 / 已判废 | `parent_not_effective_at_calibration` / `parent_invalid` |
| 同一器具证书有效期重叠 | `validity_overlap` |
| 指定证书不属于该器具 | `certificate_instrument_mismatch` |
| 指定证书有效期不覆盖测量时刻 | `certificate_not_covering` |
| 撤销时刻不在证书有效期内 | `revocation_outside_validity` |
| 撤销已撤销 / 已判废证书 | `certificate_not_revocable` |

## 并发性与一致性说明

- 每个写请求是一个数据库事务：先对全局修订行 `SELECT ... FOR UPDATE` 加锁并比对 `expected_revision`，再做全部校验与写入，最后 `UPDATE revision SET value = value + 1` 提交。校验失败或约束冲突整体回滚，修订号不外溢。
- 同一器具证书不重叠由 PostgreSQL 的 `EXCLUDE USING gist (instrument_id WITH =, tstzrange(...) WITH &&)` 约束兜底。
- 历史结论不是快照表，而是由写入时刻（`created_revision`）、撤销时刻（`revoked_at` / `revoked_revision`）、判废修订（`invalidated_revision`）在查询时按目标修订推导，因此任意修订都能复现当时结论。

## 运行测试

端到端测试覆盖上述全部规则（半开区间边界、容差、重叠排他、自动/显式绑定、撤销截止边界、递归传播、混合传播、撤销因果追溯、409、历史修订），共 104 项断言：

```bash
# 需要一个可连接的 PostgreSQL，并设置 DATABASE_URL
python3 tests_e2e.py
```

复现 README 样例（服务启动后）：

```bash
python3 scripts/demo.py
python3 scripts/demo_recursive.py
```
