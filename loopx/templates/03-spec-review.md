# 03 规格审核

## 完整性检查

- 摘要：
- 期望行为：
- 验收标准：
- 范围：
- 边界情况：
- 测试策略：
- 执行等级决策：

## 歧义检查

## 范围检查

## 验收标准检查

## 可测试性检查

## 需求全集冻结

- requirement IDs：
- acceptance IDs：
- delivery units / 依赖：
- deferred / 用户确认凭据：
- `requirement_manifest_sha256`：由 controller 在 `spec_review PASS` 时写入 state。

## 必需修改

```yaml
spec_gate:
  result: PASS
  required_fields:
    summary: PASS
    expected_behavior: PASS
    acceptance_criteria: PASS
    scope: PASS
    edge_cases: PASS
    test_strategy: PASS
    mode_decision: PASS
stage_result:
  stage: spec_review
  status: PASS
  return_to: ""
  next_action: mode_selection
  affected_work_items: []
  evidence: []
  user_confirmation_required: false
  blocked_reason: ""
```
