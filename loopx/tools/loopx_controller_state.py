#!/usr/bin/env python3
"""LoopX run state and tracking helpers.

本模块只负责构造和展示状态，不直接推进阶段；阶段流转统一放在
loopx_controller_flow.py，避免写入规则散在多个文件里。
"""

import re
from datetime import datetime

from loopx_controller_contracts import (
    PASSING_STATUSES,
    STAGE_DISPLAY_NAMES,
    STAGE_RESULT_FILES,
    STAGE_SEQUENCE,
)
from loopx_controller_io import load_worklist, loopx_root, run_root
from loopx_controller_store import external_runs
from loopx_controller_yaml import YamlSubsetError, dump_worklist, parse_yaml_subset, yaml_string


def slugify(text, max_length=48):
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_length] or "loopx-run"


def default_run_id(requirement):
    return f"{datetime.now().strftime('%Y-%m-%d')}-{slugify(requirement)}"


def render_worklist(run_id, requirement, mode):
    stage_lines = []
    for index, stage in enumerate(STAGE_SEQUENCE):
        stage_lines.extend([
            f"  - id: {yaml_string(f'{index:02d}')}",
            f"    stage: {stage}",
            f"    name: {STAGE_DISPLAY_NAMES[stage]}",
            "    status: PENDING",
            "    required: true",
            "    evidence: \"\"",
        ])
    return f"""run:
  id: {yaml_string(run_id)}
  requirement: {yaml_string(requirement)}
  mode: {mode}
  status: ACTIVE
  current_stage: environment_check
  next_action: requirement_intake

spec:
  status: NOT_CREATED
  path: {yaml_string(f"docs/loopx/runs/{run_id}/artifacts/spec.md")}
  approved: false

interview:
  status: NOT_STARTED
  unanswered_questions: 0
  path: {yaml_string(f"docs/loopx/runs/{run_id}/artifacts/interview.md")}

stages:
{chr(10).join(stage_lines)}

items: []
"""


def risk_config(project=None):
    if project is not None:
        local = project / "loopx" / "risk.yml"
        if local.is_file():
            return parse_yaml_subset(local.read_text(encoding="utf-8"))
    return parse_yaml_subset((loopx_root() / "risk.yml").read_text(encoding="utf-8"))


def resolve_mode(mode, risk_tags, project=None):
    if mode != "auto":
        return mode
    config = risk_config(project)
    critical = set(config.get("critical_triggers", []))
    score_rules = config.get("score_rules", {})
    thresholds = config.get("thresholds", {})
    if critical.intersection(risk_tags):
        selected = "FULL"
    else:
        score = sum(int(score_rules.get(tag, 0)) for tag in risk_tags)
        if score >= int(thresholds.get("full_min", 6)):
            selected = "FULL"
        elif score <= int(thresholds.get("light_max", 1)):
            selected = "LIGHT"
        else:
            selected = "STANDARD"
    ranks = {"LIGHT": 1, "STANDARD": 2, "FULL": 3}
    profiles = config.get("risk_profiles") or {}
    for tag in risk_tags:
        minimum = (profiles.get(tag) or {}).get("minimum_mode")
        if ranks.get(minimum, 0) > ranks[selected]:
            selected = minimum
    return selected


def mode_rank(mode):
    return {"LIGHT": 1, "STANDARD": 2, "FULL": 3}.get(mode, 0)


def interview_state(run_id):
    return {
        "status": "NOT_STARTED",
        "artifact": f"docs/loopx/runs/{run_id}/artifacts/interview.md",
        "unanswered_questions": 0,
    }


def spec_state(run_id):
    return {
        "status": "NOT_CREATED",
        "artifact": f"docs/loopx/runs/{run_id}/artifacts/spec.md",
        "approved": False,
        "gate_result": "PENDING",
        "extensions": {
            "requirement_manifest": f"docs/loopx/runs/{run_id}/artifacts/requirement-manifest.json",
        },
    }


def mode_decision_state(mode, risk_tags, selected_by):
    confirmed = selected_by != "auto"
    return {
        "recommended": mode,
        "selected": mode if confirmed else "",
        "selection_status": "CONFIRMED" if confirmed else "NEED_HUMAN",
        "selected_by": selected_by,
        "reason": risk_tags,
        "accepted_risk": {
            "selected_lower_than_recommended": False,
            "reason": "",
        },
    }


def tracking_state(run_id):
    return {
        "worklist": f"docs/loopx/runs/{run_id}/worklist.yml",
    }


def start_stage_timing(state, stage):
    """记录阶段开始时间；重复进入同一阶段会增加 attempt。"""

    timing = state.setdefault("stage_timing", {}).setdefault(stage, {})
    timing["started_at"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
    timing.pop("finished_at", None)
    timing.pop("duration_ms", None)
    timing["attempt"] = int(timing.get("attempt") or 0) + 1
    return timing


def finish_stage_timing(state, stage):
    timing = state.setdefault("stage_timing", {}).setdefault(stage, {})
    # BLOCKED/CHANGES_REQUIRED 后直接重录当前阶段时，应开启新 attempt，
    # 不能把人工等待或返工间隔累计到上一次执行时长。
    if timing.get("finished_at"):
        timing = start_stage_timing(state, stage)
    finished = datetime.now().astimezone()
    started_raw = timing.get("started_at")
    if started_raw:
        started = datetime.fromisoformat(started_raw)
    else:
        started = finished
        timing["started_at"] = finished.isoformat(timespec="milliseconds")
        timing["attempt"] = int(timing.get("attempt") or 0) + 1
    timing["finished_at"] = finished.isoformat(timespec="milliseconds")
    timing["duration_ms"] = max(0, int((finished - started).total_seconds() * 1000))
    return dict(timing)


def update_worklist_state(project, state, stage=None, stage_status=None):
    # worklist 是给人和 agent 看的同步视图，真实状态仍以 state.json 为准。
    try:
        worklist_path, worklist = load_worklist(project, state)
    except (FileNotFoundError, YamlSubsetError):
        return
    update_worklist_state_data(worklist, state, stage, stage_status)
    worklist_path.write_text(dump_worklist(worklist), encoding="utf-8")


def update_worklist_state_data(worklist, state, stage=None, stage_status=None):
    """只更新内存中的 worklist；v2 用它在统一提交前完成全部计算。"""

    worklist.setdefault("run", {})["current_stage"] = state.get("current_stage")
    worklist["run"]["mode"] = state.get("mode", "")
    worklist["run"]["status"] = state.get("status", "ACTIVE")
    worklist["run"]["next_action"] = state.get("next_action", "")
    if "spec" in state:
        worklist["spec"] = {
            "status": state["spec"].get("status", ""),
            "path": state["spec"].get("artifact", ""),
            "approved": state["spec"].get("approved", False),
        }
    if "interview" in state:
        worklist["interview"] = {
            "status": state["interview"].get("status", ""),
            "unanswered_questions": state["interview"].get("unanswered_questions", 0),
            "path": state["interview"].get("artifact", ""),
        }
    if stage and "stages" in worklist:
        for item in worklist.get("stages") or []:
            if item.get("stage") == stage:
                item["status"] = stage_status or item.get("status", "PENDING")
                artifact = ""
                if stage == "requirement_interview":
                    artifact = state.get("interview", {}).get("artifact", "")
                if stage in {"spec_draft", "spec_review"}:
                    artifact = state.get("spec", {}).get("artifact", "")
                if not artifact and stage in STAGE_RESULT_FILES:
                    artifact = f"docs/loopx/runs/{state.get('run_id')}/stage-results/{STAGE_RESULT_FILES[stage]}"
                item["evidence"] = artifact or item.get("evidence", "")
    return worklist


def build_tracking_snapshot(state):
    current = state.get("current_stage", "")
    completed = state.get("stages", {})
    snapshot = []
    for index, stage in enumerate(STAGE_SEQUENCE):
        snapshot.append({
            "id": f"{index:02d}",
            "stage": stage,
            "name": STAGE_DISPLAY_NAMES[stage],
            "status": completed.get(stage, "IN_PROGRESS" if stage == current else "PENDING"),
        })
    return snapshot


def format_tracking(state):
    lines = [
        "LoopX 追踪",
        "",
        f"运行: {state.get('run_id')}",
        f"模式: {state.get('mode')}",
        f"当前阶段: {STAGE_DISPLAY_NAMES.get(state.get('current_stage'), state.get('current_stage'))}",
        f"需求规格: {state.get('spec', {}).get('status', 'UNKNOWN')}",
        "Git 检查: PENDING",
        "",
        "阶段:",
    ]
    current = state.get("current_stage")
    statuses = state.get("stages", {})
    for index, stage in enumerate(STAGE_SEQUENCE):
        status = statuses.get(stage)
        marker = "[>]" if stage == current else "[x]" if status in PASSING_STATUSES else "[ ]"
        lines.append(f"{marker} {index:02d} {STAGE_DISPLAY_NAMES[stage]}")
    return "\n".join(lines) + "\n"


def latest_run_id(project):
    root = run_root(project)
    candidates = []
    if root.exists():
        candidates.extend((path.name, path.stat().st_mtime) for path in root.iterdir() if path.is_dir())
    candidates.extend(external_runs(project))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[1])[0]


def resolve_run_id(project, run_id):
    if run_id:
        return run_id
    resolved = latest_run_id(project)
    if not resolved:
        raise ValueError("没有可用的 LoopX 运行记录")
    return resolved
