# 03 方案审核

- 状态：NEED_HUMAN（独立审核通过后等待用户确认）
- 审核结论：
- 问题列表：
- 项目规则符合性：
- 剩余风险：
- 回退阶段：
- 下一阶段：

## 五项子结论

| 维度 | 结论（PASS / NOT_APPLICABLE / UNKNOWN） | 证据 | 理由 |
|---|---|---|---|
| 需求与验收全集覆盖 | | | |
| 满足需求的最小修改 | | | |
| 现有功能完整性与准确性 | | | |
| 内外部接口契约 | | | |
| 验证、回滚与发布 | | | |

## 审核完整性

- `reviewed_snapshot_id`（已记录 solution_design JSON 文件的 SHA-256）：
- `review_kind`：FULL / DELTA
- `baseline_snapshot_id`（DELTA 必填，必须引用上一次已记录审核快照）：
- 首轮 blocking findings：
- unknowns：
- completeness attestation：

## stage_result

```yaml
stage_result:
  stage: solution_review
  status: NEED_HUMAN
  return_to:
  next_action: confirm-stage --stage solution_review
  affected_work_items: []
  evidence: []
  user_confirmation_required: true
  blocked_reason:
```

## 证据

| 类型 | 命令/文件 | 结果 | 说明 |
|---|---|---|---|
