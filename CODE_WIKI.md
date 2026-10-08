# 新闻快讯文本去重系统 (news-flash-dedup) — Code Wiki

> 版本说明：本 Wiki 基于代码库当前快照生成（2026-09-27）。系统处于「UAT 联调 / 待生产权限」阶段，环境边界严格限制为仅连 UAT 集群（见 §6 运行方式）。

---

## 1. 项目概述

`news-flash-dedup` 是一个**新闻快讯（News Flash）文本去重服务**：接收一条（或一批）快讯原文，判断它是否与已受理的历史快讯「重复 / 不重复 / 边界/疑难」，并把判定结果通过回调投递给上游调用方。

核心能力：

- **受理（Admission）**：以「单槽 / 有界日志」两种方式受理快讯，写入 ES 控制文档，支持幂等重放（`reused`）。
- **召回（Recall）**：多通道（精确哈希 / SimHash·MinHash 近重复 / BM25 / 实体 / 向量）召回候选历史记录。
- **比较 + 决策（Compare / Decide）**：事实级对齐 → 数值/时间槽比较 → 逐对精判 → 集合级聚合，产出结构化决策 `DecideOutcome`。
- **提交 + 持久化（Commit / Persist）**：审计前置 → CAS 写主记录 → 推进水位（watermark）。
- **投递（Delivery）**：带 lease/backoff 的可靠回调投递，失败可恢复。
- **金标准评估（Gold）**：旧标注迁移、冻结数据集、家族聚类，用于可复现评估。

技术栈：**Python 3.11+ / FastAPI / Elasticsearch 8.x / Milvus 2.6 / pydantic v2**。

---

## 2. 目录结构

```
news-flash-dedup/
├── src/news_flash_dedup/        # 主包（命名空间）
│   ├── admission.py             # 单槽受理协调器（G0）
│   ├── admission_schema.py      # 受理字段约束（pydantic）
│   ├── batch_admission.py       # 有界批量受理协调器
│   ├── batch_collector.py       # 批量受理有界等待队列
│   ├── batch_es_store.py        # 批量受理真 ES 仓储
│   ├── es_admission_index.py    # 受理索引管理
│   ├── es_admission_schema.py   # 受理 ES 映射/约束
│   ├── es_admission_store.py    # 受理真 ES Store
│   ├── g0_persistence.py        # G0 固定工件探针（P01 原型端到端）
│   ├── es_client.py             # ES 客户端工厂（仅 UAT）
│   ├── milvus_client.py         # Milvus 客户端工厂（仅测试）
│   ├── lifecycle.py             # 域日生命周期 / 索引清理
│   ├── api/                     # HTTP 入口 + 线协议合同
│   │   ├── http.py              # FastAPI create_app
│   │   ├── contract.py          # 提交/回调载荷合同
│   │   ├── cleanup.py           # 清理 API
│   │   └── recovery.py          # 恢复 API
│   ├── product/                 # 产品化入口
│   │   └── admission_port.py    # CollectorAdmissionPort
│   ├── recall/                  # 多通道召回
│   ├── compare/                 # 对齐/逐对精判/聚合
│   ├── decide/                  # 决策编排 + DTO + 审计
│   ├── facts/                   # 事实抽取与校验
│   ├── text/                    # 文本哈希/入桶闸门
│   ├── vector/                  # Milvus 向量写入
│   ├── persist/                 # 主记录/审计持久化
│   ├── delivery/                # 回调投递
│   ├── commit/                  # 提交终态协调
│   ├── gold/                    # 金标准/冻结数据集
│   └── metrics/                 # 指标（p23）
├── scripts/                     # 压测/探针脚本
├── tests/                       # unit + integration 测试
├── requirements.txt
├── .env.example
└── .env                         # 本地环境（未入库）
```

> 勘正（W2Fε，第二波审核）：本树原列 `│   ├── commit.py # commit 包 re-export`
> 一行——`src/news_flash_dedup/commit.py` 实物不存在（commit 仅 `commit/`
> 包，已列于上），系幽灵行，已删除。

---

## 3. 整体架构

系统采用**分层流水线**：接入 → 受理 → 召回 → 比较 → 决策 → 提交/持久化 → 投递，各层通过「协议（Protocol）+ 仓储（Store）+ 协调器（Coordinator）」解耦。真实实现（ES/Milvus/网络）与 `fake_store` 模拟实现可互换，支撑单元测试与 UAT。

```
                        ┌─────────────────────────────┐
  上游调用方 ──POST──▶  │  api/http.py (FastAPI)      │
  /v1/api/task/run      │  api/contract.py (提交合同)  │
                        └──────────────┬──────────────┘
                                       │ submit → AdmissionPort.accept
                        ┌──────────────▼──────────────┐
                        │ product/admission_port.py   │  CollectorAdmissionPort
                        └──────────────┬──────────────┘
                                       │
              ┌────────────────────────▼───────────────┐
              │ batch_collector.py (有界等待队列/批次聚合) │
              │ batch_admission.py (有界日志 CAS + 物化)  │
              │ admission.py (单槽受理/可重放物化)        │
              │ es_client.py / milvus_client.py (连接)    │
              └────────────────────────┬───────────────┘
                                       │ 受理凭据 → 异步流水线
        ┌─────────────┬─────────────────┼──────────────────┬─────────────┐
        ▼             ▼                 ▼                  ▼             ▼
  recall/        compare/           decide/            commit/       persist/
  多通道召回     对齐+逐对精判        决策编排+DTO        提交终态      主/审计记录
  fusion.py     aggregate.py       service.py          coordinator  es_store
                pair_compare.py    types.py            es_store     fake_store
                pair_alignment.py  audit.py
        │             │                 │                  │             │
        └─────────────┴────────┬────────┴──────────────────┴──────┬──────┘
                               ▼                                   ▼
                          vector/ (Milvus)                   delivery/ (回调)
                          milvus_store.py                    sender/dispatcher
                                                              ──▶ 上游回调
```

依赖方向：`api/product → admission → (recall/compare/decide/facts) → commit/persist/delivery/vector`。`api/contract.py` 是线协议唯一契约出口，`decide/types.py::DecideOutcome` 是去重决策的结构化结果，`commit/coordinator.py::commit_one` 是终态编排入口。

---

## 4. 主要模块职责

### 4.1 接入与合同层 (`api/`, `product/`)

| 文件 | 职责 |
| --- | --- |
| `api/http.py` | 可注入 HTTP 入口。`create_app(admission_port, context_provider, max_http_bytes)` 返回 FastAPI 应用，路由 `POST /v1/api/task/run`、`GET /health`；依赖缺失即拒绝启动。 |
| `api/contract.py` | 线协议合同：提交载荷解析（`parse_submission`）、发起（`submit`）、结果校验（`validate_result`）、回调编解码（`success_callback`/`failure_callback`）。不含网络路由、终态提交。 |
| `api/cleanup.py` / `api/recovery.py` | 索引清理 / 恢复 API 封装。 |
| `product/admission_port.py` | 产品化 `AdmissionPort`：把 `BatchAdmissionCollector.accept` 包装成合同层协议，并把异常分类映射为 `Backpressure/Unavailable/IdentityConflict` 等。 |

### 4.2 受理层 (`admission*.py`, `batch_*.py`)

| 文件 | 职责 |
| --- | --- |
| `admission.py` | **单槽受理** `AdmissionCoordinator`：ES 控制文档 CAS，产出可重放物化（主记录 `news-dedup-items-v1-YYYY.MM.DD` + `request/item/seq` 三份身份映射）。 |
| `admission_schema.py` | `PendingAdmissionV1` / `DeliveryAdmissionV1` pydantic 字段约束（strict + forbid extra）。 |
| `batch_admission.py` | **有界批量受理** `BatchAdmissionCoordinator`：有界多条日志、批次 CAS、异步物化、隔离/quarantine、复古（takeover）。 |
| `batch_collector.py` | **有界等待队列** `BatchAdmissionCollector`：接受单条请求，聚合为批次后交给协调器 CAS，批次确认后返回凭据。 |
| `batch_es_store.py` / `es_admission_*.py` | 受理层真 ES 仓储与索引/映射管理。 |

### 4.3 召回层 (`recall/`)

| 文件 | 职责 |
| --- | --- |
| `models.py` | 核心数据模型：`RecallRequest` / `RecallCandidate` / `ChannelResult`。 |
| `fusion.py` | 融合入口：汇总各通道、校验冲突、按记录 ID 去重，用 RRF 公式 `sum(1/(60+rank))` 排序并取 top30/top10，产出 `freeze_recall_plan`。 |
| `hash_channel.py` | 精确哈希召回：`prepare_recall_fields` 生成 raw/normalized hash，`HashChannel.search` 按 ES hash 查候选并校验一致性。 |
| `near_channel.py` | 近重复召回：SimHash/MinHash 指纹按 ES band 检索，`NearChannel.search` 返回 top40。 |
| `bm25_channel.py` | BM25 文本相关性召回：`BM25Channel.search` 分段查询，按 `_score` 排序 top40。 |
| `entity_channel.py` | 实体召回：基于 `entities.py::extract_body_entities` 抽取的实体（机构/证券代码）召回。 |
| `vector_space.py` / `vector_store.py` | 向量召回空间与向量存储抽象。 |
| `es_gateway.py` / `fingerprints.py` / `entities.py` | ES 网关、指纹构造、实体抽取。 |

### 4.4 事实层 (`facts/`)

| 文件 | 职责 |
| --- | --- |
| `schema.py` | 事实抽取输出 JSON Schema（`slot/evidence/fact/numeric/uncertainty`）。 |
| `core.py` | `FactValidationReport`、`FactIssue`、`NumericMention`，`scan_numeric_inventory()` 及 schema/evidence/slot 校验。 |
| `rule.py` | 确定性规则基线抽取（主体/极性/模态/引述/时间/关键对象），零第三方依赖，作为模型对照与回归地板。 |
| `llm.py` | LLM 抽取通道（模型对照实现）。 |

### 4.5 比较层 (`compare/`)

| 文件 | 职责 |
| --- | --- |
| `pair_alignment.py` | 事实级对齐：`build_aligned()` 按主体/谓词/关键对象三元组做确定性一对一配对，产出 `AlignedPair` / `FrozenRecallPlan`。 |
| `p15_integration.py` | 中级集成 `extract_p15_results()`：基于对齐抽取数值/时间槽并调用 P15 比较，颁发文本等价证书。 |
| `value_time.py` | `compare_numeric()` / `compare_time()`：归一化数值、时间关系比较，产出 verified conflict / equal / compatible / unresolved。 |
| `pair_compare.py` | 逐对精判 `compare_pair()`：综合冲突/未解/等价就绪状态，产出 `PairResult`（`equivalent/conflict/unresolved`）与 `PairIssue`。 |
| `aggregate.py` | 集合级聚合 `aggregate()`：`AggregateOutcome`、`FrozenRecallPlan`、`CoverageStatus`；产出最终 `decision/duplicate_ids/internal_code/reason`。 |

### 4.6 决策层 (`decide/`)

| 文件 | 职责 |
| --- | --- |
| `service.py` | 编排层：`decide_once()`（单对便捷包装）与 `decide_for_task()`（集合级：history ∪ candidates 逐对跑 P16-A→P15→P16-B→extra_event→aggregate）。 |
| `types.py` | `DecideOutcome` DTO（严格五字段公开序列化 `to_public_dict()`，白名单 `internal_code` 校验）。 |
| `audit.py` | `build_audit_record` / `build_audit_batch`：从 `PairResult` 构造审计批次。 |
| `extra_event.py` | `detect_extra_event()`：检测重合事件之外的独立事件（`RULE_UNCOVERED`）。 |

### 4.7 提交层 (`commit/`)

| 文件 | 职责 |
| --- | --- |
| `coordinator.py` | `commit_one()` 终态编排：水位校验 → `decide_for_task` → 审计前置 → CAS 写主记录 → 推进 `decision_watermark`。 |
| `__init__.py` | 公共类型：`CommitPlan/CommitResult/DecideResultStale/WatermarkSnapshot`，及 `is_watermark_complete`、`re_freeze_required`、`incremental_query_window`。 |
| `es_store.py` | 真 ES 提交仓储：审计批量 `create`、主记录 CAS、decision watermark 推进（含 UAT 环境闸门 `assert_p17_uat_open`）。 |
| `fake_store.py` | `FakeCommitStore` / `FakeMainRecord`（内存注入）。 |

### 4.8 持久化层 (`persist/`)

| 文件 | 职责 |
| --- | --- |
| `coordinator.py` | `persist_main_record`：计算 callback/hash → 构造主记录 → CAS 写主记录 → 推进 watermark。 |
| `es_store.py` | 真 ES 仓储：隔离索引创建、`write_audit_batch` 批量审计、主记录首建/CAS（保护投递状态不被覆盖）。 |
| `main_record_persist.py` / `audit_persist.py` | 主记录 / 审计记录持久化辅助。 |
| `fake_store.py` | `FakeAuditDoc` / `FakeMainRecordV1` / `FakeP18Store`。 |

### 4.9 投递层 (`delivery/`)

| 文件 | 职责 |
| --- | --- |
| `sender.py` | 仅 POST 回调：2xx + `succeed=true` → `delivered`；HTTP 错误 → `pending`；超时/断开 → `delivering` 等待续期。 |
| `coordinator.py` | 领取协调器：校验 payload/hash/route/轮次/截止，CAS 原子更新 lease，状态置 `delivering`。 |
| `dispatcher.py` / `es_store.py` / `fake_store.py` | 派发与仓储。 |

### 4.10 向量层 (`vector/`)

| 文件 | 职责 |
| --- | --- |
| `coordinator.py` | P19 协调器：按 chunk 调用 Milvus store `upsert`，返回写入向量行。 |
| `milvus_store.py` | 真 Milvus/ES 双写仓储：创建/校验 Milvus 集合与 ES 索引、Strong 一致读、`upsert_chunks` 含 ES 主记录身份对拍。 |
| `fake_store.py` | 内存模拟向量存储。 |

### 4.11 生命周期 / 清理 (`lifecycle.py`)

`CleanupTarget` / `CleanupAuditLog`、`candidate_indices_for_cleanup`（按 `today - retention_days` 反推）、`probe_targets_present`、`dry_run_cleanup`、`execute_cleanup`、`is_valid_cleanup_target`（硬正则闸门 `^news-dedup-(items|audits)-v1-\d{4}\.\d{2}\.\d{2}$`）。删除前输出「将删除清单」，每次删除记录索引名+日期+触发者。

### 4.12 金标准 / 评估 (`gold/`)

| 文件 | 职责 |
| --- | --- |
| `materials.py` | `MaterialFile/MaterialRow/NegativePair/MaterialCatalog`：读取历史标注 Excel、构造标注行、解析「重复/不重复/第二批」表结构。 |
| `legacy.py` | 冻结历史标注数据，建立来源关系，生成 legacy dataset manifest。 |
| `freeze.py` | `freeze_dataset()`：校验 replay/assignment/exclusion，生成带版本号 frozen manifest；记录人工 review/adjudication。 |
| `families.py` | 构建隔离家族图（历史组/相同 ID/相同正文/负对连接），产出 family/member/edge/risk。 |
| `artifacts.py` | 评估产物管理。 |

### 4.13 基础设施与指标

| 文件 | 职责 |
| --- | --- |
| `es_client.py` | ES 客户端工厂：`load_environment/assert_test_environment/build_uat_client/client_from_environment`；生产环境 fail-fast 拒绝（`ProductionAccessDenied`）。 |
| `milvus_client.py` | Milvus 客户端工厂：`build_test_milvus_client/collection_name_for_test/partition_name`；仅测试集群。 |
| `text/hash.py` | `TextHashes/compute_text_hashes`（原文+归一化双重 hash）、`is_indexable/bucket_key/bucket_key_if_indexable`（入桶闸门）。 |
| `metrics/p23_metrics.py` | P23 指标采集。 |
| `g0_persistence.py` | **G0 原型端到端**固定工件探针 `FixedArtifactProbe`：`capture_visibility → prepare_visibility → freeze → advance_watermark → claim/ack/recover_delivery`，展示完整受理→冻结→投递生命周期。 |

---

## 5. 关键类与函数

### 5.1 受理层

```python
# admission.py
class AdmissionRequest:  # 受理请求（frozen dataclass）
    scope_id, request_id, item_id, text, received_at,
    schema_version, pipeline_version, embedding_space_id,
    delivery_route_ref, trace_id

class AdmissionReceipt:  # 受理凭据
    ...business_date, arrival_seq, record_id, accepted_at, expires_at,
    reused: bool = False   # reused=True 表示幂等重放

class AdmissionCoordinator:
    def __init__(self, store, *, owner_id, owner_isolated, clock=None)
    def accept(self, request, *, materialize=True) -> AdmissionReceipt
    def recover(self) -> None
    def takeover(self) -> None
    def lookup(self, scope_id, request_id) -> AdmissionReceipt | None

# 异常：IdentityConflict / AdmissionConflict / AdmissionUnknown
```

### 5.2 批量受理

```python
# batch_admission.py
class AdmissionCapacityExceeded(AdmissionConflict): reason: str  # log_item_limit/batch_byte_limit/log_byte_limit
class BatchLimits: max_batch_items=8, max_batch_bytes=512KB, max_log_items=64, max_log_bytes=4MB
class BatchAdmissionCoordinator:
    def accept_batch(self, requests) -> list[AdmissionReceipt]
    def materialize_oldest(self) -> int
    def materialize_prefix(self) -> int
    def recover(self) -> int
    def reconcile(self, request) -> AdmissionReceipt
    def takeover(self) -> int

# batch_collector.py
class BatchAdmissionCollector:
    def accept(self, request) -> AdmissionReceipt
    def snapshot(self) -> CollectorSnapshot
    def close(self) -> None
```

### 5.3 召回层

```python
# recall/fusion.py
freeze_recall_plan(...)   # 主通道 [hash/near/bm25/embedding/entity]，RRF sum(1/(60+rank))，top30/top10
# recall/models.py
RewriteHistory/RecallRequest/RecallCandidate/ChannelResult
# 各通道
HashChannel.search(...) / NearChannel.search(...) / BM25Channel.search(...) / EntityChannel...
```

### 5.4 比较与决策

```python
# compare/pair_alignment.py
build_aligned(history_report, current_report, history_text, current_text, ...) -> PairAlignmentOutcome
# compare/p15_integration.py
extract_p15_results(...) -> P15Report
# compare/pair_compare.py
compare_pair(history_ctx, current_ctx, ..., alignment, pipeline_version, p15_results) -> PairResult
# compare/aggregate.py
aggregate(current, plan, pair_results, ..., new_text_issues, coverage) -> AggregateOutcome
class FrozenRecallPlan(version, required)  # required 按 arrival_seq/record_id 升序
class CoverageStatus(visible_seq, prepared_seq, complete)

# decide/service.py
decide_once(history, current, ...) -> DecideOutcome          # 单对便捷包装
decide_for_task(history, candidates, *, current=None, ...) -> DecideOutcome  # 集合级
# decide/types.py
class DecideOutcome(..., decision, duplicate_ids, reason, internal_code, pair_codes,
                    used_evidence, unresolved_fields, raw_hash, pipeline_version, pair_results)
    def to_public_dict(self) -> dict   # 恰五字段：item_id/text/decision/duplicate_ids/reason
```

**决策结果三值**：`重复` / `不重复` / `边界case/疑难case`。内部码白名单区分 `等价(EQUIVALENT)/冲突(CONFLICT)/未解(UNRESOLVED)`。

### 5.5 提交与持久化

```python
# commit/coordinator.py
commit_one(ctx: CommitContext, store, *, decision_watermark_seq=None,
           allow_audit_complete_false=False, audit_complete=True,
           coverage_complete=None) -> CommitOutcome
class CommitContext / CommitOutcome(state="committed"|"stale") / CommitOneError

# commit/__init__.py
CommitPlan / CommitResult / DecideResultStale / WatermarkSnapshot
is_watermark_complete(wm, arrival_seq) -> (bool, detail)   # 双水位必须覆盖 arrival_seq-1
re_freeze_required(history, raw_candidates) -> dict         # history ∪ candidates 升序冻结
incremental_query_window(arrival_seq, budget=200) -> (floor, seq)

# decide/audit.py
build_audit_record(pair_result, ...) / build_audit_batch(pair_results, audit_complete, index_prefix)
```

### 5.6 合同层

```python
# api/contract.py
parse_submission(value, *, max_text_codepoints, max_http_bytes, raw_http_body) -> Submission
submit(value, context, port, *, raw_http_body) -> HttpResponse
validate_result(value, current, audit_lookup) -> dict
success_callback(value, current, audit_lookup, *, request_id, trace_id, completed_time) -> CallbackBytes
failure_callback(*, request_id, trace_id, completed_time, code, allowed_codes, message, detail)
ack_succeeded(status, body) -> bool
# 异常：ContractError / BodyTooLarge / Backpressure / Unavailable
```

### 5.7 基础设施

```python
# es_client.py
load_environment(dotenv_path=None) / assert_test_environment(env=None)
build_uat_client(env=None, *, request_timeout=5, max_retries=0, retry_on_timeout=False)
client_from_environment(load_dotenv_first=True)

# milvus_client.py
assert_test_milvus_environment(env=None) / build_test_milvus_client(env=None, *, client_class=None)
collection_name_for_test(run_id, space_id) / partition_name(business_date)

# text/hash.py
compute_text_hashes(...) -> TextHashes / is_indexable(...) / bucket_key(...)

# lifecycle.py
candidate_indices_for_cleanup(today, *, retention_days=7, kinds, index_prefix)
dry_run_cleanup(client, today, audit, ...) / execute_cleanup(client, today, audit, ...)
is_valid_cleanup_target(index_name)  # 硬正则闸门
```

---

## 6. 项目运行方式

### 6.1 依赖安装

```bash
# 建议使用 Python 3.11+（仓库已有 .venv-v1 虚拟环境）
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

主要依赖：`fastapi`、`uvicorn`、`pydantic>=2`、`elasticsearch==8.19.3`、`pymilvus==2.6.13`、`httpx`、`python-dotenv`、`openpyxl`、`pytest`、`scipy`。

### 6.2 环境配置（.env）

复制 `.env.example` 为 `.env` 并填写。**当前阶段强制 UAT-only**：

```ini
DEPLOY_ENV=test
TEST_ES_HOST=es-cn-9fr4srbma0001lus6.elasticsearch.aliyuncs.com
TEST_ES_PORT=9200
TEST_ES_USER=...
TEST_ES_PASS=...
TEST_MILVUS_HOST=c-75641abe71c37c6f-internal.milvus.aliyuncs.com
TEST_MILVUS_PORT=19530
TEST_MILVUS_USER=...
TEST_MILVUS_PASS=...
ALLOW_PROD_WRITE=false
```

> 安全约束：`assert_test_environment()` 会 fail-fast 拒绝任何生产迹象（`DEPLOY_ENV!=test`、设置 `PROD_ES_HOST`、`ALLOW_PROD_WRITE=true`、或 ES/Milvus 端点非批准集群）。

### 6.3 启动 HTTP 服务

`api/http.py::create_app` 是唯一入口，不提供匿名 scope 或默认回调路由，需宿主显式注入 `admission_port`、`context_provider`、`max_http_bytes`：

```python
from news_flash_dedup.api.http import create_app
# 负载来自你的宿主装配：构建 CollectorAdmissionPort 等依赖后
app = create_app(admission_port=port, context_provider=my_context, max_http_bytes=1_000_000)
# uvicorn app.main:app --host 0.0.0.0 --port 8000
```

路由：`POST /v1/api/task/run`（受理）、`GET /health`（存活探针）。

### 6.4 运行测试

```bash
pytest tests/unit                 # 单元测试（fake store，无网络）
pytest tests/integration          # 集成/UAT（连 UAT ES/Milvus，需 .env）
pytest -k "test_name"             # 按名称筛选
```

测试组织：`tests/unit/` 覆盖各模块（受理/召回/比较/决策/commit/清理/gold 等）；`tests/integration/` 覆盖 UAT 端到端及跨日/召回/清理等证据脚本。

### 6.5 辅助脚本

```bash
python scripts/bench_admission.py         # 单条受理压测
python scripts/bench_batch_admission.py   # 批量受理压测
python scripts/probe_g0_recovery.py       # G0 恢复探针
```

---

## 7. 依赖关系总览

- **协议/契约**：`api/contract.py` 是全系统线协议权威，`api/http.py` 只做传输适配；`product/admission_port.py` 把受理异常分类映射回合同层语义。
- **受理 → 下游**：`AdmissionReceipt`（含 `arrival_seq`、`business_date` 域日）是全流水线的身份凭据；`decide_for_task` 消费受理侧冻结的历史候选。
- **召回 → 比较**：`FrozenRecallPlan.required` 是 `aggregate` 的输入（L01/L02/L08 不变量基础：aggregate 必须同时见全部 pair_results + 完整 required 集）。
- **比较 → 决策**：`PairResult` 汇总为 `DecideOutcome.pair_results`，供 `commit_one` 构造审计批次。
- **决策 → 提交**：`commit_one` 以 `build_audit_batch(decide.pair_results)` 落库，CAS 写主记录后推进 `decision_watermark`。
- **提交 → 投递**：主记录冻结出 `delivery_lease` 与 `callback_body`，由 `delivery/sender.py` 带 backoff 投递、`g0_persistence.py::FixedArtifactProbe` 展示完整生命周期闭环。
- **环境边界**：`es_client.py` / `milvus_client.py` 是唯一合法连接出站口，统一执行 UAT-only + 生产 fail-fast 防线。

---

## 8. 核心设计要点

1. **确定性 ID / 幂等重放**：`record_id = sha256([scope_id, item_id])`（JCS 规范化 JSON + SHA-256），文本指纹 `sha256({item_id, text})`；同身份重放返回 `reused=True`，跨语言确定性收敛。
2. **域日（Business Date）+ 水位（Watermark）**：`Asia/Shanghai` 时区按受理日生成 `business_date`（`YYYY-MM-DD`），物化索引按日切分 `news-dedup-items-v1-YYYY.MM.DD`；`arrival_seq` 全局单调，`decision_watermark`/`lexical_watermark` 保证增量屏障（`is_watermark_complete` 要求覆盖 `arrival_seq-1`）。
3. **单活写隔离（Single-Writer）**：`owner_isolated()` 回调 + CAS（`if_seq_no/if_primary_term`）保证旧写者隔离；`takeover()`/`recover()` 支持所有权转移与崩溃恢复，全程「未确定写」显式为 `AdmissionUnknown`。
4. **逻辑过期（Logical Expiry）**：受理凭据带 `expires_at`（受理日 +7 天），所有业务读写在每个 IO 前后反复过 `_live()` 闸门，过期即拒绝，不产生「过期后写入」。
5. **有界 + 异步物化**：`batch_admission` 以有界日志（item/byte 上限）承载在途受理，`materialize_prefix` 批量物化并一次 CAS 收缩前缀，quarantine 机制隔离永久性/确定性冲突。
6. **多层去重判定**：哈希/近重复/BM25/实体/向量五通道召回 → 事实级对齐 → 数值时间槽比较 → 逐对精判 → 集合聚合，最终三值决策；`DecideOutcome.to_public_dict()` 严格五字段封闭，不泄露内部码。
7. **合规与安全**：清理走硬正则闸门 + dry-run + 审计日志；合同层 `safe_log_fields()` 只记录白名单字段；`failure_callback` 校验码/防 CRLF 注入。

---

*本文档由源码分析自动生成，覆盖整体架构、模块职责、关键类函数、依赖关系与运行方式。如需细化某个子包的时序/字段级说明，可进一步展开。*