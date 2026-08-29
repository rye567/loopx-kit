#!/usr/bin/env python3
"""需求全集冻结与下游产物一致性检查。"""

from __future__ import annotations

import hashlib
import json

from loopx_controller_io import load_schema, project_path, read_json, validate_schema


def requires_requirement_manifest(state_or_snapshot):
    try:
        return int(str(state_or_snapshot.get("catalog_version") or "0")) >= 3
    except ValueError:
        return False


def canonical_json_sha256(value):
    """计算可跨进程复核的 JSON 内容摘要。"""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path):
    """计算文件原始字节摘要，便于使用 shasum 等工具直接复核。"""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _unique_strings(values, path, errors):
    if not isinstance(values, list) or not values:
        errors.append(f"{path} 必须是非空字符串数组")
        return set()
    normalized = [str(value).strip() for value in values]
    if any(not value for value in normalized):
        errors.append(f"{path} 不能包含空值")
    if len(normalized) != len(set(normalized)):
        errors.append(f"{path} 不能包含重复值")
    return set(normalized)


def validate_requirement_manifest(manifest):
    errors = validate_schema(manifest, load_schema("requirement-manifest"), "requirement_manifest")
    if errors:
        return errors
    requirement_ids = _unique_strings(manifest.get("requirement_ids"), "requirement_ids", errors)
    acceptance_ids = _unique_strings(manifest.get("acceptance_ids"), "acceptance_ids", errors)
    units = manifest.get("delivery_units") or []
    unit_ids = [unit.get("id") for unit in units]
    if len(unit_ids) != len(set(unit_ids)):
        errors.append("delivery_units.id 不能重复")
    covered_requirements = set()
    covered_acceptance = set()
    known_units = set(unit_ids)
    for unit in units:
        covered_requirements.update(unit.get("requirement_ids") or [])
        covered_acceptance.update(unit.get("acceptance_ids") or [])
        unknown_dependencies = set(unit.get("depends_on") or []) - known_units
        if unknown_dependencies:
            errors.append(f"delivery unit {unit.get('id')} 引用了未知依赖：{', '.join(sorted(unknown_dependencies))}")
    if covered_requirements != requirement_ids:
        errors.append("delivery_units 必须完整且仅覆盖 requirement_ids")
    if covered_acceptance != acceptance_ids:
        errors.append("delivery_units 必须完整且仅覆盖 acceptance_ids")
    if len(units) > 1 and manifest.get("delivery_strategy") == "SINGLE_RUN" and len(manifest.get("coupled_reason") or "") < 3:
        errors.append("多个 delivery unit 使用 SINGLE_RUN 时必须说明 coupled_reason")
    return errors


def prepare_requirement_freeze(project, state):
    spec = state.get("spec") or {}
    extensions = spec.get("extensions") or {}
    relative = extensions.get("requirement_manifest")
    if not relative:
        raise ValueError("spec_review 通过前必须配置 requirement_manifest")
    manifest_path = project_path(project, relative)
    manifest = read_json(manifest_path)
    errors = validate_requirement_manifest(manifest)
    if errors:
        raise ValueError("需求全集校验失败：\n- " + "\n- ".join(errors))
    for item in manifest.get("deferred") or []:
        evidence = project_path(project, item.get("confirmation_evidence"))
        if not evidence.is_file():
            raise ValueError(f"延期项 {item.get('id')} 的用户确认凭据不存在：{item.get('confirmation_evidence')}")
    spec_path = project_path(project, spec.get("artifact"))
    if not spec_path.is_file():
        raise ValueError(f"需求规格不存在：{spec.get('artifact')}")
    return {
        "requirement_manifest": relative,
        "requirement_manifest_sha256": canonical_json_sha256(manifest),
        "spec_artifact_sha256": file_sha256(spec_path),
    }


def apply_requirement_freeze(state, freeze):
    extensions = state.setdefault("spec", {}).setdefault("extensions", {})
    extensions.update(freeze)


def validate_frozen_requirements(project, state):
    extensions = (state.get("spec") or {}).get("extensions") or {}
    expected = extensions.get("requirement_manifest_sha256")
    if not expected:
        if requires_requirement_manifest(state) and state.get("stages", {}).get("spec_review") in {"PASS", "ACCEPTED_RISK"}:
            return ["spec_review 已通过但缺少冻结的 requirement_manifest_sha256"]
        return []
    try:
        current = prepare_requirement_freeze(project, state)
    except ValueError as exc:
        return [str(exc)]
    errors = []
    if current["requirement_manifest_sha256"] != expected:
        errors.append("requirement_manifest 在规格审核通过后发生变化，必须返回 spec_review 重新冻结")
    if current["spec_artifact_sha256"] != extensions.get("spec_artifact_sha256"):
        errors.append("spec.md 在规格审核通过后发生变化，必须返回 spec_review 重新冻结")
    return errors


def validate_artifact_requirements(project, state, artifact_type, artifact):
    extensions = (state.get("spec") or {}).get("extensions") or {}
    expected_digest = extensions.get("requirement_manifest_sha256")
    if not expected_digest:
        if requires_requirement_manifest(state) and state.get("stages", {}).get("spec_review") in {"PASS", "ACCEPTED_RISK"}:
            return ["spec_review 已通过但缺少冻结的 requirement_manifest_sha256"]
        return []
    frozen_errors = validate_frozen_requirements(project, state)
    if frozen_errors:
        return frozen_errors
    artifact_digest = (artifact.get("extensions") or {}).get("requirement_manifest_sha256")
    errors = []
    if artifact_digest != expected_digest:
        errors.append(f"{artifact_type}.extensions.requirement_manifest_sha256 与冻结需求全集不一致")
    manifest = read_json(project_path(project, extensions.get("requirement_manifest")))
    expected_requirements = set(manifest.get("requirement_ids") or [])
    actual_requirements = set(artifact.get("requirement_ids") or [])
    if actual_requirements != expected_requirements:
        errors.append(f"{artifact_type}.requirement_ids 必须等于冻结的活动需求全集")
    if artifact_type == "test_plan":
        covered = {
            acceptance_id
            for mapping in artifact.get("mappings") or []
            if isinstance(mapping, dict)
            for acceptance_id in mapping.get("acceptance_ids") or []
        }
        missing = set(manifest.get("acceptance_ids") or []) - covered
        if missing:
            errors.append("test_plan 未覆盖冻结验收标识：" + ", ".join(sorted(missing)))
    return errors
