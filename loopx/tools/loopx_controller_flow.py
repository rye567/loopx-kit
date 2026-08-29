#!/usr/bin/env python3
"""Stage progression rules for the LoopX controller.

所有阶段推进、确认和写入保护都从这里走；命令函数只负责调用，
不要在命令层绕开这些放行条件。
"""

import copy
from datetime import datetime

from loopx_controller_artifacts import interview_has_unanswered_placeholders
from loopx_controller_contracts import (
    CONFIRMATION_GATE_STAGES,
    DEFAULT_STAGE_OWNERS,
    MODE_SKIPPABLE_STAGES,
    PASSING_STATUSES,
    STAGES,
    STAGE_RESULT_FILES,
    STAGE_SEQUENCE,
)
from loopx_controller_io import (
    atomic_write_texts,
    append_event,
    event_line,
    get_run_dir,
    load_state,
    load_worklist,
    project_path,
    read_json,
    save_state,
    json_text,
    write_json,
)
from loopx_controller_policy import CONTRACT_VERSION, is_v2_run
from loopx_controller_state import (
    build_tracking_snapshot,
    interview_state,
    mode_decision_state,
    spec_state,
    finish_stage_timing,
    start_stage_timing,
    update_worklist_state,
    update_worklist_state_data,
)
from loopx_controller_tickets import iter_repair_tickets
from loopx_controller_requirements import apply_requirement_freeze
from loopx_controller_yaml import YamlSubsetError, dump_worklist


def stage_index(stage):
    return STAGE_SEQUENCE.index(stage)


def stages_before(stage):
    return STAGE_SEQUENCE[:stage_index(stage)]


def first_changes_required(stages):
    for stage in STAGE_SEQUENCE:
        if stages.get(stage) in {"CHANGES_REQUIRED", "BLOCKED"}:
            return stage
    return None


def first_stage_with_status(stages, status):
    for stage in STAGE_SEQUENCE:
        if stages.get(stage) == status:
            return stage
    return None


def has_open_dev_repair_ticket(project, run_id, state):
    if project is None or run_id is None:
        return False
    return any(
        ticket.get("status") == "OPEN" and ticket.get("return_to") == "development"
        for ticket in iter_repair_tickets(project, run_id, state)
    )


def confirmation_next_action(stage):
    return f"confirm-stage --stage {stage}"


def pending_confirmation_message(stage):
    return f"阶段 {stage} 正在等待用户确认；请执行 {confirmation_next_action(stage)}"


def is_waiting_confirmation(stage, status):
    return stage in CONFIRMATION_GATE_STAGES and status == "NEED_HUMAN"


def automation_is_enabled(state):
    policy = state.get("automation_policy") or {}
    return (
        policy.get("mode") == "auto_until_blocked"
        and policy.get("authorized_by") == "user_cli"
        and bool(policy.get("authorized_at"))
    )


def stored_stage_status(stage, status, state=None):
    if stage in CONFIRMATION_GATE_STAGES and status == "PASS":
        if automation_is_enabled(state or {}):
            return "PASS"
        return "NEED_HUMAN"
    return status


def default_next_stage(stage):
    index = stage_index(stage)
    if index + 1 >= len(STAGE_SEQUENCE):
        return "final_report"
    return STAGE_SEQUENCE[index + 1]


def stage_result_path(directory, stage):
    return directory / "stage-results" / STAGE_RESULT_FILES[stage]


def stage_result_next_action(stage, status, stored_status, return_to, next_action):
    if stored_status == "NEED_HUMAN":
        return confirmation_next_action(stage)
    if stored_status == "BLOCKED":
        return f"await_user:{next_action or return_to or stage}"
    if next_action:
        return next_action
    if status == "CHANGES_REQUIRED":
        return return_to
    return default_next_stage(stage)


def build_stage_result(
    state,
    stage,
    agent_result,
    stored_status,
    return_to,
    next_action,
    evidence,
    affected_work_items,
    blocked_reason,
    artifacts=None,
    rule_results=None,
    review_binding=None,
):
    snapshot_state = dict(state)
    snapshot_state["stages"] = dict(state.get("stages", {}))
    snapshot_state["stages"][stage] = stored_status
    result = {
        "stage": stage,
        "status": stored_status,
        "agent_result": agent_result,
        "mode": state.get("mode", ""),
        "summary": "",
        "return_to": return_to,
        "next_action": next_action,
        "affected_work_items": affected_work_items or [],
        "evidence": evidence,
        "tracking_snapshot": build_tracking_snapshot(snapshot_state),
        "gate": {
            "result": stored_status,
            "blocking_issues": [blocked_reason] if blocked_reason else [],
            "non_blocking_issues": [],
        },
        "user_confirmation_required": stored_status in {"CHANGES_REQUIRED", "BLOCKED", "NEED_HUMAN"},
        "blocked_reason": blocked_reason,
        "timing": dict((state.get("stage_timing") or {}).get(stage) or {}),
    }
    if stage in CONFIRMATION_GATE_STAGES and agent_result == "PASS" and stored_status == "PASS":
        result["confirmation_waived_by_init_authorization"] = automation_is_enabled(state)
    if is_v2_run(state):
        result.update({
            "contract_version": CONTRACT_VERSION,
            "artifacts": artifacts or [],
            "rule_results": rule_results or [],
        })
        if review_binding:
            result.update(review_binding)
    return result


def validate_requirement_interview_pass(project, run_id, state):
    interview = state.setdefault("interview", interview_state(run_id))
    artifact = project_path(project, interview.get("artifact"))
    if not artifact.exists():
        raise ValueError(f"需求采访产物不存在，requirement_interview 不能记录为 PASS：{interview.get('artifact')}")
    try:
        text = artifact.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"无法读取 requirement_interview 产物：{exc}") from exc
    if interview_has_unanswered_placeholders(text):
        raise ValueError("需求采访问题尚未全部回答，requirement_interview 不能记录为 PASS")
    interview["unanswered_questions"] = 0
    interview["blocking_questions"] = []


def apply_stage_metadata(state, run_id, stage, status, stored_status=None):
    effective_status = stored_status or status
    if stage == "requirement_interview":
        state.setdefault("interview", interview_state(run_id))["status"] = effective_status
    if stage == "spec_review":
        spec = state.setdefault("spec", spec_state(run_id))
        spec["gate_result"] = effective_status
        spec["approved"] = effective_status in PASSING_STATUSES
        if effective_status in PASSING_STATUSES:
            spec["status"] = "APPROVED"
    if stage == "mode_selection":
        mode_decision = state.setdefault("mode_decision", mode_decision_state(state.get("mode", ""), state.get("risk_tags", []), "auto"))
        if effective_status in PASSING_STATUSES:
            mode_decision["selection_status"] = "CONFIRMED"


def apply_stage_progression(state, stage, status, stored_status, return_to, next_action):
    state["active_agent"] = state.get("stage_owners", DEFAULT_STAGE_OWNERS).get(stage, stage)
    if stored_status == "NEED_HUMAN":
        state["next_action"] = next_action
    if stored_status == "BLOCKED":
        state["status"] = "BLOCKED"
        state["current_stage"] = stage
        state["next_action"] = next_action
        return
    if state.get("status") == "BLOCKED":
        state["status"] = "ACTIVE"
    if status != "CHANGES_REQUIRED":
        return
    # 回退会清理后续阶段，防止旧 PASS 继续影响新的方案或实现。
    target = return_to or stage
    state["current_stage"] = target
    state["active_agent"] = state.get("stage_owners", DEFAULT_STAGE_OWNERS).get(target, target)
    state["next_action"] = f"repair_{target}"
    start_stage_timing(state, target)
    for later_stage in STAGE_SEQUENCE[stage_index(target) + 1:]:
        if later_stage != stage:
            state["stages"].pop(later_stage, None)


def _same_stage_submission(existing, candidate):
    keys = (
        "stage",
        "mode",
        "agent_result",
        "status",
        "return_to",
        "next_action",
        "affected_work_items",
        "evidence",
        "artifacts",
        "rule_results",
        "blocked_reason",
        "reviewed_snapshot_id",
        "review_input_contract_sha256",
        "review_source_snapshot_id",
        "review_kind",
        "review_baseline_snapshot_id",
    )
    return all(existing.get(key) == candidate.get(key) for key in keys)


def record_prepared_v2_stage_result(
    project,
    run_id,
    state,
    stage,
    status,
    prepared,
    return_to="",
    next_action=None,
    affected_work_items=None,
    blocked_reason="",
    worklist_path=None,
    worklist=None,
    extra_files=None,
):
    """把已完成内存校验的 v2 阶段结果和附属文件作为一次提交写入。"""

    directory = get_run_dir(project, run_id)
    if worklist_path is None or worklist is None:
        try:
            worklist_path, worklist = load_worklist(project, state)
        except (FileNotFoundError, YamlSubsetError) as exc:
            raise ValueError(f"无法读取 worklist，阶段结果未写入：{exc}") from exc
    affected_work_items = affected_work_items or []
    agent_result = status
    stored_status = stored_stage_status(stage, status, state)
    computed_next_action = stage_result_next_action(stage, status, stored_status, return_to, next_action)
    new_state = copy.deepcopy(state)
    finish_stage_timing(new_state, stage)
    result = build_stage_result(
        new_state,
        stage,
        agent_result,
        stored_status,
        return_to,
        computed_next_action,
        prepared["evidence"],
        affected_work_items,
        blocked_reason,
        artifacts=prepared["artifacts"],
        rule_results=prepared["rule_results"],
        review_binding=prepared.get("review_binding"),
    )
    result_path = stage_result_path(directory, stage)
    if result_path.exists():
        existing = read_json(result_path)
        worklist_stage = next(
            (item for item in worklist.get("stages") or [] if item.get("stage") == stage),
            {},
        )
        if (
            _same_stage_submission(existing, result)
            and state.get("stages", {}).get(stage) == stored_status
            and worklist_stage.get("status") == stored_status
        ):
            return existing

    new_state.setdefault("stages", {})[stage] = stored_status
    apply_stage_metadata(new_state, run_id, stage, status, stored_status)
    if prepared.get("spec_freeze"):
        apply_requirement_freeze(new_state, prepared["spec_freeze"])
    if stage == "solution_review" and agent_result == "PASS" and prepared.get("review_binding"):
        new_state["last_solution_review"] = dict(prepared["review_binding"])
        new_state["source_baseline"] = prepared.get("review_source_baseline") or "UNAVAILABLE"
    apply_stage_progression(new_state, stage, status, stored_status, return_to, computed_next_action)
    new_worklist = copy.deepcopy(worklist)
    if prepared["solution_items"] is not None:
        new_worklist["items"] = prepared["solution_items"]
    if stage == "development" and status == "PASS":
        affected = set(affected_work_items)
        for item in new_worklist.get("items") or []:
            if item.get("id") not in affected:
                continue
            item["status"] = "PASS"
            item["evidence"] = list(prepared["evidence"])
            item["failed_by"] = ""
            item["return_to"] = ""
            item["required_changes"] = []
    update_worklist_state_data(new_worklist, new_state, stage, stored_status)

    events_path = directory / "events.jsonl"
    try:
        old_events = events_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        old_events = ""
    new_event = event_line({
        "type": "stage_recorded",
        "stage": stage,
        "status": stored_status,
        "agent_result": agent_result,
        "return_to": return_to,
        "timing": result.get("timing", {}),
    })
    files = dict(extra_files or {})
    files.update({
        result_path: json_text(result),
        directory / "state.json": json_text(new_state),
        worklist_path: dump_worklist(new_worklist),
        events_path: old_events + new_event,
    })
    atomic_write_texts(files)
    return result


def _record_v2_stage_result(
    project,
    run_id,
    state,
    stage,
    status,
    evidence,
    artifacts,
    return_to,
    next_action,
    affected_work_items,
    blocked_reason,
):
    # 延迟导入避免 evidence -> policy/io 与 flow 的模块初始化形成环。
    from loopx_controller_evidence import prepare_v2_stage_record

    try:
        worklist_path, worklist = load_worklist(project, state)
    except (FileNotFoundError, YamlSubsetError) as exc:
        raise ValueError(f"无法读取 worklist，阶段结果未写入：{exc}") from exc
    affected_work_items = affected_work_items or []
    prepared = prepare_v2_stage_record(
        project,
        state,
        stage,
        status,
        evidence,
        artifacts,
        affected_work_items,
        worklist,
    )
    return record_prepared_v2_stage_result(
        project,
        run_id,
        state,
        stage,
        status,
        prepared,
        return_to=return_to,
        next_action=next_action,
        affected_work_items=affected_work_items,
        blocked_reason=blocked_reason,
        worklist_path=worklist_path,
        worklist=worklist,
    )


def record_stage_result(
    project,
    run_id,
    stage,
    status,
    evidence,
    return_to="",
    next_action=None,
    affected_work_items=None,
    blocked_reason="",
    artifacts=None,
):
    directory = get_run_dir(project, run_id)
    state = load_state(project, run_id)
    if stage == "requirement_interview" and status == "PASS":
        validate_requirement_interview_pass(project, run_id, state)
    if is_v2_run(state):
        return _record_v2_stage_result(
            project,
            run_id,
            state,
            stage,
            status,
            evidence,
            artifacts or {},
            return_to,
            next_action,
            affected_work_items,
            blocked_reason,
        )
    agent_result = status
    stored_status = stored_stage_status(stage, status, state)
    computed_next_action = stage_result_next_action(stage, status, stored_status, return_to, next_action)
    finish_stage_timing(state, stage)
    result = build_stage_result(
        state,
        stage,
        agent_result,
        stored_status,
        return_to,
        computed_next_action,
        evidence,
        affected_work_items,
        blocked_reason,
    )
    write_json(stage_result_path(directory, stage), result)
    state.setdefault("stages", {})[stage] = stored_status
    apply_stage_metadata(state, run_id, stage, status, stored_status)
    apply_stage_progression(state, stage, status, stored_status, return_to, computed_next_action)
    save_state(project, run_id, state)
    append_event(directory, {
        "type": "stage_recorded",
        "stage": stage,
        "status": stored_status,
        "agent_result": agent_result,
        "return_to": return_to,
        "timing": result.get("timing", {}),
    })
    return result


def stage_can_be_skipped(stage, state):
    return stage in MODE_SKIPPABLE_STAGES.get(state.get("mode", ""), frozenset())


def advance_blockers(project, run_id, state, target_stage):
    blockers = []
    stages = state.get("stages", {})
    for ticket in iter_repair_tickets(project, run_id, state):
        return_to = ticket.get("return_to")
        if ticket.get("status") == "OPEN" and return_to in STAGES and stage_index(return_to) < stage_index(target_stage):
            blockers.append(f"进入 {target_stage} 前必须关闭返工单 {ticket.get('item')}")
    changed = first_changes_required(stages)
    if changed:
        blockers.append(f"阶段 {changed} 状态为 {stages[changed]}，推进前必须返回处理")
    for stage in stages_before(target_stage):
        status = stages.get(stage)
        if is_waiting_confirmation(stage, status):
            blockers.append(pending_confirmation_message(stage))
            continue
        if status in PASSING_STATUSES:
            continue
        if status == "SKIPPED" and stage_can_be_skipped(stage, state):
            continue
        blockers.append(f"进入 {target_stage} 前，阶段 {stage} 必须为 PASS")
    return blockers


def business_write_blockers(state, project=None, run_id=None):
    blockers = []
    if state.get("current_stage") != "development":
        blockers.append("当前阶段必须为 development")
    stages = state.get("stages", {})
    for stage in ("solution_review", "test_review"):
        status = stages.get(stage)
        if is_waiting_confirmation(stage, status):
            blockers.append(pending_confirmation_message(stage))
        elif status not in PASSING_STATUSES and not (
            status == "SKIPPED" and stage_can_be_skipped(stage, state)
        ):
            blockers.append(f"写入业务文件前，阶段 {stage} 必须为 PASS")
    # BLOCKED 始终锁定写入：它表示等待人工处理，不属于自动返工路径。
    blocked = first_stage_with_status(stages, "BLOCKED")
    if blocked:
        blockers.append(f"阶段 {blocked} 状态为 BLOCKED")
    # CHANGES_REQUIRED 若存在指向 development 的开放返工单，是返工信号而非阻塞：
    # 开发必须能修改代码来修复该阶段，否则状态机自我死锁（修复必须先过 can-write，
    # 而 can-write 又因待修复的 CHANGES_REQUIRED 拒绝写入）。
    changed = first_stage_with_status(stages, "CHANGES_REQUIRED")
    if changed and not has_open_dev_repair_ticket(project, run_id, state):
        blockers.append(f"阶段 {changed} 状态为 {stages[changed]}")
    return blockers


def build_confirmation(evidence, confirmed_by):
    return {
        "confirmed_by": confirmed_by,
        "confirmed_at": datetime.now().isoformat(timespec="seconds"),
        "confirmation_evidence": evidence,
    }


def apply_confirmation_result(result, state, stage, confirmation):
    result["status"] = "PASS"
    result["next_action"] = CONFIRMATION_GATE_STAGES[stage]
    result["user_confirmation_required"] = False
    result["confirmed_by"] = confirmation["confirmed_by"]
    result["confirmed_at"] = confirmation["confirmed_at"]
    result["confirmation_evidence"] = confirmation["confirmation_evidence"]
    result["tracking_snapshot"] = build_tracking_snapshot(state)
    result.setdefault("gate", {})["result"] = "PASS"


def confirm_stage(project, run_id, stage, evidence, confirmed_by):
    if stage not in CONFIRMATION_GATE_STAGES:
        raise ValueError(f"阶段 {stage} 不需要用户确认")
    directory = get_run_dir(project, run_id)
    state = load_state(project, run_id)
    current_status = state.get("stages", {}).get(stage)
    if current_status != "NEED_HUMAN":
        raise ValueError(f"确认前，阶段 {stage} 必须为 NEED_HUMAN")
    result_path = stage_result_path(directory, stage)
    result = read_json(result_path)
    if result.get("status") != "NEED_HUMAN":
        raise ValueError(f"确认前，{result_path.name} 的状态必须为 NEED_HUMAN")
    if is_v2_run(state):
        # v2 的用户确认同样属于可复核证据，不能退回 v1 的自由文本语义。
        from loopx_controller_evidence import resolve_project_file

        evidence = [
            resolve_project_file(project, raw, "用户确认凭据")[0]
            for raw in evidence
        ]
        if not evidence:
            raise ValueError("v2 用户确认必须提供至少一个有效证据文件")
    try:
        worklist_path, worklist = load_worklist(project, state)
    except (FileNotFoundError, YamlSubsetError) as exc:
        raise ValueError(f"无法读取 worklist，阶段确认未写入：{exc}") from exc
    confirmation = build_confirmation(evidence, confirmed_by)
    new_state = copy.deepcopy(state)
    new_result = copy.deepcopy(result)
    new_worklist = copy.deepcopy(worklist)
    new_state.setdefault("stages", {})[stage] = "PASS"
    new_state.setdefault("confirmations", {})[stage] = confirmation
    if stage == "requirement_interview":
        new_state.setdefault("interview", interview_state(run_id))["status"] = "PASS"
    new_state["next_action"] = CONFIRMATION_GATE_STAGES[stage]
    apply_confirmation_result(new_result, new_state, stage, confirmation)
    update_worklist_state_data(new_worklist, new_state, stage, "PASS")
    events_path = directory / "events.jsonl"
    try:
        old_events = events_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        old_events = ""
    new_event = event_line({
        "type": "stage_confirmed",
        "stage": stage,
        "confirmed_by": confirmed_by,
        "evidence": evidence,
    })
    atomic_write_texts({
        result_path: json_text(new_result),
        directory / "state.json": json_text(new_state),
        worklist_path: dump_worklist(new_worklist),
        events_path: old_events + new_event,
    })
    return confirmation
