#!/usr/bin/env python3
"""LoopX v2 阶段记录准备与严格复检。

 本模块负责“任何持久化前”的完整校验（``prepare_v2_stage_record``）和
 严格检查时的复检（``validate_recorded_v2_stage``）；
 共享常量与文件解析在 ``_evidence_shared``，语义校验在
 ``_evidence_semantics``，工作项校验在 ``_evidence_workitems``。
 对外公共 API 由文件末尾的 re-export 保持不变。
"""

from __future__ import annotations

import json

from loopx_controller_io import (
    get_run_dir,
    load_schema,
    loopx_root,
    read_json,
    source_snapshot_id,
    validate_schema,
)
from loopx_controller_policy import (
    load_policy_snapshot,
    required_artifacts_for_stage,
    rules_for_stage,
)
from loopx_controller_evidence_shared import (
    ARTIFACT_SCHEMAS,
    ARTIFACT_VERSION,
    RULE_RESULT_STATUSES,
    resolve_project_file,
)
from loopx_controller_evidence_semantics import SEMANTIC_VALIDATORS, validate_review_assurance
from loopx_controller_evidence_workitems import (
    WORK_ITEM_INPUT_FIELDS,
    runtime_work_items,
    validate_work_item_references,
)
from loopx_controller_tickets import iter_repair_tickets
from loopx_controller_requirements import (
    canonical_json_sha256,
    file_sha256,
    prepare_requirement_freeze,
    requires_requirement_manifest,
    validate_artifact_requirements,
)


def _review_content(artifact):
    """去掉审核结论字段后比较方案内容，防止审核产物偷换设计方案。"""

    value = json.loads(json.dumps(artifact))
    value.pop("stage", None)
    value.pop("rule_results", None)
    extensions = value.get("extensions") or {}
    extensions.pop("review_assurance", None)
    value["extensions"] = extensions
    return value


def _solution_review_binding(project, state, review_artifact, source_baseline=None):
    result_path = get_run_dir(project, state.get("run_id")) / "stage-results" / "06-solution-design.json"
    if not result_path.is_file():
        raise ValueError("solution_review 前缺少已记录的 solution_design 结果")
    design_result = read_json(result_path)
    if design_result.get("status") not in {"PASS", "ACCEPTED_RISK"}:
        raise ValueError("solution_review 前的 solution_design 尚未通过")
    design_entry = next(
        (entry for entry in design_result.get("artifacts") or [] if entry.get("type") == "solution"),
        None,
    )
    if not design_entry:
        raise ValueError("solution_design 结果未记录 solution 产物")
    _, design_path = resolve_project_file(project, design_entry.get("path"), "solution_design 产物")
    design_artifact = _read_json_artifact(design_path, "solution_design 产物")
    recorded_design_sha256 = design_entry.get("sha256")
    if not recorded_design_sha256:
        raise ValueError("solution_design 结果缺少已冻结的产物摘要")
    if file_sha256(design_path) != recorded_design_sha256:
        raise ValueError("solution_design 产物在记录通过后发生变化；请返回方案设计阶段重新记录")
    if _review_content(review_artifact) != _review_content(design_artifact):
        raise ValueError("solution_review 产物与已记录的 solution_design 内容不一致；请先返回方案设计阶段")
    reviewed_snapshot_id = recorded_design_sha256
    current_source_baseline = source_baseline if source_baseline is not None else source_snapshot_id(project)
    contract = {
        "policy_snapshot_sha256": state.get("policy_snapshot_sha256") or "",
        "requirement_manifest_sha256": (state.get("spec") or {}).get("extensions", {}).get(
            "requirement_manifest_sha256"
        ) or "",
        "spec_artifact_sha256": (state.get("spec") or {}).get("extensions", {}).get("spec_artifact_sha256") or "",
        "source_baseline": current_source_baseline,
    }
    return reviewed_snapshot_id, canonical_json_sha256(contract), current_source_baseline


def _validate_review_binding(project, state, artifact, recorded_result=None):
    errors = validate_review_assurance(artifact)
    if errors:
        return errors, "", "", ""
    recorded_source_baseline = (
        recorded_result.get("review_source_snapshot_id") if recorded_result is not None else None
    )
    reviewed_snapshot_id, contract_digest, current_source_baseline = _solution_review_binding(
        project, state, artifact, source_baseline=recorded_source_baseline,
    )
    assurance = artifact["extensions"]["review_assurance"]
    if assurance.get("reviewed_snapshot_id") != reviewed_snapshot_id:
        errors.append("review_assurance.reviewed_snapshot_id 与已记录的 solution_design 内容摘要不一致")
    if assurance.get("review_kind") == "DELTA":
        if recorded_result is None and (
            current_source_baseline == "UNAVAILABLE"
            or state.get("source_baseline") in {None, "", "UNAVAILABLE"}
        ):
            errors.append("源码快照不可用，无法证明 DELTA 审核基线未变；必须执行 FULL 方案审核")
        if recorded_result is not None:
            if assurance.get("baseline_snapshot_id") != recorded_result.get("review_baseline_snapshot_id"):
                errors.append("DELTA 审核基线与已记录阶段结果不一致")
        else:
            previous = state.get("last_solution_review") or {}
            if assurance.get("baseline_snapshot_id") != previous.get("reviewed_snapshot_id"):
                errors.append("DELTA 审核的 baseline_snapshot_id 必须引用上一次已记录的方案审核快照")
            if previous.get("review_input_contract_sha256") != contract_digest:
                errors.append("需求、规格、策略或源码基线已变化，必须执行 FULL 方案审核")
    if recorded_result is not None:
        if recorded_result.get("reviewed_snapshot_id") != reviewed_snapshot_id:
            errors.append("已记录阶段结果的 reviewed_snapshot_id 与当前方案设计不一致")
        if recorded_result.get("review_input_contract_sha256") != contract_digest:
            errors.append("已记录阶段结果的审核输入契约摘要不一致")
        if recorded_result.get("review_kind") != assurance.get("review_kind"):
            errors.append("已记录阶段结果的 review_kind 与审核产物不一致")
        if recorded_result.get("review_source_snapshot_id") != current_source_baseline:
            errors.append("已记录阶段结果的 review_source_snapshot_id 与审核输入不一致")
    return errors, reviewed_snapshot_id, contract_digest, current_source_baseline


def _read_json_artifact(path, label):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}不是有效 JSON：{exc}") from exc
    except OSError as exc:
        raise ValueError(f"无法读取{label}：{exc}") from exc


def _load_artifact_schema(artifact_type):
    name = ARTIFACT_SCHEMAS[artifact_type]
    path = loopx_root() / "schemas" / f"{name}.schema.json"
    if not path.is_file():
        raise ValueError(f"缺少产物结构定义：{path.name}")
    return load_schema(name)


def _artifact_rule_results(artifact):
    value = artifact.get("rule_results") or []
    return value if isinstance(value, list) else []


def _embedded_evidence_values(value):
    """收集结构化产物内明确命名为证据引用的字段。"""

    collected = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"evidence", "verification_refs"}:
                if isinstance(child, list):
                    collected.extend(item for item in child if isinstance(item, str))
                continue
            if key == "confirmation_evidence" and isinstance(child, str):
                collected.append(child)
                continue
            collected.extend(_embedded_evidence_values(child))
    elif isinstance(value, list):
        for item in value:
            collected.extend(_embedded_evidence_values(item))
    return collected


def _canonical_rule_results(project, results):
    canonical = []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise ValueError(f"rule_results[{index}] 必须是对象")
        rule_id = result.get("rule_id") or result.get("id")
        status = result.get("status")
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError(f"rule_results[{index}].rule_id 必须是非空字符串")
        if status not in RULE_RESULT_STATUSES:
            raise ValueError(f"规则 {rule_id} 的状态不合法：{status}")
        reason = str(result.get("reason") or "")
        if status != "PASS" and len(reason.strip()) < 3:
            raise ValueError(f"规则 {rule_id} 未通过时必须提供具体理由")
        evidence = []
        for raw in result.get("evidence") or []:
            relative, _ = resolve_project_file(project, raw, f"规则 {rule_id} 的证据")
            evidence.append(relative)
        canonical.append({
            "rule_id": rule_id,
            "status": status,
            "evidence": evidence,
            "reason": reason,
        })
    ids = [item["rule_id"] for item in canonical]
    if len(ids) != len(set(ids)):
        raise ValueError("rule_results 包含重复规则 ID")
    return canonical


def _accepted_risk_is_valid(result, accepted_risk_ids):
    if len(result.get("reason") or "") < 3:
        return False
    return result["rule_id"] in accepted_risk_ids


def _validate_rule_results(stage, status, rules, results, accepted_risk_ids):
    if status != "PASS":
        return
    by_id = {item["rule_id"]: item for item in results}
    for rule in rules:
        if rule.get("level") != "required":
            continue
        rule_id = rule["id"]
        result = by_id.get(rule_id)
        if not result:
            raise ValueError(f"阶段 {stage} 缺少必需规则结果：{rule_id}")
        if result["status"] == "ACCEPTED_RISK":
            if not _accepted_risk_is_valid(result, accepted_risk_ids):
                raise ValueError(f"规则 {rule_id} 的风险接受缺少用户确认或具体理由")
        elif result["status"] != "PASS":
            unavailable = rule.get("unavailable")
            raise ValueError(f"规则 {rule_id} 未通过（{result['status']}）；不可用策略为 {unavailable}")
        if not result.get("evidence"):
            raise ValueError(f"规则 {rule_id} 通过时必须提供有效证据文件")


def prepare_v2_stage_record(
    project, state, stage, status, evidence, artifacts, affected_work_items, worklist, recorded_result=None,
):
    """在任何持久化前完成 v2 阶段输入检查并返回规范化数据。"""

    if status == "ACCEPTED_RISK":
        raise ValueError("v2 运行不接受阶段级 ACCEPTED_RISK；请在 quality_result 中逐规则记录并提供确认凭据")
    snapshot = load_policy_snapshot(project, state)
    artifact_inputs = artifacts or {}
    required_types = set(required_artifacts_for_stage(snapshot, stage))
    stage_rules = rules_for_stage(snapshot, stage)
    for rule in stage_rules:
        if rule.get("level") == "required":
            required_types.update(rule.get("evidence_types") or [])
    if status == "PASS":
        missing = sorted(required_types - set(artifact_inputs))
        if missing:
            raise ValueError(f"阶段 {stage} 通过前缺少必需产物：{', '.join(missing)}")

    artifact_entries = []
    loaded = {}
    all_rule_results = []
    all_evidence = []
    review_binding = None
    review_source_baseline = None
    for artifact_type, raw_path in artifact_inputs.items():
        if artifact_type not in ARTIFACT_SCHEMAS:
            raise ValueError(f"未知产物类型：{artifact_type}")
        relative, path = resolve_project_file(project, raw_path, f"{artifact_type} 产物")
        artifact = _read_json_artifact(path, f"{artifact_type} 产物")
        schema_errors = validate_schema(artifact, _load_artifact_schema(artifact_type), artifact_type)
        if schema_errors:
            raise ValueError(f"{artifact_type} 产物结构校验失败：\n- " + "\n- ".join(schema_errors))
        if artifact.get("artifact_type") != artifact_type:
            raise ValueError(f"{artifact_type} 产物的 artifact_type 不一致")
        if str(artifact.get("artifact_version")) != ARTIFACT_VERSION:
            raise ValueError(f"{artifact_type} 产物版本必须是 {ARTIFACT_VERSION}")
        if artifact.get("run_id") != state.get("run_id"):
            raise ValueError(f"{artifact_type} 产物的 run_id 与当前运行不一致")
        if artifact.get("stage") != stage:
            raise ValueError(f"{artifact_type} 产物声明的阶段与当前阶段不一致：{artifact.get('stage')} != {stage}")
        document_relative, _ = resolve_project_file(project, artifact.get("document"), f"{artifact_type} 文档")
        semantic = SEMANTIC_VALIDATORS.get(artifact_type)
        if semantic:
            if artifact_type in {"solution", "security_result"}:
                semantic_errors = semantic(artifact, state.get("risk_tags") or [])
            elif artifact_type == "test_plan":
                required_rules = [
                    rule["id"] for rule in (snapshot.get("rules") or [])
                    if rule.get("level") == "required"
                ]
                semantic_errors = semantic(artifact, required_rules)
            elif artifact_type == "quality_result":
                semantic_errors = semantic(artifact, status)
            else:
                semantic_errors = semantic(artifact)
            if semantic_errors:
                raise ValueError(f"{artifact_type} 产物语义校验失败：\n- " + "\n- ".join(semantic_errors))
        if (
            artifact_type == "solution"
            and stage == "solution_review"
            and requires_requirement_manifest(snapshot)
        ):
            binding_errors, reviewed_snapshot_id, contract_digest, review_source_baseline = _validate_review_binding(
                project, state, artifact, recorded_result=recorded_result,
            )
            if binding_errors:
                raise ValueError(f"solution 产物审核绑定校验失败：\n- " + "\n- ".join(binding_errors))
            review_binding = {
                "reviewed_snapshot_id": reviewed_snapshot_id,
                "review_input_contract_sha256": contract_digest,
                "review_source_snapshot_id": review_source_baseline,
                "review_kind": artifact["extensions"]["review_assurance"]["review_kind"],
                "review_baseline_snapshot_id": artifact["extensions"]["review_assurance"].get(
                    "baseline_snapshot_id"
                ) or "",
            }
        requirement_errors = validate_artifact_requirements(project, state, artifact_type, artifact)
        if requirement_errors:
            raise ValueError(f"{artifact_type} 需求全集校验失败：\n- " + "\n- ".join(requirement_errors))
        if (
            artifact_type == "solution"
            and stage == "solution_review"
            and status == "PASS"
            and requires_requirement_manifest(snapshot)
        ):
            assurance = (artifact.get("extensions") or {}).get("review_assurance") or {}
            if assurance.get("blocking_findings"):
                raise ValueError("solution_review 通过前必须清空 review_assurance.blocking_findings")
            if assurance.get("unknowns"):
                raise ValueError("solution_review 通过前必须清空 review_assurance.unknowns")
            dimensions = assurance.get("checked_dimensions") or {}
            unknown = [name for name, verdict in dimensions.items() if verdict.get("status") == "UNKNOWN"]
            if unknown:
                raise ValueError("solution_review 存在 UNKNOWN 子结论：" + ", ".join(sorted(unknown)))
        loaded[artifact_type] = artifact
        artifact_entry = {"type": artifact_type, "path": relative}
        if requires_requirement_manifest(snapshot):
            artifact_entry["sha256"] = file_sha256(path)
        artifact_entries.append(artifact_entry)
        all_evidence.extend((document_relative, relative))
        for raw in _embedded_evidence_values(artifact):
            embedded_relative, _ = resolve_project_file(project, raw, f"{artifact_type} 内嵌证据")
            all_evidence.append(embedded_relative)
        all_rule_results.extend(_artifact_rule_results(artifact))

    canonical_results = _canonical_rule_results(project, all_rule_results)
    snapshot_rule_ids = {rule["id"] for rule in (snapshot.get("rules") or [])}
    unknown_results = sorted({result["rule_id"] for result in canonical_results} - snapshot_rule_ids)
    if unknown_results:
        raise ValueError(f"产物包含当前运行未选择的规则结果：{', '.join(unknown_results)}")
    stage_rule_ids = {rule["id"] for rule in stage_rules}
    canonical_results = [result for result in canonical_results if result["rule_id"] in stage_rule_ids]
    accepted_risk_ids = {
        item.get("rule_id")
        for artifact in loaded.values()
        for item in (artifact.get("accepted_risks") or [])
        if isinstance(item, dict) and item.get("rule_id")
    }
    _validate_rule_results(stage, status, stage_rules, canonical_results, accepted_risk_ids)
    for result in canonical_results:
        all_evidence.extend(result["evidence"])
    for raw in evidence or []:
        relative, _ = resolve_project_file(project, raw, "补充证据")
        all_evidence.append(relative)
    all_evidence = list(dict.fromkeys(all_evidence))
    if status == "PASS" and not all_evidence:
        raise ValueError(f"阶段 {stage} 通过时必须提供至少一个有效证据文件")

    solution_items = None
    extra_ids = set()
    if stage == "solution_design" and status == "PASS":
        solution = loaded.get("solution")
        if not solution:
            raise ValueError("方案设计通过前必须提供 solution 产物")
        protected_ids = {
            ticket.get("item")
            for ticket in iter_repair_tickets(project, state.get("run_id"), state)
            if ticket.get("status") == "OPEN" and ticket.get("item")
        }
        solution_items = runtime_work_items(
            solution.get("work_items"),
            existing_items=worklist.get("items") or [],
            protected_ids=protected_ids,
        )
        extra_ids = {item["id"] for item in solution_items}
    validate_work_item_references(worklist, affected_work_items, extra_ids=extra_ids)
    spec_freeze = None
    if stage == "spec_review" and status == "PASS" and requires_requirement_manifest(snapshot):
        spec_freeze = prepare_requirement_freeze(project, state)
    return {
        "artifacts": artifact_entries,
        "rule_results": canonical_results,
        "evidence": all_evidence,
        "solution_items": solution_items,
        "spec_freeze": spec_freeze,
        "review_binding": review_binding,
        "review_source_baseline": review_source_baseline,
    }


def validate_recorded_v2_stage(project, state, stage, result, worklist):
    """严格检查时复用记录阶段的完整校验。"""

    artifact_map = {}
    for entry in result.get("artifacts") or []:
        if not isinstance(entry, dict) or not entry.get("type") or not entry.get("path"):
            raise ValueError(f"阶段 {stage} 的 artifacts 结构无效")
        artifact_map[entry["type"]] = entry["path"]
    prepared = prepare_v2_stage_record(
        project,
        state,
        stage,
        result.get("agent_result") or result.get("status"),
        result.get("evidence") or [],
        artifact_map,
        result.get("affected_work_items") or [],
        worklist,
        recorded_result=result,
    )
    if prepared["artifacts"] != result.get("artifacts"):
        raise ValueError(f"阶段 {stage} 的产物路径不是规范化项目内路径")
    if prepared["rule_results"] != result.get("rule_results"):
        raise ValueError(f"阶段 {stage} 的规则结果与产物不一致")
    if prepared["evidence"] != result.get("evidence"):
        raise ValueError(f"阶段 {stage} 的证据集合与产物不一致")
    if prepared.get("review_binding"):
        for key, value in prepared["review_binding"].items():
            if result.get(key) != value:
                raise ValueError(f"阶段 {stage} 的 {key} 与审核产物不一致")
    if prepared["solution_items"] is not None:
        expected = {
            item["id"]: {key: item[key] for key in WORK_ITEM_INPUT_FIELDS}
            for item in prepared["solution_items"]
        }
        actual = {
            item.get("id"): {key: item.get(key) for key in WORK_ITEM_INPUT_FIELDS}
            for item in (worklist.get("items") or []) if isinstance(item, dict) and item.get("id")
        }
        if actual != expected:
            raise ValueError("worklist 工作项与已记录方案不一致")
    for raw in result.get("confirmation_evidence") or []:
        resolve_project_file(project, raw, f"阶段 {stage} 的用户确认凭据")
    return prepared


# ---- 公共 API re-export：保持 loopx_controller_evidence.* 旧引用不变 ----
from loopx_controller_evidence_shared import (  # noqa: E402,F401
    parse_artifact_arguments,
)
from loopx_controller_evidence_semantics import (  # noqa: E402,F401
    validate_performance_semantics,
    validate_quality_semantics,
    validate_security_semantics,
    validate_solution_semantics,
    validate_test_plan_semantics,
)
from loopx_controller_evidence_workitems import (  # noqa: E402,F401
    known_work_item_ids,
    validate_work_items,
)
