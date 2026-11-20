# 深度登记 · 节目载入编排台

- 规模：中型
- 使用者：剧场值班员按剧场排期批准载入日执行单。
- 核心规则：依赖图先验，逐工单找舞台、剧场和流动技师的共同空段。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 演出工单 | 逐行采信与冲突隔离 | `GET /api/samples/{sample}/orders` | `app/modules/orders/intake.py` | `tests/test_orders.py` |
| M2 工序依赖 | 查环并检查缺失前置 | `GET /api/samples/{sample}/dependencies` | `app/modules/dependencies/checks.py` | `tests/test_dependencies.py` |
| M3 工种日历 | 核对有效开工交集 | `GET /api/samples/{sample}/calendars` | `app/modules/calendars/registry.py` | `tests/test_calendars.py` |
| M4 工位冲突 | 定位重叠预约与未知资源 | `GET /api/samples/{sample}/conflicts` | `app/modules/conflicts/occupancy.py` | `tests/test_conflicts.py` |
| M5 转场排程 | 按工种、工位和预约找空段 | `GET /api/samples/{sample}/schedule` | `app/modules/schedule/planner.py` | `tests/test_schedule.py` |
| M6 超时勾稽 | 复核工单覆盖及资源碰撞 | `GET /api/samples/{sample}/reconcile` | `app/modules/reconcile/audit.py` | `tests/test_reconcile.py` |
| M7 执行清单 | 归档各工位执行顺序 | `GET /api/samples/{sample}/dispatch` | `app/modules/dispatch/checklist.py` | `tests/test_dispatch.py` |
| M8 修订联单 | 差异定位、受影响面锁定重排、核对后原子发布 | `POST /api/runs/{id}/revisions` | `app/modules/revision/service.py` | `tests/test_revision.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 带 BOM 的工单 | 首行解码失败 | 文件首字符清理后保留行号 | `app/modules/orders/intake.py` | `tests/test_orders.py::test_bom_and_source_line` |
| TRAP-07 | 时长为未知 | 误当零分钟 | 无效行留下源值 | `app/modules/orders/contract.py` | `tests/test_orders.py::test_sentinel_refused` |
| TRAP-14 | 同号时长不同 | 后写覆盖 | 同号两行隔离 | `app/modules/orders/intake.py` | `tests/test_orders.py::test_conflicting_rollback` |
| TRAP-16 | 工单尾行截断 | 整个导入中断 | 保留截断物理行，继续采信 | `app/modules/orders/intake.py` | `tests/test_orders.py::test_truncated_record` |
| TRAP-18 | 空文本或空行 | 误作正常工单 | 按物理行分类 | `app/modules/orders/intake.py` | `tests/test_orders.py::test_blank_distinguished` |
| TRAP-19 | 重叠预约连续阻塞 | 只避第一条预约 | 跳过所有当前阻塞的最大结束点 | `app/modules/schedule/search.py` | `tests/test_schedule.py::test_skips_over_both_holds` |
| TRAP-20 | 双节点环和自环 | 把下游工序也认作成环 | 强连通分量标记环成员 | `app/modules/dependencies/graph.py` | `tests/test_dependencies.py::test_cycle_members` |
| TRAP-23 | 预约与工序端点相邻 | 多占一分钟 | 半开区间统一判断 | `app/modules/calendars/intervals.py` | `tests/test_calendars.py::test_adjacent_endpoints` |

除空输入对照外，陷阱由 `sample_data/dirty/orders.jsonl` 与 `sample_data/dirty/calendar.json` 触发。预约相邻对照由 `sample_data/clean/calendar.json` 提供。修订路径的连带与锁定由 `sample_data/revision/`（A01 时长 35→55、新增 D01，日历不变）触发。

## 修订联单状态机

`DRAFT → PASSED → PUBLISHED`，任一环节复算漂移或核对不过则置 `FAILED`，旧版联单继续生效，发布接口对非 `PASSED/PUBLISHED` 一律拒绝。修订编号 = 基线编号 + 修订规则版本 + 修订输入摘要，因此同一修订重新运行命中同一主键（`ON CONFLICT DO NOTHING`），不会重复生成联单。

受影响面以直接修订点、新增工序和被删工序的原后继为种子，迭代扩张前序链后继与旧时段上共享舞台/技师的相交工序，直到不动点；集合外工序在重排时按旧 `start/end/stage_end/tech` 锁定登记，仅当旧时段在修订后日历或预约下失效才临时纳入并记录 `LOCK_INVALID`。

## 对账

- 输入行 = 已落位 + 待排 + 隔离工单 + 源隔离 + 重传 + 空行。
- 复核同工位、同技师、预约、前置顺序及两侧键集合。超时在已落位内单列。
- 技师的选择按最早可用、技师号决定。固定预约交叠与排程碰撞分别计数。
