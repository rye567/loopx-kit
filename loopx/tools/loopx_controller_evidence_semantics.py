#!/usr/bin/env python3
"""LoopX v2 结构化产物的语义校验器。

 每类产物一个校验器：solution / test_plan / security_result /
 performance_result / quality_result。语义校验在 schema 结构校验
 之后执行，检查的是“结构合法但内容不合流程要求”的问题。
"""

from __future__ import annotations


def _not_applicable(value):
    if not isinstance(value, dict):
        return False
    status = str(value.get("status") or value.get("applicability") or "").upper().replace("-", "_")
    return status in {"N/A", "NA", "NOT_APPLICABLE", "SKIPPED"} or value.get("applicable") is False


def _reason(value):
    if not isinstance(value, dict):
        return ""
    return str(value.get("reason") or value.get("not_applicable_reason") or value.get("rationale") or "").strip()


def _quality_attributes(artifact):
    value = artifact.get("quality_attributes") or artifact.get("qualities") or {}
    return value if isinstance(value, dict) else {}


def _find_attribute(attributes, *names):
    for name in names:
        if name in attributes:
            return attributes[name]
    return None


def _require_nonempty(value, path, errors):
    if value is None or value == "" or value == [] or value == {}:
        errors.append(f"{path} 不能为空")


def _mapping_ids(value, id_keys):
    if isinstance(value, dict):
        return {str(key) for key in value}
    result = set()
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                result.add(item)
            elif isinstance(item, dict):
                for key in id_keys:
                    if item.get(key):
                        result.add(str(item[key]))
                        break
    return result


REVIEW_DIMENSIONS = (
    "requirement_coverage",
    "minimal_modification",
    "existing_behavior_impact",
    "interface_contract",
    "verification_deployment",
)


def validate_review_assurance(artifact):
    """校验 catalog v3 方案审核声明的结构，不影响旧快照。"""

    errors = []
    assurance = (artifact.get("extensions") or {}).get("review_assurance")
    if not isinstance(assurance, dict):
        errors.append("solution.extensions.review_assurance 缺失")
        return errors
    snapshot_id = str(assurance.get("reviewed_snapshot_id") or "")
    if len(snapshot_id) != 64 or any(char not in "0123456789abcdef" for char in snapshot_id.lower()):
        errors.append("review_assurance.reviewed_snapshot_id 必须是 64 位内容摘要")
    if assurance.get("review_kind") not in {"FULL", "DELTA"}:
        errors.append("review_assurance.review_kind 必须是 FULL 或 DELTA")
    if assurance.get("review_kind") == "DELTA":
        baseline = str(assurance.get("baseline_snapshot_id") or "")
        if len(baseline) != 64 or any(char not in "0123456789abcdef" for char in baseline.lower()):
            errors.append("DELTA 审核的 baseline_snapshot_id 必须是 64 位内容摘要")
    _require_nonempty(assurance.get("review_scope"), "review_assurance.review_scope", errors)
    _require_nonempty(
        assurance.get("completeness_attestation"),
        "review_assurance.completeness_attestation",
        errors,
    )
    dimensions = assurance.get("checked_dimensions")
    if not isinstance(dimensions, dict):
        errors.append("review_assurance.checked_dimensions 必须是对象")
        return errors
    for name in REVIEW_DIMENSIONS:
        verdict = dimensions.get(name)
        if not isinstance(verdict, dict):
            errors.append(f"review_assurance.checked_dimensions 缺少 {name}")
            continue
        status = verdict.get("status")
        if status not in {"PASS", "NOT_APPLICABLE", "UNKNOWN"}:
            errors.append(f"review_assurance.checked_dimensions.{name}.status 不合法")
        evidence = verdict.get("evidence")
        if not isinstance(evidence, list) or any(not isinstance(item, str) or not item for item in evidence):
            errors.append(f"review_assurance.checked_dimensions.{name}.evidence 必须是字符串数组")
        if status == "PASS" and not evidence:
            errors.append(f"review_assurance.checked_dimensions.{name} 通过时必须提供证据")
        if status == "NOT_APPLICABLE" and len(str(verdict.get("reason") or "")) < 3:
            errors.append(f"review_assurance.checked_dimensions.{name} 不适用时必须说明理由")
    return errors


def validate_solution_semantics(artifact, risk_tags=None):
    errors = []
    attributes = _quality_attributes(artifact)
    dimensions = {
        "simplicity": ("simplicity", "simple_design"),
        "module_boundaries": ("module_boundaries", "boundaries", "architecture_boundaries"),
        "security": ("security",),
        "performance": ("performance",),
        "extensibility": ("extensibility",),
        "compatibility": ("compatibility",),
        "reliability": ("reliability",),
        "observability": ("observability",),
    }
    for display, aliases in dimensions.items():
        value = _find_attribute(attributes, *aliases)
        if value is None:
            errors.append(f"solution.quality_attributes 缺少 {display}")
            continue
        if not isinstance(value, dict):
            errors.append(f"solution.quality_attributes.{display} 必须是对象")
            continue
        required_fields = {"status", "approach", "reason", "evidence"}
        unknown = set(value) - required_fields
        missing = required_fields - set(value)
        if unknown:
            errors.append(f"solution.quality_attributes.{display} 包含未知字段：{', '.join(sorted(unknown))}")
        if missing:
            errors.append(f"solution.quality_attributes.{display} 缺少字段：{', '.join(sorted(missing))}")
        if value.get("status") not in {"APPLICABLE", "NOT_APPLICABLE"}:
            errors.append(f"solution.quality_attributes.{display}.status 不合法")
        if not isinstance(value.get("approach"), str):
            errors.append(f"solution.quality_attributes.{display}.approach 必须是字符串")
        if not isinstance(value.get("reason"), str):
            errors.append(f"solution.quality_attributes.{display}.reason 必须是字符串")
        evidence = value.get("evidence")
        if not isinstance(evidence, list) or any(not isinstance(item, str) or not item for item in evidence):
            errors.append(f"solution.quality_attributes.{display}.evidence 必须是字符串数组")
        if value.get("status") == "APPLICABLE":
            _require_nonempty(value.get("approach"), f"solution.quality_attributes.{display}.approach", errors)
            _require_nonempty(evidence, f"solution.quality_attributes.{display}.evidence", errors)
        if _not_applicable(value) and len(_reason(value)) < 3:
            errors.append(f"solution.quality_attributes.{display} 标记不适用时必须提供具体理由")

    tags = set(risk_tags or [])
    rollback = artifact.get("rollback")
    if not isinstance(rollback, dict):
        errors.append("solution.rollback 必须是对象")
    else:
        has_strategy = bool(rollback.get("strategy") or rollback.get("steps") or rollback.get("validation"))
        if has_strategy:
            for field in ("strategy", "steps", "validation"):
                _require_nonempty(rollback.get(field), f"solution.rollback.{field}", errors)
        elif len(_reason(rollback)) < 3:
            errors.append("solution.rollback 不适用时必须提供具体理由")

    if "performance" in tags:
        targets = artifact.get("performance_targets") or []
        if not isinstance(targets, list) or not targets:
            errors.append("命中 performance 风险时必须提供 performance_targets")
        else:
            required = (
                "metric",
                "unit",
                "target",
                "target_source",
                "load",
                "environment",
                "baseline",
                "allowed_variation",
                "evidence",
            )
            for index, target in enumerate(targets):
                if not isinstance(target, dict):
                    errors.append(f"solution.performance_targets[{index}] 必须是对象")
                    continue
                for field in required:
                    _require_nonempty(target.get(field), f"solution.performance_targets[{index}].{field}", errors)
    return errors


def validate_test_plan_semantics(artifact, required_rule_ids=None):
    errors = []
    requirement_ids = {str(item) for item in artifact.get("requirement_ids") or []}
    acceptance = artifact.get("mappings") or artifact.get("acceptance_criteria") or artifact.get("acceptance_mappings")
    covered_acceptance = _mapping_ids(acceptance, ("requirement_id", "acceptance_id", "id"))
    if requirement_ids and not requirement_ids.issubset(covered_acceptance):
        missing = sorted(requirement_ids - covered_acceptance)
        errors.append(f"test_plan 未覆盖验收/需求标识：{', '.join(missing)}")

    rule_mapping = artifact.get("mappings") or artifact.get("rule_mappings") or artifact.get("rule_coverage")
    covered_rules = set()
    if isinstance(rule_mapping, list):
        for mapping in rule_mapping:
            if isinstance(mapping, dict):
                covered_rules.update(str(item) for item in mapping.get("rule_ids") or [])
    required_rule_ids = set(required_rule_ids or [])
    if required_rule_ids and not required_rule_ids.issubset(covered_rules):
        errors.append(f"test_plan 未覆盖必需规则：{', '.join(sorted(required_rule_ids - covered_rules))}")

    cases = artifact.get("cases") or artifact.get("test_cases") or []
    if not isinstance(cases, list) or not cases:
        errors.append("test_plan.test_cases 至少需要一个测试用例")
        return errors
    case_ids = [case.get("id") for case in cases if isinstance(case, dict) and case.get("id")]
    if len(case_ids) != len(set(case_ids)):
        errors.append("test_plan.test_cases 包含重复 ID")
    cases_by_id = {
        case["id"]: case for case in cases
        if isinstance(case, dict) and isinstance(case.get("id"), str) and case.get("id")
    }
    for index, mapping in enumerate(artifact.get("mappings") or []):
        if not isinstance(mapping, dict):
            continue
        requirement_id = mapping.get("requirement_id")
        covered_by_cases = set()
        for test_case_id in mapping.get("test_case_ids") or []:
            case = cases_by_id.get(test_case_id)
            if case is None:
                errors.append(f"test_plan.mappings[{index}] 引用了未知测试用例：{test_case_id}")
                continue
            case_coverage = {str(item) for item in case.get("covers") or []}
            covered_by_cases.update(case_coverage)
            if requirement_id not in case_coverage:
                errors.append(f"测试用例 {test_case_id} 未声明覆盖 {requirement_id}")
        missing_acceptance = {
            str(item) for item in mapping.get("acceptance_ids") or []
        } - covered_by_cases
        if missing_acceptance:
            errors.append(
                f"test_plan.mappings[{index}] 的测试用例未覆盖验收标识："
                + ", ".join(sorted(missing_acceptance))
            )
    lifecycle = {
        "data_setup": ("data_setup", "setup"),
        "execution": ("execution",),
        "assertions": ("assertions",),
        "cleanup": ("cleanup",),
    }
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"test_plan.test_cases[{index}] 必须是对象")
            continue
        for name, aliases in lifecycle.items():
            value = next((case.get(key) for key in aliases if key in case), None)
            if value in (None, "", [], {}):
                errors.append(f"test_plan.test_cases[{index}].{name} 不能为空")
        execution = case.get("execution") or {}
        if not execution.get("entrypoint") or not execution.get("steps"):
            errors.append(f"test_plan.test_cases[{index}].execution 必须包含入口和步骤")
        cleanup = case.get("cleanup") or {}
        if not cleanup.get("steps") or not cleanup.get("verification"):
            errors.append(f"test_plan.test_cases[{index}].cleanup 必须包含清理动作和清理验证")
    return errors


def validate_security_semantics(artifact, risk_tags=None):
    security_tags = {"auth", "permission", "tenant_scope", "config_or_secret", "dependency", "external_side_effect"}
    tags = security_tags.intersection(set(risk_tags or []))
    if not tags:
        return []
    controls = artifact.get("controls") or artifact.get("control_results") or []
    if not isinstance(controls, list) or not controls:
        return ["security_result.controls 缺少适用安全控制结果"]
    errors = []
    required_controls = {"input", "sensitive_data", "dependency"}
    mapping = {
        "auth": "identity",
        "permission": "permission",
        "tenant_scope": "tenant_scope",
        "config_or_secret": "sensitive_data",
        "dependency": "dependency",
        "external_side_effect": "external_side_effect",
    }
    required_controls.update(mapping[tag] for tag in tags)
    actual_controls = {
        item.get("control") for item in controls if isinstance(item, dict) and item.get("control")
    }
    missing = sorted(required_controls - actual_controls)
    if missing:
        errors.append(f"security_result 缺少适用控制：{', '.join(missing)}")
    for index, control in enumerate(controls):
        if not isinstance(control, dict):
            errors.append(f"security_result.controls[{index}] 必须是对象")
            continue
        status = str(control.get("status") or "")
        if status not in {"PASS", "CHANGES_REQUIRED", "CI_REQUIRED", "BLOCKED", "SKIPPED", "ACCEPTED_RISK"}:
            errors.append(f"security_result.controls[{index}].status 不合法")
        if status == "PASS" and not (control.get("evidence") or []):
            errors.append(f"security_result.controls[{index}] 通过时必须提供证据")
        remaining = str(control.get("remaining_risk") or _reason(control)).strip()
        if status in {"CHANGES_REQUIRED", "CI_REQUIRED", "BLOCKED", "SKIPPED", "ACCEPTED_RISK"} and len(remaining) < 3:
            errors.append(f"security_result.controls[{index}] 未完成时必须提供具体理由")
    return errors


def validate_performance_semantics(artifact):
    metrics = artifact.get("metrics") or []
    if not isinstance(metrics, list) or not metrics:
        return ["performance_result.metrics 至少需要一个指标"]
    errors = []
    required = (
        "metric",
        "unit",
        "target",
        "target_source",
        "load",
        "environment",
        "baseline",
        "actual",
        "allowed_variation",
        "status",
        "evidence",
    )
    for index, metric in enumerate(metrics):
        if not isinstance(metric, dict):
            errors.append(f"performance_result.metrics[{index}] 必须是对象")
            continue
        for key in required:
            _require_nonempty(metric.get(key), f"performance_result.metrics[{index}].{key}", errors)
    return errors


def validate_quality_semantics(artifact, stage_status=""):
    errors = []
    if stage_status == "PASS" and artifact.get("unresolved_items"):
        errors.append("quality_result 仍有未解决项，阶段不能通过")
    outside = (artifact.get("diff_scope") or {}).get("outside") or []
    if stage_status == "PASS" and outside:
        errors.append("quality_result 存在超出工作项写入范围的变更")
    accepted = artifact.get("accepted_risks") or []
    accepted_ids = [item.get("rule_id") for item in accepted if isinstance(item, dict)]
    if len(accepted_ids) != len(set(accepted_ids)):
        errors.append("quality_result.accepted_risks 包含重复规则 ID")
    accepted_results = {
        item.get("rule_id") for item in (artifact.get("rule_results") or [])
        if isinstance(item, dict) and item.get("status") == "ACCEPTED_RISK"
    }
    undeclared = sorted(accepted_results - set(accepted_ids))
    unused = sorted(set(accepted_ids) - accepted_results)
    if undeclared:
        errors.append(f"quality_result 缺少逐规则风险接受确认：{', '.join(undeclared)}")
    if unused:
        errors.append(f"quality_result.accepted_risks 没有对应的风险接受结果：{', '.join(unused)}")
    for index, item in enumerate(accepted):
        if isinstance(item, dict) and len(str(item.get("reason") or "").strip()) < 3:
            errors.append(f"quality_result.accepted_risks[{index}].reason 必须是具体理由")
    return errors


SEMANTIC_VALIDATORS = {
    "solution": validate_solution_semantics,
    "test_plan": validate_test_plan_semantics,
    "security_result": validate_security_semantics,
    "performance_result": validate_performance_semantics,
    "quality_result": validate_quality_semantics,
}
