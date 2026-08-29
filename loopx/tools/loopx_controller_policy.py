#!/usr/bin/env python3
"""LoopX v2 规则目录、项目策略与运行快照。

新运行只在初始化时解析公共规则；后续检查始终读取运行内快照，避免仓库
更新导致同一运行的适用规则发生漂移。旧运行没有 ``contract_version``，
调用方必须继续按 v1 处理。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from loopx_controller_contracts import STAGES, STAGE_SEQUENCE
from loopx_controller_io import load_schema, loopx_root, project_path, validate_schema
from loopx_controller_store import runtime_relative_path
from loopx_controller_yaml import YamlSubsetError, parse_yaml_subset


CONTRACT_VERSION = "2"
CHECK_TYPES = {"schema", "builtin", "command", "review"}
RULE_LEVELS = {"required", "recommended"}
UNAVAILABLE_STATUSES = {"BLOCKED", "CI_REQUIRED", "SKIPPED"}
MODES = {"LIGHT", "STANDARD", "FULL"}
ARTIFACT_TYPES = {
    "solution",
    "test_plan",
    "development_evidence",
    "quality_result",
    "performance_result",
    "security_result",
}
ARTIFACT_SCHEMA_FILES = {
    "solution": "solution.schema.json",
    "test_plan": "test-plan.schema.json",
    "development_evidence": "development-evidence.schema.json",
    "quality_result": "quality-result.schema.json",
    "performance_result": "performance-result.schema.json",
    "security_result": "security-result.schema.json",
}
REVIEW_CHECK_STAGES = {
    "solution_design_review": "solution_review",
    "solution_review_completeness": "solution_review",
}
KNOWN_CHECKS = {
    "schema": {"solution_schema", "performance_result_schema"},
    "builtin": {
        "evidence_present",
        "solution_compatibility",
        "security_controls_by_risk",
        "performance_target_declared",
        "solution_reliability",
        "observability_evidence",
        "test_plan_coverage",
        "cleanup_plan_complete",
    },
    "review": {"solution_design_review", "solution_review_completeness"},
}


def run_contract_version(state):
    """返回运行契约版本；没有版本字段的历史运行固定视为 v1。"""

    return str(state.get("contract_version") or "1")


def is_v2_run(state):
    return run_contract_version(state) == CONTRACT_VERSION


def canonical_digest(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _schema_if_present(name):
    path = loopx_root() / "schemas" / f"{name}.schema.json"
    return load_schema(name) if path.exists() else None


def _catalog_path(project=None):
    if project is not None:
        local = Path(project).resolve() / "loopx" / "standards" / "catalog.yml"
        if local.is_file():
            return local
    return loopx_root() / "standards" / "catalog.yml"


def _risk_path(project=None):
    if project is not None:
        local = Path(project).resolve() / "loopx" / "risk.yml"
        if local.is_file():
            return local
    return loopx_root() / "risk.yml"


def _profiles_path(project=None):
    if project is not None:
        local = Path(project).resolve() / "loopx" / "project-profiles.yml"
        if local.is_file():
            return local
    return loopx_root() / "project-profiles.yml"


def _read_yaml(path, label):
    try:
        return parse_yaml_subset(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"缺少{label}：{path}") from exc
    except OSError as exc:
        raise ValueError(f"无法读取{label}：{path}：{exc}") from exc
    except YamlSubsetError as exc:
        raise ValueError(f"{label}不是有效的 LoopX YAML：{exc}") from exc


def _source_path(catalog_path, source):
    candidate = Path(source)
    if candidate.is_absolute():
        raise ValueError("规则来源必须使用 LoopX 包内相对路径")
    # catalog 位于 loopx/standards，source 以 loopx/ 为基准（例如 standards/a.md）。
    root = catalog_path.parent.parent.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("规则来源解析后超出 LoopX 包目录") from exc
    return resolved


def validate_catalog(catalog, catalog_path=None):
    """校验目录的受控结构及跨引用，返回错误列表。"""

    errors = []
    schema = _schema_if_present("standard-catalog")
    if schema:
        errors.extend(validate_schema(catalog, schema, "catalog"))
    if not isinstance(catalog, dict):
        return errors or ["catalog 必须是对象"]

    version = catalog.get("catalog_version")
    if not isinstance(version, str) or not version.strip():
        errors.append("catalog.catalog_version 必须是非空字符串")
    rule_sets = catalog.get("rule_sets")
    rules = catalog.get("rules")
    contracts = catalog.get("stage_contracts")
    if not isinstance(rule_sets, dict):
        errors.append("catalog.rule_sets 必须是对象")
        rule_sets = {}
    if not isinstance(rules, list):
        errors.append("catalog.rules 必须是数组")
        rules = []
    if not isinstance(contracts, dict):
        errors.append("catalog.stage_contracts 必须是对象")
        contracts = {}

    by_id = {}
    for index, rule in enumerate(rules):
        path = f"catalog.rules[{index}]"
        if not isinstance(rule, dict):
            errors.append(f"{path} 必须是对象")
            continue
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id.strip():
            errors.append(f"{path}.id 必须是非空字符串")
            continue
        if rule_id in by_id:
            errors.append(f"{path}.id 重复：{rule_id}")
        by_id[rule_id] = rule
        for stage in rule.get("stages") or []:
            if stage not in STAGES:
                errors.append(f"{path}.stages 包含未知阶段：{stage}")
        for mode in rule.get("modes") or []:
            if mode not in MODES:
                errors.append(f"{path}.modes 包含未知模式：{mode}")
        if rule.get("level") not in RULE_LEVELS:
            errors.append(f"{path}.level 必须是 required 或 recommended")
        check = rule.get("check")
        if not isinstance(check, dict) or check.get("type") not in CHECK_TYPES or not check.get("id"):
            errors.append(f"{path}.check 必须包含合法的 type 和非空 id")
        elif check.get("type") in KNOWN_CHECKS and check.get("id") not in KNOWN_CHECKS[check["type"]]:
            errors.append(f"{path}.check.id 不是已登记检查：{check.get('id')}")
        elif check.get("type") == "review":
            available_stage = REVIEW_CHECK_STAGES.get(check.get("id"))
            if available_stage:
                available_index = STAGE_SEQUENCE.index(available_stage)
                for stage in rule.get("stages") or []:
                    if stage in STAGES and STAGE_SEQUENCE.index(stage) < available_index:
                        errors.append(f"{path}.check 审核结果不能用于产生阶段之前：{stage}")
        for artifact_type in rule.get("evidence_types") or []:
            if artifact_type not in ARTIFACT_TYPES:
                errors.append(f"{path}.evidence_types 包含未知产物类型：{artifact_type}")
            elif catalog_path:
                schema_path = catalog_path.parent.parent / "schemas" / ARTIFACT_SCHEMA_FILES[artifact_type]
                if not schema_path.is_file():
                    errors.append(f"{path}.evidence_types 缺少结构定义：{schema_path.name}")
        if rule.get("unavailable") not in UNAVAILABLE_STATUSES:
            errors.append(f"{path}.unavailable 必须是 BLOCKED、CI_REQUIRED 或 SKIPPED")
        if rule.get("return_to") not in STAGES:
            errors.append(f"{path}.return_to 包含未知阶段：{rule.get('return_to')}")
        if catalog_path and rule.get("source"):
            try:
                source = _source_path(catalog_path, rule["source"])
            except ValueError as exc:
                errors.append(f"{path}.source 不安全：{exc}")
            else:
                if not source.is_file():
                    errors.append(f"{path}.source 不存在：{rule['source']}")

    for set_name, rule_ids in rule_sets.items():
        if not isinstance(rule_ids, list):
            errors.append(f"catalog.rule_sets.{set_name} 必须是数组")
            continue
        for rule_id in rule_ids:
            if rule_id not in by_id:
                errors.append(f"catalog.rule_sets.{set_name} 引用了未知规则：{rule_id}")
    for stage, artifact_types in contracts.items():
        if stage not in STAGES:
            errors.append(f"catalog.stage_contracts 包含未知阶段：{stage}")
        if not isinstance(artifact_types, list) or any(not isinstance(item, str) or not item for item in artifact_types):
            errors.append(f"catalog.stage_contracts.{stage} 必须是非空字符串数组")
        else:
            for artifact_type in artifact_types:
                if artifact_type not in ARTIFACT_TYPES:
                    errors.append(f"catalog.stage_contracts.{stage} 包含未知产物类型：{artifact_type}")
    return errors


def load_catalog(project=None):
    path = _catalog_path(project)
    catalog = _read_yaml(path, "规则目录")
    errors = validate_catalog(catalog, path)
    if errors:
        raise ValueError("规则目录校验失败：\n- " + "\n- ".join(errors))
    return catalog, path


def load_risk_config(project=None):
    return _read_yaml(_risk_path(project), "风险配置")


def load_project_profiles(project=None):
    """读取项目类型配置并校验第一版受控字段。"""

    path = _profiles_path(project)
    value = _read_yaml(path, "项目类型配置")
    if not isinstance(value, dict) or set(value) != {"profiles"} or not isinstance(value.get("profiles"), dict):
        raise ValueError("项目类型配置根对象只能包含 profiles")
    for name, profile in value["profiles"].items():
        prefix = f"profiles.{name}"
        if not isinstance(profile, dict):
            raise ValueError(f"{prefix} 必须是对象")
        unknown = set(profile) - {"detect", "rule_sets", "commands"}
        if unknown:
            raise ValueError(f"{prefix} 包含未知字段：{', '.join(sorted(unknown))}")
        for field in ("detect", "rule_sets"):
            items = profile.get(field)
            if not isinstance(items, list) or not items or any(not isinstance(item, str) or not item for item in items):
                raise ValueError(f"{prefix}.{field} 必须是非空字符串数组")
        commands = profile.get("commands")
        if not isinstance(commands, dict):
            raise ValueError(f"{prefix}.commands 必须是对象")
        for command_id, spec in commands.items():
            if not isinstance(spec, dict) or set(spec) - {"argv", "timeout_seconds"}:
                raise ValueError(f"{prefix}.commands.{command_id} 包含未知字段或不是对象")
            argv = spec.get("argv")
            timeout = spec.get("timeout_seconds")
            if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item for item in argv):
                raise ValueError(f"{prefix}.commands.{command_id}.argv 必须是非空字符串数组")
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
                raise ValueError(f"{prefix}.commands.{command_id}.timeout_seconds 必须是正整数")
    return value, path


def _matching_project_profiles(project, profiles, catalog):
    root = Path(project).resolve(strict=True)
    known_sets = set(catalog.get("rule_sets") or {})
    selected_names = []
    selected_sets = set()
    commands = {}
    for name, profile in profiles.get("profiles", {}).items():
        detected = False
        for marker in profile.get("detect") or []:
            candidate = Path(marker)
            if candidate.is_absolute():
                raise ValueError(f"profiles.{name}.detect 必须使用项目内相对路径")
            resolved = (root / candidate).resolve()
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"profiles.{name}.detect 解析后超出项目根目录") from exc
            detected = detected or resolved.exists()
        if not detected:
            continue
        selected_names.append(name)
        profile_sets = set(profile.get("rule_sets") or [])
        unknown = sorted(profile_sets - known_sets)
        if unknown:
            raise ValueError(f"profiles.{name} 引用了未知规则集合：{', '.join(unknown)}")
        selected_sets.update(profile_sets)
        for command_id, spec in (profile.get("commands") or {}).items():
            if command_id in commands:
                raise ValueError(f"匹配的项目类型包含重复命令：{command_id}")
            commands[command_id] = {
                "id": command_id,
                "argv": list(spec["argv"]),
                "timeout_seconds": spec["timeout_seconds"],
                "required": True,
                "ci_only": False,
            }
    return selected_names, selected_sets, commands


def validate_risk_profiles(catalog, risk_config):
    errors = []
    declared = set(risk_config.get("critical_triggers") or [])
    declared.update((risk_config.get("score_rules") or {}).keys())
    profiles = risk_config.get("risk_profiles") or {}
    missing = sorted(declared - set(profiles))
    extra = sorted(set(profiles) - declared)
    if missing:
        errors.append(f"风险标签缺少 profile：{', '.join(missing)}")
    if extra:
        errors.append(f"risk_profiles 包含未声明标签：{', '.join(extra)}")
    known_sets = set(catalog.get("rule_sets") or {})
    for tag, profile in profiles.items():
        if not isinstance(profile, dict):
            errors.append(f"risk_profiles.{tag} 必须是对象")
            continue
        minimum = profile.get("minimum_mode")
        if minimum not in MODES:
            errors.append(f"risk_profiles.{tag}.minimum_mode 不合法")
        rule_sets = profile.get("rule_sets")
        if not isinstance(rule_sets, list):
            errors.append(f"risk_profiles.{tag}.rule_sets 必须是数组")
            continue
        unknown = sorted(set(rule_sets) - known_sets)
        if unknown:
            errors.append(f"risk_profiles.{tag} 引用了未知规则集合：{', '.join(unknown)}")
        if not rule_sets and not str(profile.get("reason") or "").strip():
            errors.append(f"risk_profiles.{tag} 规则集合为空时必须说明理由")
    return errors


def load_project_policy(project):
    """读取项目根可选策略；路径固定，避免通过配置跳出项目边界。"""

    root = Path(project).resolve(strict=True)
    path = root / "loopx-policy.yml"
    if not path.exists():
        return {}, None
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("项目策略路径解析后超出项目根目录") from exc
    if not resolved.is_file():
        raise ValueError("项目策略必须是普通文件")
    policy = _read_yaml(resolved, "项目策略")
    schema = _schema_if_present("loopx-policy") or _schema_if_present("project-policy")
    if schema:
        errors = validate_schema(policy, schema, "policy")
        if errors:
            raise ValueError("项目策略校验失败：\n- " + "\n- ".join(errors))
    return policy, resolved


def _risk_rule_sets(risk_config, risk_tags):
    selected = {"common"}
    profiles = risk_config.get("risk_profiles") or {}
    for tag in risk_tags:
        profile = profiles.get(tag) or {}
        selected.update(profile.get("rule_sets") or [])
    return selected


def _candidate_rules(catalog, risk_tags, selected_sets, forced_sets=None):
    """先固定与项目和风险相关的候选规则，执行等级只负责最后筛选。"""

    rule_ids = set()
    for set_name in selected_sets:
        rule_ids.update(catalog.get("rule_sets", {}).get(set_name) or [])
    forced_rule_ids = set()
    for set_name in forced_sets or set():
        forced_rule_ids.update(catalog.get("rule_sets", {}).get(set_name) or [])
    selected = []
    risk_tags = set(risk_tags)
    for rule in catalog.get("rules") or []:
        if rule.get("id") not in rule_ids:
            continue
        required_tags = set(rule.get("risk_tags_any") or [])
        if required_tags and not required_tags.intersection(risk_tags) and rule.get("id") not in forced_rule_ids:
            continue
        selected.append(rule)
    return selected


def _rules_for_mode(rules, mode):
    return [
        dict(rule) for rule in rules
        if not rule.get("modes") or mode in set(rule.get("modes") or [])
    ]


def _apply_project_overrides(rules, policy):
    by_id = {rule["id"]: dict(rule) for rule in rules}
    unavailable_rank = {"SKIPPED": 0, "CI_REQUIRED": 1, "BLOCKED": 2}
    for override in policy.get("rule_overrides") or []:
        rule_id = override.get("rule_id")
        if rule_id not in by_id:
            raise ValueError(f"项目策略引用了当前运行不适用的规则：{rule_id}")
        current = by_id[rule_id]
        if current.get("level") == "required" and override.get("level") == "recommended":
            raise ValueError(f"项目策略不能静默降低公共必需规则：{rule_id}")
        previous_unavailable = current.get("unavailable")
        next_unavailable = override.get("unavailable", previous_unavailable)
        if unavailable_rank.get(next_unavailable, -1) < unavailable_rank.get(previous_unavailable, -1):
            raise ValueError(f"项目策略不能降低规则不可用策略：{rule_id}")
        current["level"] = override.get("level", current.get("level"))
        current["unavailable"] = override.get("unavailable", current.get("unavailable"))
        current["override_reason"] = override.get("reason", "")
    return list(by_id.values())


def build_policy_snapshot(project, mode, risk_tags):
    catalog, catalog_path = load_catalog(project)
    risk_config = load_risk_config(project)
    profiles, profiles_path = load_project_profiles(project)
    policy, policy_path = load_project_policy(project)
    risk_errors = validate_risk_profiles(catalog, risk_config)
    if risk_errors:
        raise ValueError("风险配置校验失败：\n- " + "\n- ".join(risk_errors))
    profile_names, profile_sets, profile_commands = _matching_project_profiles(project, profiles, catalog)
    policy_sets = set(policy.get("rule_sets") or [])
    selected_sets = _risk_rule_sets(risk_config, risk_tags)
    selected_sets.update(profile_sets)
    selected_sets.update(policy_sets)
    unknown_sets = sorted(selected_sets - set(catalog.get("rule_sets") or {}))
    if unknown_sets:
        raise ValueError(f"项目策略引用了未知规则集合：{', '.join(unknown_sets)}")
    candidate_rules = _candidate_rules(catalog, risk_tags, selected_sets, profile_sets | policy_sets)
    candidate_rules = _apply_project_overrides(candidate_rules, policy)
    rules = _rules_for_mode(candidate_rules, mode)
    commands = dict(profile_commands)
    for command in policy.get("commands") or []:
        commands[command["id"]] = command
    snapshot = {
        "contract_version": CONTRACT_VERSION,
        "catalog_version": catalog["catalog_version"],
        "mode": mode,
        "risk_tags": list(risk_tags),
        "project_profiles": profile_names,
        "rule_sets": sorted(selected_sets),
        "rules": rules,
        "available_rules": candidate_rules,
        "stage_contracts": catalog.get("stage_contracts") or {},
        "thresholds": policy.get("thresholds") or [],
        "commands": list(commands.values()),
        "ci_checks": policy.get("ci_checks") or [],
        "sources": {
            "catalog": str(catalog_path),
            "risk": str(_risk_path(project)),
            "project_profiles": str(profiles_path),
            "project_policy": str(policy_path) if policy_path else "",
        },
    }
    snapshot["digest"] = canonical_digest(snapshot)
    return snapshot


def reselect_policy_snapshot(snapshot, mode):
    """从初始化时固定的候选规则生成新的等级视图，不重新读取全局配置。"""

    if mode not in MODES:
        raise ValueError(f"未知执行等级：{mode}")
    available = snapshot.get("available_rules")
    if not isinstance(available, list):
        raise ValueError("规则快照缺少可用于执行等级选择的候选规则")
    selected = dict(snapshot)
    selected.pop("digest", None)
    selected["mode"] = mode
    selected["rules"] = _rules_for_mode(available, mode)
    selected["digest"] = canonical_digest(selected)
    return selected


def policy_snapshot_relative_path(run_id):
    return f"docs/loopx/runs/{run_id}/artifacts/policy-snapshot.json"


def load_policy_snapshot(project, state):
    if not is_v2_run(state):
        return None
    raw = state.get("policy_snapshot")
    if not isinstance(raw, str) or not raw:
        raise ValueError("v2 运行缺少 policy_snapshot")
    path = Path(raw)
    if path.is_absolute():
        raise ValueError("policy_snapshot 必须使用项目内相对路径")
    root = Path(project).resolve(strict=True)
    try:
        resolved = project_path(root, path).resolve(strict=True)
        if runtime_relative_path(root, resolved) is None:
            resolved.relative_to(root)
    except FileNotFoundError as exc:
        raise ValueError(f"规则快照不存在：{raw}") from exc
    except ValueError as exc:
        raise ValueError("规则快照路径解析后超出项目根目录") from exc
    if not resolved.is_file():
        raise ValueError("规则快照必须是普通文件")
    try:
        snapshot = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"规则快照不是有效 JSON：{exc}") from exc
    expected = snapshot.get("digest")
    payload = dict(snapshot)
    payload.pop("digest", None)
    actual = canonical_digest(payload)
    state_digest = state.get("policy_snapshot_sha256")
    if not expected or expected != actual or state_digest != actual:
        raise ValueError("规则快照摘要校验失败，文件可能已被修改")
    if str(snapshot.get("catalog_version")) != str(state.get("catalog_version")):
        raise ValueError("规则快照目录版本与运行状态不一致")
    if snapshot.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("规则快照契约版本不是 v2")
    if snapshot.get("mode") != state.get("mode"):
        raise ValueError("规则快照执行等级与运行状态不一致")
    return snapshot


def rules_for_stage(snapshot, stage):
    return [rule for rule in (snapshot.get("rules") or []) if stage in (rule.get("stages") or [])]


def required_artifacts_for_stage(snapshot, stage):
    return list((snapshot.get("stage_contracts") or {}).get(stage) or [])
