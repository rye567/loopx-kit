#!/usr/bin/env python3
"""LoopX 需求输入阶段命令（init / interview / spec）。

 这组命令只负责需求侧产物的生成与初始化，依赖底层 io/state/flow/artifacts
 模块，不反向依赖 controller 核心，避免形成环。
"""

import json
from datetime import datetime
from pathlib import Path

from loopx_controller_artifacts import (
    interview_has_unanswered_placeholders,
    interview_questions,
    render_interview_artifact,
    render_spec_artifact,
)
from loopx_controller_contracts import (
    DEFAULT_STAGE_OWNERS,
    PASSING_STATUSES,
)
from loopx_controller_flow import (
    build_stage_result,
    is_waiting_confirmation,
    pending_confirmation_message,
    stage_result_path,
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
    source_snapshot_id,
    write_json,
)
from loopx_controller_policy import (
    CONTRACT_VERSION,
    build_policy_snapshot,
    is_v2_run,
    policy_snapshot_relative_path,
)
from loopx_controller_state import (
    default_run_id,
    interview_state,
    mode_decision_state,
    render_worklist,
    resolve_mode,
    resolve_run_id,
    spec_state,
    start_stage_timing,
    finish_stage_timing,
    tracking_state,
    update_worklist_state,
    update_worklist_state_data,
)
from loopx_controller_yaml import dump_worklist, parse_yaml_subset


def auto_pass_environment_check(project, run_id):
    directory = get_run_dir(project, run_id)
    state = load_state(project, run_id)
    worklist_path, worklist = load_worklist(project, state)
    next_action = "requirement_intake"
    evidence = [
        "LoopX controller initialized run state",
        "Project root resolved",
        "Python controller runtime available",
    ]
    if is_v2_run(state):
        relative = f"docs/loopx/runs/{run_id}/artifacts/environment-check.txt"
        artifact = project_path(project, relative)
        evidence = [relative]
    finish_stage_timing(state, "environment_check")
    result = build_stage_result(
        state,
        "environment_check",
        "PASS",
        "PASS",
        "",
        next_action,
        evidence,
        [],
        "",
    )
    state.setdefault("stages", {})["environment_check"] = "PASS"
    state["current_stage"] = "requirement_intake"
    state["active_agent"] = state.get("stage_owners", DEFAULT_STAGE_OWNERS)["requirement_intake"]
    state["next_action"] = next_action
    update_worklist_state_data(worklist, state, "environment_check", "PASS")
    events_path = directory / "events.jsonl"
    try:
        old_events = events_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        old_events = ""
    files = {
        stage_result_path(directory, "environment_check"): json_text(result),
        directory / "state.json": json_text(state),
        worklist_path: dump_worklist(worklist),
        events_path: old_events + event_line({
        "type": "stage_auto_passed",
        "stage": "environment_check",
        "next_action": next_action,
        }),
    }
    if is_v2_run(state):
        files[artifact] = "LoopX 控制器、项目根目录和 Python 运行时已就绪。\n"
    atomic_write_texts(files)
    return result


def cmd_init(args, stdout):
    project = Path(args.project).resolve()
    run_id = args.run_id or default_run_id(args.requirement)
    risk_tags = args.risk_tags or []
    mode = resolve_mode(args.mode, risk_tags, project)
    directory = get_run_dir(project, run_id)
    if directory.exists():
        # 上次 init 若在原子提交前中断，可能只留下空目录骨架；
        # 没有任何文件时允许同 run_id 幂等重试，有文件则仍禁止覆盖。
        if any(path.is_file() for path in directory.rglob("*")):
            print(f"运行已存在：{run_id}", file=stdout)
            return 1
    try:
        policy_snapshot = build_policy_snapshot(project, mode, risk_tags)
    except ValueError as exc:
        print(f"无法初始化运行：{exc}", file=stdout)
        return 1
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "artifacts").mkdir(exist_ok=True)
    (directory / "stage-results").mkdir(exist_ok=True)
    (directory / "artifacts" / "repair-tickets").mkdir(exist_ok=True)

    state = {
        "run_id": run_id,
        "requirement": args.requirement,
        "mode": mode,
        "status": "ACTIVE",
        "current_stage": "environment_check",
        "next_action": "requirement_intake",
        "active_agent": DEFAULT_STAGE_OWNERS["environment_check"],
        "stage_owners": DEFAULT_STAGE_OWNERS,
        "risk_tags": risk_tags,
        "automation_policy": {
            "mode": args.automation_policy,
            "authorized_by": "user_cli" if args.automation_policy == "auto_until_blocked" else "",
            "authorized_at": datetime.now().astimezone().isoformat(timespec="seconds")
            if args.automation_policy == "auto_until_blocked" else "",
        },
        "max_auto_repair": 2,
        "worklist": f"docs/loopx/runs/{run_id}/worklist.yml",
        "events": f"docs/loopx/runs/{run_id}/events.jsonl",
        "repair_tickets": f"docs/loopx/runs/{run_id}/artifacts/repair-tickets",
        "loop_attempts": {},
        "stages": {},
        "stage_timing": {},
        "interview": interview_state(run_id),
        "spec": spec_state(run_id),
        "mode_decision": mode_decision_state(mode, risk_tags, "auto" if args.mode == "auto" else "user"),
        "tracking": tracking_state(run_id),
        "git_gate": {
            "status": "PENDING",
            "diff_summary": "",
        },
        "contract_version": CONTRACT_VERSION,
        "catalog_version": policy_snapshot["catalog_version"],
        "policy_snapshot": policy_snapshot_relative_path(run_id),
        "policy_snapshot_sha256": policy_snapshot["digest"],
        "source_baseline": source_snapshot_id(project),
    }
    start_stage_timing(state, "environment_check")
    environment_relative = f"docs/loopx/runs/{run_id}/artifacts/environment-check.txt"
    environment_evidence = [environment_relative] if is_v2_run(state) else [
        "LoopX controller initialized run state",
        "Project root resolved",
        "Python controller runtime available",
    ]
    finish_stage_timing(state, "environment_check")
    environment_result = build_stage_result(
        state,
        "environment_check",
        "PASS",
        "PASS",
        "",
        "requirement_intake",
        environment_evidence,
        [],
        "",
    )
    state.setdefault("stages", {})["environment_check"] = "PASS"
    state["current_stage"] = "requirement_intake"
    state["active_agent"] = DEFAULT_STAGE_OWNERS["requirement_intake"]
    state["next_action"] = "requirement_intake"
    worklist = parse_yaml_subset(render_worklist(run_id, args.requirement, mode))
    update_worklist_state_data(worklist, state, "environment_check", "PASS")
    event = {"type": "run_created", "run_id": run_id, "current_stage": "environment_check"}
    files = {
        directory / "artifacts" / "policy-snapshot.json": json_text(policy_snapshot),
        directory / "artifacts" / "environment-check.txt": (
            "LoopX 控制器、项目根目录和 Python 运行时已就绪。\n"
        ),
        stage_result_path(directory, "environment_check"): json_text(environment_result),
        directory / "state.json": json_text(state),
        directory / "worklist.yml": dump_worklist(worklist),
        directory / "events.jsonl": (
            json.dumps(event, ensure_ascii=False) + "\n"
            + event_line({
                "type": "stage_auto_passed",
                "stage": "environment_check",
                "next_action": "requirement_intake",
            })
        ),
    }
    atomic_write_texts(files)
    print(f"PASS 已创建运行：{run_id}", file=stdout)
    print(f"执行等级：{mode}", file=stdout)
    print(f"建议执行等级：{mode}", file=stdout)
    print("环境检查（environment_check）：PASS", file=stdout)
    if args.mode == "auto":
        print("执行等级选择：需要用户确认（NEED_HUMAN）", file=stdout)
    print(f"自动化策略：{args.automation_policy}", file=stdout)
    print(f"状态文件：{state['worklist'].rsplit('/', 1)[0]}/state.json", file=stdout)
    return 0


def cmd_interview(args, stdout):
    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
        state = load_state(project, run_id)
    except ValueError as exc:
        print(str(exc), file=stdout)
        return 1
    artifact = project_path(project, state.setdefault("interview", interview_state(run_id)).get("artifact"))
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(render_interview_artifact(state), encoding="utf-8")
    questions = interview_questions(state)
    state["current_stage"] = "requirement_interview"
    start_stage_timing(state, "requirement_interview")
    state["active_agent"] = state.get("stage_owners", DEFAULT_STAGE_OWNERS)["requirement_interview"]
    state["next_action"] = "answer interview questions"
    state["interview"]["status"] = "IN_PROGRESS"
    state["interview"]["unanswered_questions"] = len(questions)
    state["interview"]["blocking_questions"] = [item["question"] for item in questions]
    save_state(project, run_id, state)
    update_worklist_state(project, state, "requirement_interview", "IN_PROGRESS")
    append_event(get_run_dir(project, run_id), {"type": "artifact_generated", "stage": "requirement_interview", "artifact": state["interview"]["artifact"]})
    print(f"已生成需求采访：{state['interview']['artifact']}", file=stdout)
    print("请回答以下需求采访问题：", file=stdout)
    for index, item in enumerate(questions, start=1):
        print(f"问题 {index}：{item['question']}", file=stdout)
        print(f"  为什么需要：{item['reason']}", file=stdout)
    print("回答后请更新 interview.md，再记录 requirement_interview 为 PASS；控制器随后等待用户确认（NEED_HUMAN）。", file=stdout)
    print("当前阶段：requirement_interview", file=stdout)
    return 0


def cmd_spec(args, stdout):
    project = Path(args.project).resolve()
    try:
        run_id = resolve_run_id(project, args.run_id)
        state = load_state(project, run_id)
    except ValueError as exc:
        print(str(exc), file=stdout)
        return 1
    interview_status = state.get("stages", {}).get("requirement_interview")
    if is_waiting_confirmation("requirement_interview", interview_status):
        print("FAIL 需求规格生成被阻止", file=stdout)
        print(f"- {pending_confirmation_message('requirement_interview')}", file=stdout)
        return 1
    if interview_status not in PASSING_STATUSES:
        print("FAIL 需求规格生成被阻止", file=stdout)
        print("- 进入 spec_draft 前，requirement_interview 必须为 PASS", file=stdout)
        return 1
    interview_artifact = project_path(project, state.setdefault("interview", interview_state(run_id)).get("artifact"))
    if not interview_artifact.exists():
        print("FAIL 需求规格生成被阻止", file=stdout)
        print(f"- 缺少需求采访产物：{state['interview']['artifact']}", file=stdout)
        return 1
    interview_text = interview_artifact.read_text(encoding="utf-8")
    if state.get("interview", {}).get("unanswered_questions", 0) != 0 or interview_has_unanswered_placeholders(interview_text):
        print("FAIL 需求规格生成被阻止", file=stdout)
        print("- 进入 spec_draft 前必须回答全部需求采访问题", file=stdout)
        return 1
    spec = state.setdefault("spec", spec_state(run_id))
    artifact = project_path(project, spec.get("artifact"))
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(render_spec_artifact(state), encoding="utf-8")
    manifest = project_path(project, spec.setdefault("extensions", {}).setdefault(
        "requirement_manifest",
        f"docs/loopx/runs/{run_id}/artifacts/requirement-manifest.json",
    ))
    if not manifest.exists():
        write_json(manifest, {
            "version": "1",
            "requirement_ids": [],
            "acceptance_ids": [],
            "delivery_units": [],
            "deferred": [],
            "delivery_strategy": "SINGLE_RUN",
            "coupled_reason": "",
        })
    spec["extensions"].pop("requirement_manifest_sha256", None)
    spec["extensions"].pop("spec_artifact_sha256", None)
    state["current_stage"] = "spec_draft"
    start_stage_timing(state, "spec_draft")
    state["active_agent"] = state.get("stage_owners", DEFAULT_STAGE_OWNERS)["spec_draft"]
    state["next_action"] = "record-stage --stage spec_draft --status PASS"
    spec["status"] = "DRAFT"
    spec["approved"] = False
    spec["gate_result"] = "PENDING"
    save_state(project, run_id, state)
    update_worklist_state(project, state, "spec_draft", "IN_PROGRESS")
    append_event(get_run_dir(project, run_id), {"type": "artifact_generated", "stage": "spec_draft", "artifact": spec["artifact"]})
    print(f"已生成需求规格：{spec['artifact']}", file=stdout)
    print(f"需求全集清单：{spec['extensions']['requirement_manifest']}", file=stdout)
    print("当前阶段：spec_draft", file=stdout)
    return 0
