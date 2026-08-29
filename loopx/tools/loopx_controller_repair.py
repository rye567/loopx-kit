#!/usr/bin/env python3
"""Review feedback and repair commands for LoopX.

返工流程负责把失败反馈带回原 owner 阶段；它不直接跳过阶段检查，
修复完成后仍要通过 flow/validation 的统一规则。
"""

import copy
from pathlib import Path

from loopx_controller_contracts import DEFAULT_STAGE_OWNERS, STAGE_SEQUENCE
from loopx_controller_flow import record_prepared_v2_stage_result, record_stage_result, stage_index
from loopx_controller_io import atomic_write_texts, get_run_dir, json_text, load_state, load_worklist, save_state
from loopx_controller_state import resolve_run_id, update_worklist_state
from loopx_controller_policy import is_v2_run
from loopx_controller_evidence import (
    prepare_v2_stage_record,
    resolve_project_file,
    validate_work_item_references,
)
from loopx_controller_tickets import (
    open_repair_tickets_for_stage,
    read_repair_ticket,
    repair_ticket_path,
    write_repair_ticket,
)
from loopx_controller_yaml import YamlSubsetError, dump_worklist


def update_worklist_feedback_data(worklist, item_id, return_to, reasons):
    """在内存中合并返工反馈，由调用方与 state/ticket 一次提交。"""

    for item in worklist.get("items") or []:
        if item.get("id") != item_id:
            continue
        item["status"] = "CHANGES_REQUIRED"
        item["failed_by"] = "user_feedback"
        item["return_to"] = return_to
        changes = item.get("required_changes") or []
        for reason in reasons:
            if reason not in changes:
                changes.append(reason)
        item["required_changes"] = changes
    worklist["run"]["current_stage"] = return_to


def update_worklist_feedback(project, state, item_id, return_to, reasons):
    try:
        worklist_path, worklist = load_worklist(project, state)
    except (FileNotFoundError, YamlSubsetError):
        return
    update_worklist_feedback_data(worklist, item_id, return_to, reasons)
    worklist_path.write_text(dump_worklist(worklist), encoding="utf-8")


def fail_review(project, run_id, from_stage, return_to, item_id, reasons, allow_historical=False):
    state = load_state(project, run_id)
    current_stage = state.get("current_stage")
    if not allow_historical and current_stage != from_stage:
        raise ValueError(f"当前阶段为 {current_stage}，不能提交 {from_stage} 的审核失败")
    if allow_historical and current_stage in STAGE_SEQUENCE and stage_index(current_stage) < stage_index(from_stage):
        raise ValueError(f"当前阶段为 {current_stage}，不能反馈尚未执行的 {from_stage}")
    v2 = is_v2_run(state)
    worklist_path = None
    worklist = None
    if v2:
        try:
            worklist_path, worklist = load_worklist(project, state)
        except (FileNotFoundError, YamlSubsetError) as exc:
            raise ValueError(f"无法读取 worklist：{exc}") from exc
        validate_work_item_references(worklist, [item_id])
    new_state = copy.deepcopy(state)
    owner = state.get("stage_owners", DEFAULT_STAGE_OWNERS).get(return_to, return_to)
    # 按阶段递增返工次数；超过 max_auto_repair 后第 N 次仍失败则 BLOCKED，等待用户处理。
    # ticket.status 只表达票据生命周期（OPEN/CLOSED），阶段结果记录在 stage 状态里。
    attempts = new_state.setdefault("loop_attempts", {})
    attempt = int(attempts.get(from_stage, 0)) + 1
    attempts[from_stage] = attempt
    limit = int(state.get("max_auto_repair", 2))
    stage_status = "CHANGES_REQUIRED" if attempt <= limit else "BLOCKED"
    ticket = {
        "type": "review_failed",
        "item": item_id,
        "from_stage": from_stage,
        "return_to": return_to,
        "assigned_to": owner,
        "attempt": attempt,
        "status": "OPEN",
        "stage_status": stage_status,
        "required_changes": reasons,
        "artifact": "",
        "revision": 0,
        "changes_from_review": [],
    }
    if v2:
        update_worklist_feedback_data(worklist, item_id, return_to, reasons)
        prepared = prepare_v2_stage_record(
            project,
            new_state,
            from_stage,
            stage_status,
            [],
            {},
            [item_id],
            worklist,
        )
        record_prepared_v2_stage_result(
            project,
            run_id,
            new_state,
            from_stage,
            stage_status,
            prepared,
            return_to=return_to,
            next_action=f"repair_{return_to}",
            affected_work_items=[item_id],
            blocked_reason=f"auto repair exceeded max_auto_repair={limit}" if stage_status == "BLOCKED" else "",
            worklist_path=worklist_path,
            worklist=worklist,
            extra_files={repair_ticket_path(project, run_id, item_id, new_state): json_text(ticket)},
        )
        return ticket

    new_state["current_stage"] = return_to
    new_state["next_action"] = f"repair_{return_to}"
    new_state["active_agent"] = owner
    new_state.setdefault("stages", {})[from_stage] = stage_status
    for later_stage in STAGE_SEQUENCE[stage_index(return_to) + 1:]:
        if later_stage != from_stage:
            new_state["stages"].pop(later_stage, None)
    save_state(project, run_id, new_state)
    write_repair_ticket(project, run_id, item_id, ticket, new_state)
    update_worklist_feedback(project, new_state, item_id, return_to, reasons)
    record_stage_result(
        project,
        run_id,
        from_stage,
        stage_status,
        reasons,
        return_to=return_to,
        next_action=f"repair_{return_to}",
        affected_work_items=[item_id],
        blocked_reason=f"auto repair exceeded max_auto_repair={limit}" if stage_status == "BLOCKED" else "",
    )
    # record_stage_result 已重新落库 state，这里再同步 worklist 的 stage 状态，避免 strict 校验漂移。
    new_state = load_state(project, run_id)
    update_worklist_state(project, new_state, from_stage, stage_status)
    return ticket


def cmd_fail_review(args, stdout):
    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
        ticket = fail_review(project, run_id, args.from_stage, args.return_to, args.item, args.reason)
    except ValueError as exc:
        print(str(exc), file=stdout)
        return 1
    print(f"{ticket['stage_status']} 审核未通过：{args.from_stage}", file=stdout)
    print(f"返工单：{ticket['item']}", file=stdout)
    print(f"返回阶段：{ticket['return_to']}", file=stdout)
    print(f"负责人：{ticket['assigned_to']}", file=stdout)
    print(f"尝试次数：{ticket['attempt']}", file=stdout)
    return 0


def cmd_review_feedback(args, stdout):
    args.from_stage = "solution_review"
    args.reason = [args.reason]
    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
        ticket = fail_review(
            project, run_id, args.from_stage, args.return_to, args.item, args.reason,
            allow_historical=True,
        )
    except ValueError as exc:
        print(str(exc), file=stdout)
        return 1
    print(f"{ticket['stage_status']} 审核未通过：{args.from_stage}", file=stdout)
    print(f"返工单：{ticket['item']}", file=stdout)
    print(f"返回阶段：{ticket['return_to']}", file=stdout)
    print(f"负责人：{ticket['assigned_to']}", file=stdout)
    print(f"尝试次数：{ticket['attempt']}", file=stdout)
    return 0


def cmd_claim_stage(args, stdout):
    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
        state = load_state(project, run_id)
    except ValueError as exc:
        print(str(exc), file=stdout)
        return 1
    if state.get("current_stage") != args.stage:
        print(f"FAIL 当前阶段为 {state.get('current_stage')}，无法领取 {args.stage}", file=stdout)
        return 1
    owner = state.get("stage_owners", DEFAULT_STAGE_OWNERS).get(args.stage, args.stage)
    state["active_agent"] = owner
    save_state(project, run_id, state)
    print(f"PASS 已领取阶段：{args.stage}", file=stdout)
    print(f"负责人：{owner}", file=stdout)
    for ticket in open_repair_tickets_for_stage(project, run_id, args.stage, state):
        print(f"返工单：{ticket.get('item')}", file=stdout)
        print(f"尝试次数：{ticket.get('attempt')}", file=stdout)
        for change in ticket.get("required_changes", []):
            print(f"必需修改：{change}", file=stdout)
    return 0


def cmd_close_repair(args, stdout):
    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
        state = load_state(project, run_id)
        if is_v2_run(state):
            try:
                _, worklist = load_worklist(project, state)
            except (FileNotFoundError, YamlSubsetError) as exc:
                raise ValueError(f"无法读取 worklist：{exc}") from exc
            validate_work_item_references(worklist, [args.item])
            args.artifact, _ = resolve_project_file(project, args.artifact, "返工产物")
        ticket = read_repair_ticket(project, run_id, args.item, state)
    except ValueError as exc:
        print(str(exc), file=stdout)
        return 1
    if args.revision < 2:
        print("FAIL 返工产物版本必须大于或等于 2", file=stdout)
        return 1
    new_ticket = copy.deepcopy(ticket)
    new_ticket["status"] = "CLOSED"
    new_ticket["artifact"] = args.artifact
    new_ticket["revision"] = args.revision
    new_ticket["changes_from_review"] = args.change
    new_state = copy.deepcopy(state)
    from_stage = ticket.get("from_stage")
    if from_stage:
        new_state.setdefault("stages", {}).pop(from_stage, None)
        # 返工项关闭后重置该阶段的返工计数，允许重新计数自动返工上限。
        new_state.setdefault("loop_attempts", {}).pop(from_stage, None)
    atomic_write_texts({
        repair_ticket_path(project, run_id, args.item, state): json_text(new_ticket),
        get_run_dir(project, run_id) / "state.json": json_text(new_state),
    })
    print(f"PASS 已关闭返工单：{args.item}", file=stdout)
    print(f"产物：{args.artifact}", file=stdout)
    print(f"版本：{args.revision}", file=stdout)
    return 0
