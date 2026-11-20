# gb-4105 · 逐行存储迁移验收报告

日期：2026-11-20

## 结论
18 项自动化测试全部通过（本地 PostgreSQL 16 + FastAPI TestClient + 真实 HTTP），
外加 jsdom 前端交互流程与 24 线程真并发切换/取数压测。

| 验收点 | 覆盖用例 | 结果 |
|---|---|---|
| 回填时删行 | `test_acceptance_backfill_then_delete_during_run`、`test_concurrent_edit_during_backfill_is_kept`、`test_delete_during_backfill_is_repaired_on_finalize` | 通过 |
| 切换撞取数 | `test_acceptance_switch_races_trace_creation`、`test_switch_waits_for_in_flight_snapshot`、`test_trace_creation_waits_for_switch_then_sees_one_version`、真并发脚本（19 剖面读 JSON / 5 剖面读逐行表，均自洽） | 通过 |
| 切换后编辑再回退 | `test_acceptance_edit_after_switch_then_rollback`、`test_rollback_keeps_post_switch_edits`、`test_edit_during_switch_is_not_lost` | 通过 |
| 中断恢复 / 重试同批不重复 | `test_interrupted_backfill_resumes_from_last_batch`、`test_retrying_same_batch_does_not_duplicate`、`test_acceptance_interrupted_backfill_resume_is_idempotent` | 通过 |
| 行序与同值采样 | `test_backfill_preserves_order_and_duplicate_values`、HTTP 验收中的重复对断言 | 通过 |
| 逐条核齐才切换 | `test_switch_requires_full_verification` | 通过 |
| 旧库升级 | `test_ensure_schema_upgrades_legacy_database` | 通过 |
| 权限 | `test_viewer_cannot_backfill` | 通过 |

## 运行
```sh
./test-artifacts/gb-4105/run-tests.sh
```
需要 `TEST_DATABASE_URL` 指向 PostgreSQL，`ACCEPT_BASE_URL` 指向已 seed 的后端。

## 备注
- 后端种子/测试用本地嵌入式 PostgreSQL 16 二进制与 venv（`/tmp`，未入库）。
- 前端流程在 jsdom 中驱动：选中数据集 → 发起回填 → 回填到完成 → 读源对照 → 切换 → 回退，
  每一步都断言页面状态。
