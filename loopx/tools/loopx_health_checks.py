#!/usr/bin/env python3
"""LoopX 核心健康检查器集合。

 只实现“给上下文 -> 出一条 HealthCheckResult”的纯检查逻辑；
 配置装配、命令执行和报告写入由 ``loopx_health.py`` / ``loopx_health_commands.py`` 负责。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from loopx_controller_contracts import (  # noqa: E402
    MODE_SKIPPABLE_STAGES,
)
from loopx_controller_policy import load_policy_snapshot  # noqa: E402
from loopx_controller_yaml import YamlSubsetError  # noqa: E402

from loopx_health_base import (  # noqa: E402
    BLOCKED,
    CI_REQUIRED,
    HealthCheckResult,
    HealthContext,
    PASS,
    PASSING_STATUSES,
    RESOLVED_WORK_ITEM_STATUSES,
    SKIPPED,
    STAGE_RESULT_FILES,
    STAGE_SEQUENCE,
    _iter_evidence,
    _looks_like_path,
    _result,
    _safe_file,
    _worklist,
)


def _check_stage_artifacts(ctx: HealthContext) -> HealthCheckResult:
    before_health = STAGE_SEQUENCE[:STAGE_SEQUENCE.index("health_gate")]
    skippable = MODE_SKIPPABLE_STAGES.get(ctx.state.get("mode", ""), frozenset())
    missing = []
    evidence = []
    statuses = ctx.state.get("stages") or {}
    for stage in before_health:
        status = statuses.get(stage)
        if status == SKIPPED and stage in skippable:
            continue
        if status not in PASSING_STATUSES:
            missing.append(f"{stage} 状态为 {status or 'PENDING'}")
            continue
        filename = STAGE_RESULT_FILES[stage]
        path = ctx.run_dir / "stage-results" / filename
        if not path.is_file():
            missing.append(f"{stage} 缺少 {filename}")
        else:
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                missing.append(f"{filename} 无法读取：{exc}")
                continue
            if not isinstance(result, dict) or result.get("stage") != stage or result.get("status") not in PASSING_STATUSES:
                missing.append(f"{filename} 的阶段或状态与 state.json 不一致")
                continue
            evidence.append(str(path))
    if missing:
        return _result("stage_artifacts_complete", BLOCKED, "必需阶段尚未完成。", missing)
    return _result("stage_artifacts_complete", PASS, "必需阶段及结果文件完整。", evidence)


def _check_worklist(ctx: HealthContext) -> HealthCheckResult:
    try:
        items = _worklist(ctx).get("items") or []
    except (OSError, ValueError, YamlSubsetError) as exc:
        return _result("worklist_items_resolved", BLOCKED, f"无法检查 worklist：{exc}")
    unresolved = [
        f"{item.get('id', '<unknown>')}:{item.get('status', 'PENDING')}"
        for item in items
        if not isinstance(item, dict) or (
            item.get("lineage", {}).get("state", "ACTIVE") == "ACTIVE"
            and item.get("status") not in RESOLVED_WORK_ITEM_STATUSES
        )
    ]
    if unresolved:
        return _result("worklist_items_resolved", BLOCKED, "worklist 存在未解决项。", unresolved)
    return _result("worklist_items_resolved", PASS, "worklist 没有未解决项。")


def _check_evidence(ctx: HealthContext) -> HealthCheckResult:
    invalid = []
    checked = []
    for path, result in ctx.stage_results():
        if result.get("status") not in PASSING_STATUSES:
            continue
        values = list(_iter_evidence(result))
        if ctx.is_v2 and not values:
            invalid.append(f"{path.name} 没有证据")
        for raw in values:
            if not ctx.is_v2 and not _looks_like_path(raw):
                continue
            try:
                checked.append(str(_safe_file(ctx.project, raw)))
            except ValueError as exc:
                invalid.append(f"{path.name}: {raw}: {exc}")
    if invalid:
        return _result("validation_evidence_exists", BLOCKED, "存在无效或缺失的证据文件。", invalid)
    return _result("validation_evidence_exists", PASS, "已引用的证据文件均可读取。", checked)


def _check_cleanup(ctx: HealthContext) -> HealthCheckResult:
    path = ctx.run_dir / "stage-results" / STAGE_RESULT_FILES["test_execution"]
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _result("cleanup_verified", BLOCKED, f"无法读取测试执行结果：{exc}")
    if not ctx.is_v2 and result.get("status") in PASSING_STATUSES:
        return _result("cleanup_verified", PASS, "旧运行以已通过的测试执行结果作为清理证明。", [str(path)])
    candidates = [(path, result)]
    for raw in _iter_evidence(result):
        if not raw.lower().endswith(".json"):
            continue
        try:
            evidence_path = _safe_file(ctx.project, raw)
            value = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (ValueError, OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            candidates.append((evidence_path, value))
    for candidate_path, value in candidates:
        cleanup = value.get("cleanup") if isinstance(value.get("cleanup"), dict) else {}
        extensions = value.get("extensions") if isinstance(value.get("extensions"), dict) else {}
        extension_cleanup = extensions.get("cleanup") if isinstance(extensions.get("cleanup"), dict) else {}
        verified = (
            value.get("cleanup_verified") is True
            or cleanup.get("verified") is True
            or extensions.get("cleanup_verified") is True
            or extension_cleanup.get("verified") is True
        )
        not_applicable = any(
            item.get("not_applicable") is True and bool(str(item.get("reason") or "").strip())
            for item in (cleanup, extension_cleanup)
        )
        if verified or not_applicable:
            return _result(
                "cleanup_verified",
                PASS,
                "测试数据清理已验证或已说明不适用。",
                [str(candidate_path)],
            )
    return _result("cleanup_verified", BLOCKED, "v2 测试结果缺少清理验证或不适用理由。", [str(path)])


def _check_ci_gap(ctx: HealthContext) -> HealthCheckResult:
    workflows = sorted((ctx.project / ".github" / "workflows").glob("*.yml"))
    workflows += sorted((ctx.project / ".github" / "workflows").glob("*.yaml"))
    if workflows:
        return _result("ci_gap_declared", PASS, "已检测到 CI 配置。", [str(path) for path in workflows])
    return _result("ci_gap_declared", CI_REQUIRED, "本地未检测到 CI 配置，结果已声明为需要 CI 验证。")


def _rule_results(ctx: HealthContext) -> dict[str, dict]:
    results = {}
    for _, stage_result in ctx.stage_results():
        for value in stage_result.get("rule_results") or []:
            if isinstance(value, dict):
                rule_id = value.get("rule_id") or value.get("id")
                if isinstance(rule_id, str) and rule_id:
                    results[rule_id] = value
    return results


def _check_required_rules(ctx: HealthContext) -> HealthCheckResult:
    try:
        snapshot = load_policy_snapshot(ctx.project, ctx.state)
    except ValueError as exc:
        return _result("required_rule_results", BLOCKED, f"无法读取规则快照：{exc}")
    actual = _rule_results(ctx)
    completed_stages = set(STAGE_SEQUENCE[:STAGE_SEQUENCE.index("health_gate")])
    required = [
        rule for rule in snapshot.get("rules") or []
        if rule.get("level") == "required"
        and completed_stages.intersection(rule.get("stages") or [])
    ]
    missing = []
    unavailable = []
    for rule in required:
        value = actual.get(rule.get("id"))
        if not value:
            missing.append(str(rule.get("id") or "<unknown>"))
        elif not value.get("evidence"):
            missing.append(f"{rule.get('id')} 缺少证据")
        elif value.get("status") in PASSING_STATUSES:
            continue
        elif value.get("status") == rule.get("unavailable") and value.get("status") in {CI_REQUIRED, SKIPPED}:
            unavailable.append(value.get("status"))
        else:
            missing.append(f"{rule.get('id')} 状态为 {value.get('status') or 'UNKNOWN'}")
    if missing:
        return _result("required_rule_results", BLOCKED, "必需规则缺少有效结果。", missing)
    if CI_REQUIRED in unavailable:
        return _result("required_rule_results", CI_REQUIRED, "部分必需规则已声明由 CI 验证。", sorted(actual))
    if SKIPPED in unavailable:
        return _result("required_rule_results", SKIPPED, "部分必需规则按配置跳过。", sorted(actual))
    return _result("required_rule_results", PASS, "必需规则均有有效结果。", sorted(actual))


def _check_accepted_risks(ctx: HealthContext) -> HealthCheckResult:
    unconfirmed = []
    confirmed_rule_ids = set()
    for _, stage_result in ctx.stage_results():
        for entry in stage_result.get("artifacts") or []:
            if not isinstance(entry, dict) or entry.get("type") != "quality_result":
                continue
            try:
                artifact_path = _safe_file(ctx.project, entry.get("path") or "")
                artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            for item in artifact.get("accepted_risks") or []:
                if not isinstance(item, dict) or not item.get("rule_id") or not item.get("confirmation_evidence"):
                    continue
                try:
                    _safe_file(ctx.project, item["confirmation_evidence"])
                except ValueError:
                    continue
                confirmed_rule_ids.add(item["rule_id"])
    for rule_id, value in _rule_results(ctx).items():
        if value.get("status") != "ACCEPTED_RISK":
            continue
        if rule_id not in confirmed_rule_ids:
            unconfirmed.append(rule_id)
    decision = ctx.state.get("mode_decision") or {}
    accepted = decision.get("accepted_risk") or {}
    if accepted.get("selected_lower_than_recommended"):
        if not accepted.get("reason") or decision.get("selected_by") != "user":
            unconfirmed.append("mode_selection")
    if unconfirmed:
        return _result("accepted_risks_confirmed", BLOCKED, "存在未获用户确认的风险接受。", unconfirmed)
    return _result("accepted_risks_confirmed", PASS, "风险接受记录均已确认。")


def _check_snapshot(ctx: HealthContext) -> HealthCheckResult:
    try:
        load_policy_snapshot(ctx.project, ctx.state)
    except ValueError as exc:
        return _result("policy_snapshot_integrity", BLOCKED, f"规则快照检查失败：{exc}")
    return _result(
        "policy_snapshot_integrity",
        PASS,
        "规则快照摘要有效。",
        [str(ctx.state.get("policy_snapshot"))],
    )


CORE_CHECKS: dict[str, Callable[[HealthContext], HealthCheckResult]] = {
    "stage_artifacts_complete": _check_stage_artifacts,
    "worklist_items_resolved": _check_worklist,
    "validation_evidence_exists": _check_evidence,
    "cleanup_verified": _check_cleanup,
    "ci_gap_declared": _check_ci_gap,
    "required_rule_results": _check_required_rules,
    "accepted_risks_confirmed": _check_accepted_risks,
    "policy_snapshot_integrity": _check_snapshot,
}
