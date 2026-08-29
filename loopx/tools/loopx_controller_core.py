#!/usr/bin/env python3
"""LoopX controller 命令协调层。

 核心文件只保留中段流程命令（状态/校验/推进/写入保护/产物导入）；
 需求输入命令在 ``loopx_controller_intake.py``，CLI 装配在
 ``loopx_controller_cli.py``，返工和收口在各自专用模块。
"""

import copy
from pathlib import Path, PurePosixPath

from loopx_controller_contracts import (
    CONFIRMATION_GATE_STAGES,
    DEFAULT_STAGE_OWNERS,
    PASSING_STATUSES,
    STAGES,
)
from loopx_controller_flow import (
    advance_blockers,
    business_write_blockers,
    confirm_stage,
    default_next_stage,
    record_prepared_v2_stage_result,
    record_stage_result,
    stage_can_be_skipped,
)
from loopx_controller_io import (
    append_event,
    atomic_write_texts,
    event_line,
    get_run_dir,
    json_text,
    load_state,
    load_worklist,
    project_path,
    save_state,
)
from loopx_controller_evidence import parse_artifact_arguments
from loopx_controller_policy import (
    is_v2_run,
    load_policy_snapshot,
    reselect_policy_snapshot,
)
from loopx_controller_state import (
    mode_decision_state,
    mode_rank,
    resolve_run_id,
    start_stage_timing,
    update_worklist_state,
    update_worklist_state_data,
)
from loopx_controller_yaml import dump_worklist


def cmd_status(args, stdout):
    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
        state = load_state(project, run_id)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=stdout)
        return 1
    if args.tracking:
        from loopx_controller_state import format_tracking

        print(format_tracking(state), end="", file=stdout)
        return 0
    print(f"运行 ID：{state.get('run_id')}", file=stdout)
    print(f"执行等级：{state.get('mode')}", file=stdout)
    print(f"运行状态：{state.get('status')}", file=stdout)
    print(f"当前阶段：{state.get('current_stage')}", file=stdout)
    print(f"下一步：{state.get('next_action', 'validate')}", file=stdout)
    return 0


def cmd_validate(args, stdout):
    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
    except ValueError as exc:
        print(str(exc), file=stdout)
        return 1
    from loopx_controller_validation import validate_run

    errors = validate_run(project, run_id, strict=args.strict)
    if errors:
        print(f"FAIL 运行检查未通过：{run_id}", file=stdout)
        for error in errors:
            print(f"- {error}", file=stdout)
        return 1
    print(f"PASS 运行检查通过：{run_id}", file=stdout)
    return 0


def cmd_gate(args, stdout):
    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
    except ValueError as exc:
        print(str(exc), file=stdout)
        return 1
    from loopx_controller_validation import validate_run

    errors = validate_run(project, run_id, strict=True)
    if errors:
        print(f"FAIL 流程检查未通过：{run_id}", file=stdout)
        print("严格检查：FAIL", file=stdout)
        for error in errors:
            print(f"- {error}", file=stdout)
        return 1
    print(f"PASS 流程检查通过：{run_id}", file=stdout)
    print("严格检查：PASS", file=stdout)
    return 0


def cmd_mode(args, stdout):
    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
        state = load_state(project, run_id)
    except ValueError as exc:
        print(str(exc), file=stdout)
        return 1
    spec_review_status = state.get("stages", {}).get("spec_review")
    if spec_review_status not in PASSING_STATUSES and not (
        spec_review_status == "SKIPPED" and stage_can_be_skipped("spec_review", state)
    ):
        print("FAIL 执行等级选择被阻止", file=stdout)
        print("- 进入 mode_selection 前，spec_review 必须为 PASS", file=stdout)
        return 1
    if is_v2_run(state) and state.get("current_stage") != "mode_selection":
        print("FAIL 执行等级选择被阻止", file=stdout)
        print(f"- v2 只能在 mode_selection 阶段选择执行等级；当前阶段为 {state.get('current_stage')}", file=stdout)
        return 1
    selected = args.select
    new_state = copy.deepcopy(state)
    decision = new_state.setdefault("mode_decision", mode_decision_state(state.get("mode", selected), state.get("risk_tags", []), "auto"))
    recommended = decision.get("recommended") or state.get("mode")
    downgraded = mode_rank(recommended) > mode_rank(selected)
    if downgraded and not args.accepted_risk:
        print("FAIL 执行等级选择被阻止", file=stdout)
        print("- 选择低于建议的执行等级时，必须说明接受风险的理由", file=stdout)
        return 1
    new_state["mode"] = selected
    new_state["current_stage"] = "mode_selection"
    start_stage_timing(new_state, "mode_selection")
    new_state["active_agent"] = new_state.get("stage_owners", DEFAULT_STAGE_OWNERS)["mode_selection"]
    new_state["next_action"] = "solution_design"
    decision["selected"] = selected
    decision["selection_status"] = "CONFIRMED"
    decision["selected_by"] = "user"
    decision.setdefault("accepted_risk", {})
    decision["accepted_risk"]["selected_lower_than_recommended"] = downgraded
    decision["accepted_risk"]["reason"] = args.accepted_risk or ""
    status = "ACCEPTED_RISK" if downgraded else "PASS"
    if is_v2_run(state):
        evidence_path = f"docs/loopx/runs/{run_id}/artifacts/mode-decision.json"
        try:
            snapshot = reselect_policy_snapshot(load_policy_snapshot(project, state), selected)
            new_state["policy_snapshot_sha256"] = snapshot["digest"]
            prepared = {
                "artifacts": [],
                "rule_results": [],
                "evidence": [evidence_path],
                "solution_items": None,
            }
            record_prepared_v2_stage_result(
                project,
                run_id,
                new_state,
                "mode_selection",
                status,
                prepared,
                next_action="solution_design",
                extra_files={
                    project_path(project, new_state["policy_snapshot"]): json_text(snapshot),
                    project_path(project, evidence_path): json_text(decision),
                },
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print("FAIL 执行等级选择被阻止", file=stdout)
            print(f"- {exc}", file=stdout)
            return 1
    else:
        save_state(project, run_id, new_state)
        evidence = [args.accepted_risk] if args.accepted_risk else ["mode_decision"]
        record_stage_result(project, run_id, "mode_selection", status, evidence, next_action="solution_design")
        new_state = load_state(project, run_id)
        update_worklist_state(project, new_state, "mode_selection", status)
    print(f"已选择执行等级：{selected}", file=stdout)
    print(f"建议执行等级：{recommended}", file=stdout)
    print(f"阶段状态：{status}", file=stdout)
    return 0


def cmd_next(args, stdout):
    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
        state = load_state(project, run_id)
    except ValueError as exc:
        print(str(exc), file=stdout)
        return 1
    current = state.get("current_stage")
    if current not in STAGES:
        print(f"FAIL 当前阶段不是已知阶段：{current}", file=stdout)
        return 1
    return advance_to_stage(project, run_id, state, default_next_stage(current), stdout, fail_banner="FAIL 下一阶段推进被阻止")


def advance_to_stage(project, run_id, state, target, stdout, fail_banner="FAIL 阶段推进被阻止"):
    blockers = advance_blockers(project, run_id, state, target)
    if blockers:
        print(fail_banner, file=stdout)
        for blocker in blockers:
            print(f"- {blocker}", file=stdout)
        return 1
    new_state = copy.deepcopy(state)
    new_state["current_stage"] = target
    start_stage_timing(new_state, target)
    new_state["active_agent"] = new_state.get("stage_owners", DEFAULT_STAGE_OWNERS).get(target, target)
    new_state["next_action"] = default_next_stage(target)
    directory = get_run_dir(project, run_id)
    try:
        worklist_path, worklist = load_worklist(project, state)
        new_worklist = copy.deepcopy(worklist)
        update_worklist_state_data(new_worklist, new_state, target, "IN_PROGRESS")
        events_path = directory / "events.jsonl"
        try:
            old_events = events_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            old_events = ""
        atomic_write_texts({
            directory / "state.json": json_text(new_state),
            worklist_path: dump_worklist(new_worklist),
            events_path: old_events + event_line({"type": "advanced", "to": target}),
        })
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"{fail_banner}", file=stdout)
        print(f"- 阶段推进状态提交失败：{exc}", file=stdout)
        return 1
    print(f"PASS 已进入阶段：{target}", file=stdout)
    return 0


def cmd_record_stage(args, stdout):
    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
        state = load_state(project, run_id)
        if args.stage != state.get("current_stage"):
            raise ValueError(f"当前阶段为 {state.get('current_stage')}，不能记录阶段 {args.stage}")
        artifacts = parse_artifact_arguments(args.artifact) if is_v2_run(state) else {}
        if not is_v2_run(state) and args.artifact:
            raise ValueError("v1 历史运行不接受 --artifact；请继续使用原有 --evidence")
        result = record_stage_result(
            project,
            run_id,
            args.stage,
            args.status,
            args.evidence,
            return_to=args.return_to or "",
            next_action=args.next_action,
            affected_work_items=args.item or [],
            blocked_reason=args.blocked_reason or "",
            artifacts=artifacts,
        )
        state = load_state(project, run_id)
        if not is_v2_run(state):
            update_worklist_state(project, state, args.stage, result["status"])
    except OSError as exc:
        print(f"阶段记录写入失败：{exc}", file=stdout)
        return 1
    except RuntimeError as exc:
        print(f"阶段记录恢复失败：{exc}", file=stdout)
        return 1
    except ValueError as exc:
        print(str(exc), file=stdout)
        return 1
    print(f"{result['status']} 已记录阶段：{result['stage']}", file=stdout)
    print(f"下一步：{result['next_action']}", file=stdout)
    return 0


def cmd_advance(args, stdout):
    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
        state = load_state(project, run_id)
    except ValueError as exc:
        print(str(exc), file=stdout)
        return 1
    return advance_to_stage(project, run_id, state, args.to, stdout)


def cmd_can_write(args, stdout):
    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
        state = load_state(project, run_id)
    except ValueError as exc:
        print(str(exc), file=stdout)
        return 1
    if args.kind == "loopx":
        print("PASS 允许写入 LoopX 运行文件", file=stdout)
        return 0
    blockers = business_write_blockers(state, project, run_id)
    if blockers:
        print("FAIL 业务文件写入仍被锁定", file=stdout)
        for blocker in blockers:
            print(f"- {blocker}", file=stdout)
        return 1
    print("PASS 允许写入业务文件", file=stdout)
    return 0


def cmd_confirm_stage(args, stdout):
    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
        confirmation = confirm_stage(project, run_id, args.stage, args.evidence, args.confirmed_by)
    except ValueError as exc:
        print(str(exc), file=stdout)
        return 1
    print(f"PASS 已确认阶段：{args.stage}", file=stdout)
    print(f"确认人：{confirmation['confirmed_by']}", file=stdout)
    print(f"确认时间：{confirmation['confirmed_at']}", file=stdout)
    print(f"下一步：{CONFIRMATION_GATE_STAGES[args.stage]}", file=stdout)
    return 0


def cmd_health(args, stdout):
    # 健康执行器独立于 controller 核心，延迟导入允许 v1 工具继续单独使用。
    from loopx_health import execute_health

    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
        report = execute_health(project, run_id, write_result=True)
    except ValueError as exc:
        print(f"健康检查无法执行：{exc}", file=stdout)
        return 1
    print(f"健康检查结果：{report.status}", file=stdout)
    print(f"报告：docs/loopx/runs/{run_id}/artifacts/health-result.json", file=stdout)
    for check in report.checks:
        print(f"- [{check.status}] {check.name}：{check.message}", file=stdout)
    return 0 if report.status in {"PASS", "PASS_WITH_WARNINGS", "LOCAL_INCOMPLETE_CI_REQUIRED", "CI_REQUIRED"} else 1


def import_artifact_files(project, run_id, stage, values, backups=None):
    """把显式输入文件复制到当前运行，避免结构化控制产物留在项目目录。"""

    imported = parse_artifact_arguments(values)
    result = []
    for artifact_type, raw_path in imported.items():
        source = Path(raw_path)
        source = source if source.is_absolute() else project / source
        if source.is_symlink():
            raise ValueError(f"导入产物不能是符号链接：{raw_path}")
        try:
            source = source.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(f"导入产物不存在：{raw_path}") from exc
        if not source.is_file():
            raise ValueError(f"导入产物必须是普通文件：{raw_path}")
        relative = f"docs/loopx/runs/{run_id}/artifacts/imported/{stage}-{artifact_type}.json"
        target = project_path(project, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if backups is not None:
            backups.append((target, target.read_bytes() if target.is_file() else None))
        target.write_bytes(source.read_bytes())
        result.append(f"{artifact_type}={relative}")
    return result


def restore_imported_artifacts(backups):
    for target, previous in reversed(backups):
        if previous is None:
            target.unlink(missing_ok=True)
            try:
                target.parent.rmdir()
            except OSError:
                pass
        else:
            target.write_bytes(previous)


def cmd_import_artifact(args, stdout):
    """把 agent 或用户准备的文件收纳到运行产物区，不开放状态文件写入。"""

    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
        load_state(project, run_id)
        source_input = Path(args.source)
        source_candidate = source_input if source_input.is_absolute() else project / source_input
        if source_candidate.is_symlink():
            raise ValueError("导入源文件不能是符号链接")
        source = source_candidate.resolve(strict=True)
        if not source.is_file():
            raise ValueError("导入源必须是普通文件")
        raw_target = args.target.replace("\\", "/")
        relative = PurePosixPath(raw_target)
        if relative.is_absolute() or not relative.parts or relative.parts[0] != "artifacts":
            raise ValueError("导入目标必须位于 artifacts/ 下")
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("导入目标不能包含路径跳转")
        directory = get_run_dir(project, run_id).resolve(strict=True)
        target = directory.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.parent.resolve(strict=True).relative_to(directory)
        if target.is_symlink():
            raise ValueError("导入目标不能是符号链接")
        target.write_bytes(source.read_bytes())
        append_event(directory, {
            "type": "artifact_imported",
            "artifact": f"docs/loopx/runs/{run_id}/{relative.as_posix()}",
        })
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"FAIL 运行产物导入失败：{exc}", file=stdout)
        return 1
    print(f"PASS 已导入运行产物：docs/loopx/runs/{run_id}/{relative.as_posix()}", file=stdout)
    return 0
