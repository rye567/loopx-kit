#!/usr/bin/env python3
"""Strict validation for LoopX run state.

这里集中检查“是否允许继续/收口”，命令层只能调用 validate_run，
不要在其他模块复制这些规则。
"""

from pathlib import Path

from loopx_controller_artifacts import (
    empty_spec_sections,
    interview_has_unanswered_placeholders,
    missing_spec_sections,
)
from loopx_controller_compound import validate_compound_capture
from loopx_controller_contracts import (
    CONFIRMATION_GATE_STAGES,
    FULL_REQUIRED_PASS_STAGES,
    MODE_SKIPPABLE_STAGES,
    PASSING_STATUSES,
    STAGES,
    STAGE_SEQUENCE,
    STAGE_STATUSES,
)
from loopx_controller_flow import stage_can_be_skipped, stage_result_path
from loopx_controller_io import (
    get_run_dir,
    load_schema,
    project_path,
    read_json,
    validate_schema,
)
from loopx_controller_state import mode_rank
from loopx_controller_yaml import YamlSubsetError, parse_yaml_subset
from loopx_controller_policy import is_v2_run, load_policy_snapshot
from loopx_controller_requirements import requires_requirement_manifest, validate_frozen_requirements


def strict_validation_errors(project, run_id, state, worklist):
    errors = []
    if is_v2_run(state):
        try:
            load_policy_snapshot(project, state)
        except ValueError as exc:
            errors.append(str(exc))
    for key in ("interview", "spec", "mode_decision", "tracking", "git_gate"):
        if key not in state or state[key] is None:
            errors.append(f"严格检查要求 state.{key} 存在")
    if errors:
        return errors
    if requires_requirement_manifest(state) and state.get("stages", {}).get("spec_review") in PASSING_STATUSES:
        errors.extend(validate_frozen_requirements(project, state))
    automation = state.get("automation_policy") or {}
    if automation.get("mode") == "auto_until_blocked" and (
        automation.get("authorized_by") != "user_cli" or not automation.get("authorized_at")
    ):
        errors.append("auto_until_blocked 缺少 init 显式授权来源或时间")
    for state_key, schema_name in (
        ("interview", "interview"),
        ("spec", "spec"),
        ("mode_decision", "mode"),
        ("tracking", "tracking"),
    ):
        errors.extend(validate_schema(state[state_key], load_schema(schema_name), f"state.{state_key}"))

    mode_decision = state.get("mode_decision", {})
    if not mode_decision.get("recommended"):
        errors.append("严格检查要求 state.mode_decision.recommended 存在")
    if not mode_decision.get("selected"):
        errors.append("严格检查要求 state.mode_decision.selected 存在")
    if mode_decision.get("selection_status") != "CONFIRMED":
        errors.append("严格检查要求 state.mode_decision.selection_status 为 CONFIRMED")
    if mode_rank(mode_decision.get("recommended")) > mode_rank(mode_decision.get("selected")):
        accepted = mode_decision.get("accepted_risk", {})
        if not accepted.get("selected_lower_than_recommended") or not accepted.get("reason"):
            errors.append("所选执行等级低于建议等级时，必须填写 state.mode_decision.accepted_risk.reason")
    # SKIPPED 只能出现在该模式允许跳过的阶段（MODE_SKIPPABLE_STAGES 为唯一事实源）。
    skippable = MODE_SKIPPABLE_STAGES.get(state.get("mode", ""), frozenset())
    for stage, status in state.get("stages", {}).items():
        if status == "SKIPPED" and stage not in skippable:
            errors.append(f"{state.get('mode')} 执行等级不允许将阶段 {stage} 设为 SKIPPED")
        if status == "BLOCKED":
            errors.append(f"阶段 {stage} 仍为 BLOCKED，必须先解除阻塞")
    if state.get("stages", {}).get("final_report") == "PASS":
        # final_report 是收口入口，必须同时有发布准备、Git 摘要和复利沉淀决策。
        release_status = state.get("stages", {}).get("release_readiness")
        if release_status not in PASSING_STATUSES and not (
            release_status == "SKIPPED" and stage_can_be_skipped("release_readiness", state)
        ):
            errors.append("final_report 记录为 PASS 前，release_readiness 必须为 PASS")
        git_gate = state.get("git_gate", {})
        if git_gate.get("status") != "PASS":
            errors.append("final_report 记录为 PASS 前，state.git_gate.status 必须为 PASS")
        if not git_gate.get("diff_summary"):
            errors.append("final_report 记录为 PASS 前，必须填写 state.git_gate.diff_summary")
        errors.extend(validate_compound_capture(project, state.get("compound_capture", {}), load_schema("compound-learning"), validate_schema))

    worklist_stages = worklist.get("stages") or []
    worklist_by_stage = {stage.get("stage"): stage for stage in worklist_stages if isinstance(stage, dict)}
    seen = set(worklist_by_stage)
    for stage in STAGE_SEQUENCE:
        if stage not in seen:
            errors.append(f"worklist.stages 必须包含 {stage}")
    if worklist.get("run", {}).get("current_stage") != state.get("current_stage"):
        errors.append("worklist.run.current_stage 必须与 state.current_stage 一致")
    if worklist.get("run", {}).get("status") != state.get("status"):
        errors.append("worklist.run.status 必须与 state.status 一致")
    if worklist.get("run", {}).get("next_action", "") != state.get("next_action", ""):
        errors.append("worklist.run.next_action 必须与 state.next_action 一致")
    for stage, status in state.get("stages", {}).items():
        worklist_stage = worklist_by_stage.get(stage)
        if worklist_stage and worklist_stage.get("status") != status:
            errors.append(f"worklist.stages[{stage}].status 必须与 state.stages.{stage} 一致")

    directory = get_run_dir(project, run_id)
    for stage, status in state.get("stages", {}).items():
        if status not in PASSING_STATUSES:
            continue
        result_path = stage_result_path(directory, stage)
        if not result_path.exists():
            errors.append(f"阶段 {stage} 为 {status}，但缺少 {result_path.name}")
            continue
        try:
            result = read_json(result_path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if is_v2_run(state):
            try:
                from loopx_controller_evidence import validate_recorded_v2_stage

                validate_recorded_v2_stage(project, state, stage, result, worklist)
            except ValueError as exc:
                errors.append(f"阶段 {stage} 的 v2 证据复核失败：{exc}")
        if not result.get("tracking_snapshot"):
            errors.append(f"严格检查要求 {result_path.name}.tracking_snapshot 存在")
        else:
            snapshot_stages = {item.get("stage") for item in result.get("tracking_snapshot", []) if isinstance(item, dict)}
            if snapshot_stages != set(STAGE_SEQUENCE):
                errors.append(f"{result_path.name}.tracking_snapshot 必须包含全部 LoopX 阶段")
        if stage in CONFIRMATION_GATE_STAGES and status == "PASS":
            waived = (
                result.get("confirmation_waived_by_init_authorization") is True
                and (state.get("automation_policy") or {}).get("mode") == "auto_until_blocked"
            )
            if not waived and (
                not result.get("confirmed_by")
                or not result.get("confirmed_at")
                or not result.get("confirmation_evidence")
            ):
                errors.append(f"阶段 {stage} 记录为 PASS 时必须包含确认信息")

    for stage, key in (("requirement_interview", "interview"), ("spec_review", "spec")):
        if state.get("stages", {}).get(stage) in PASSING_STATUSES:
            artifact = Path(state.get(key, {}).get("artifact", ""))
            if not artifact.is_absolute():
                artifact = project_path(project, artifact)
            if not artifact.exists():
                errors.append(f"阶段 {stage} 已通过，但 state.{key}.artifact 指向的文件不存在")
                continue
            if stage == "requirement_interview" and state.get("interview", {}).get("unanswered_questions", 0) != 0:
                errors.append("requirement_interview 已通过时，state.interview.unanswered_questions 必须为 0")
            if stage == "requirement_interview":
                try:
                    text = artifact.read_text(encoding="utf-8")
                except OSError as exc:
                    errors.append(f"无法读取 state.interview.artifact：{exc}")
                    continue
                if interview_has_unanswered_placeholders(text):
                    errors.append("interview.md 仍包含未回答占位内容")
            if stage == "spec_review":
                try:
                    text = artifact.read_text(encoding="utf-8")
                except OSError as exc:
                    errors.append(f"无法读取 state.spec.artifact：{exc}")
                    continue
                for section in missing_spec_sections(text):
                    errors.append(f"spec.md 缺少必需章节：{section}")
                for section in empty_spec_sections(text):
                    errors.append(f"spec.md 的必需章节为空：{section}")
    if state.get("stages", {}).get("final_report") == "PASS" and (state.get("mode") == "FULL" or mode_decision.get("selected") == "FULL"):
        for stage in FULL_REQUIRED_PASS_STAGES:
            if state.get("stages", {}).get(stage) not in PASSING_STATUSES:
                errors.append(f"FULL 执行等级要求阶段 {stage} 在 final_report 通过前为 PASS")
    return errors


def validate_run(project, run_id, strict=False):
    errors = []
    directory = get_run_dir(project, run_id)
    state_path = directory / "state.json"
    try:
        state = read_json(state_path)
    except ValueError as exc:
        return [str(exc)]

    errors.extend(validate_schema(state, load_schema("state")))
    if state.get("run_id") != run_id:
        errors.append("state.run_id 必须与所选运行一致")
    if state.get("current_stage") and state["current_stage"] not in STAGES:
        errors.append("current_stage 不是已知的 LoopX 阶段")
    for stage, status in state.get("stages", {}).items():
        if stage not in STAGES:
            errors.append(f"stages.{stage} 不是已知的 LoopX 阶段")
        if status not in STAGE_STATUSES:
            errors.append(f"stages.{stage} 的状态无效：{status}")

    worklist_rel = state.get("worklist") or f"docs/loopx/runs/{run_id}/worklist.yml"
    worklist_path = Path(worklist_rel)
    if not worklist_path.is_absolute():
        worklist_path = project_path(project, worklist_path)
    worklist = None
    try:
        worklist = parse_yaml_subset(worklist_path.read_text(encoding="utf-8"))
        errors.extend(validate_schema(worklist, load_schema("worklist")))
    except FileNotFoundError:
        errors.append(f"工作清单不存在：{worklist_path}")
    except YamlSubsetError as exc:
        errors.append(f"工作清单不是有效的 LoopX YAML：{worklist_path}：{exc}")

    stage_result_root = directory / "stage-results"
    if stage_result_root.exists():
        for result_path in sorted(stage_result_root.glob("*.json")):
            try:
                result = read_json(result_path)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            errors.extend(validate_schema(result, load_schema("stage-result"), result_path.name))
    if strict and worklist is not None:
        errors.extend(strict_validation_errors(project, run_id, state, worklist))
    return errors
