import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = ROOT / "loopx" / "tools" / "loopx_controller.py"


def load_controller_module():
    spec = importlib.util.spec_from_file_location("loopx_controller", CONTROLLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LoopxControllerTest(unittest.TestCase):
    def setUp(self):
        self.backend_patch = mock.patch.dict(os.environ, {"LOOPX_STATE_BACKEND": "project"})
        self.backend_patch.start()
        self.addCleanup(self.backend_patch.stop)
        self.controller = load_controller_module()
        self.tmp = Path(tempfile.mkdtemp(prefix="loopx-controller-test-"))
        # 既有控制器测试固定覆盖“无 contract_version 的历史运行”。新建 v2
        # 运行及结构化证据行为由 test_loopx_evidence.py 单独覆盖。
        original_main = self.controller.main

        def legacy_main(argv=None, stdout=None):
            code = original_main(argv, stdout)
            if code == 0 and argv and argv[0] == "init":
                run_id = argv[argv.index("--run-id") + 1]
                run_dir = self.run_dir(run_id)
                state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
                for key in ("contract_version", "catalog_version", "policy_snapshot", "policy_snapshot_sha256"):
                    state.pop(key, None)
                (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                environment = run_dir / "stage-results" / "00-environment-check.json"
                result = json.loads(environment.read_text(encoding="utf-8"))
                for key in ("contract_version", "artifacts", "rule_results"):
                    result.pop(key, None)
                environment.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            return code

        self.controller.main = legacy_main

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_dir(self, run_id):
        return self.tmp / "docs" / "loopx" / "runs" / run_id

    def read_state(self, run_id):
        return json.loads((self.run_dir(run_id) / "state.json").read_text(encoding="utf-8"))

    def write_state(self, run_id, state):
        (self.run_dir(run_id) / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def force_current_stage(self, run_id, stage):
        state = self.read_state(run_id)
        state["current_stage"] = stage
        self.write_state(run_id, state)

    def answer_interview_artifact(self, run_id):
        artifact = self.run_dir(run_id) / "artifacts" / "interview.md"
        artifact.write_text("""# 需求采访

## 运行信息

- 运行 ID：{run_id}

## 已确认事实

- 用户确认了要解决的问题、期望行为、验收标准和范围边界。

## 采访问题

| 优先级 | 问题 | 为什么需要 | 阻塞阶段 |
|---|---|---|---|
| P0 | 这个需求要解决的具体问题是什么？ | 确认问题定义 | spec_draft |

## 回答记录

- 问题：这个需求要解决的具体问题是什么？
  回答：解决指定需求，并以可验证验收标准收口。
  状态：已确认
- 问题：验收标准是什么？
  回答：核心行为可由测试或明确检查证明。
  状态：已确认
- 问题：哪些内容不在本次范围内？
  回答：未声明的新框架和无关重构不在范围内。
  状态：已确认

## 开放问题

- 阻塞问题：无
- 非阻塞问题：无
""".format(run_id=run_id), encoding="utf-8")

    def force_interview_pass_state(self, run_id, unanswered_questions):
        run_dir = self.run_dir(run_id)
        state = self.read_state(run_id)
        state.setdefault("stages", {})["requirement_interview"] = "PASS"
        state.setdefault("interview", {})["status"] = "PASS"
        state["interview"]["unanswered_questions"] = unanswered_questions
        self.write_state(run_id, state)
        result = self.controller.build_stage_result(
            state,
            "requirement_interview",
            "PASS",
            "PASS",
            "",
            "spec_draft",
            [f"docs/loopx/runs/{run_id}/artifacts/interview.md"],
            [],
            "",
        )
        (run_dir / "stage-results" / "02-requirement-interview.json").write_text(
            json.dumps(result, ensure_ascii=False),
            encoding="utf-8",
        )
        self.controller.update_worklist_state(self.tmp, state, "requirement_interview", "PASS")

    def test_init_creates_run_state_worklist_and_events(self):
        out = io.StringIO()
        code = self.controller.main([
            "init",
            "Add state driven LoopX controller",
            "--run-id",
            "2026-05-15-controller",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "2026-05-15-controller"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(state["run_id"], "2026-05-15-controller")
        self.assertEqual(state["mode"], "LIGHT")
        self.assertEqual(state["risk_tags"], [])
        self.assertEqual(state["current_stage"], "requirement_intake")
        self.assertEqual(state["active_agent"], "project-manager")
        self.assertEqual(state["stages"]["environment_check"], "PASS")
        self.assertEqual(state["stage_owners"]["solution_design"], "solution-designer")
        self.assertEqual(state["repair_tickets"], "docs/loopx/runs/2026-05-15-controller/artifacts/repair-tickets")
        self.assertEqual(state["worklist"], "docs/loopx/runs/2026-05-15-controller/worklist.yml")
        stage_result = json.loads((run_dir / "stage-results" / "00-environment-check.json").read_text(encoding="utf-8"))
        self.assertEqual(stage_result["stage"], "environment_check")
        self.assertEqual(stage_result["status"], "PASS")
        self.assertFalse(stage_result["user_confirmation_required"])
        self.assertTrue((run_dir / "worklist.yml").exists())
        self.assertTrue((run_dir / "artifacts" / "repair-tickets").exists())
        self.assertTrue((run_dir / "events.jsonl").exists())
        self.assertIn("PASS 已创建运行：2026-05-15-controller", out.getvalue())

    def test_init_creates_interview_spec_mode_and_tracking_metadata(self):
        out = io.StringIO()
        code = self.controller.main([
            "init",
            "Require interview before coding",
            "--run-id",
            "front-gates-run",
            "--mode",
            "auto",
            "--risk-tags",
            "api_contract",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "front-gates-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        worklist = (run_dir / "worklist.yml").read_text(encoding="utf-8")

        self.assertTrue((run_dir / "artifacts").exists())
        self.assertEqual(state["interview"]["status"], "NOT_STARTED")
        self.assertEqual(state["interview"]["artifact"], "docs/loopx/runs/front-gates-run/artifacts/interview.md")
        self.assertEqual(state["spec"]["status"], "NOT_CREATED")
        self.assertEqual(state["spec"]["artifact"], "docs/loopx/runs/front-gates-run/artifacts/spec.md")
        self.assertEqual(state["mode_decision"]["recommended"], "STANDARD")
        self.assertEqual(state["mode_decision"]["selected"], "")
        self.assertEqual(state["mode_decision"]["selection_status"], "NEED_HUMAN")
        self.assertEqual(state["mode_decision"]["selected_by"], "auto")
        self.assertEqual(state["interview"]["status"], "NOT_STARTED")
        self.assertIn("spec:", worklist)
        self.assertIn("interview:", worklist)
        self.assertIn("stages:", worklist)
        self.assertIn('name: "需求采访"', worklist)
        self.assertIn('name: "执行等级选择"', worklist)

    def test_init_auto_mode_uses_risk_tags_to_select_full(self):
        out = io.StringIO()
        code = self.controller.main([
            "init",
            "Change tenant scoped API state",
            "--run-id",
            "risk-run",
            "--mode",
            "auto",
            "--risk-tags",
            "tenant_scope",
            "core_state_transition",
            "api_contract",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        state = json.loads((self.tmp / "docs" / "loopx" / "runs" / "risk-run" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["mode"], "FULL")
        self.assertEqual(state["mode_decision"]["recommended"], "FULL")
        self.assertEqual(state["mode_decision"]["selected"], "")
        self.assertEqual(state["mode_decision"]["selection_status"], "NEED_HUMAN")
        self.assertEqual(state["risk_tags"], ["tenant_scope", "core_state_transition", "api_contract"])
        self.assertIn("建议执行等级：FULL", out.getvalue())

    def test_init_explicit_mode_confirms_user_selection(self):
        out = io.StringIO()
        code = self.controller.main([
            "init",
            "Explicit mode",
            "--run-id",
            "explicit-mode-run",
            "--mode",
            "STANDARD",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        state = json.loads((self.tmp / "docs" / "loopx" / "runs" / "explicit-mode-run" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["mode_decision"]["recommended"], "STANDARD")
        self.assertEqual(state["mode_decision"]["selected"], "STANDARD")
        self.assertEqual(state["mode_decision"]["selection_status"], "CONFIRMED")
        self.assertEqual(state["mode_decision"]["selected_by"], "user")

    def test_validate_rejects_worklist_items_missing_required_fields(self):
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "bad-run"
        run_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text(json.dumps({
            "run_id": "bad-run",
            "requirement": "Bad worklist",
            "mode": "STANDARD",
            "status": "ACTIVE",
            "current_stage": "environment_check",
            "max_auto_repair": 2,
            "worklist": "docs/loopx/runs/bad-run/worklist.yml",
            "events": "docs/loopx/runs/bad-run/events.jsonl",
            "stages": {},
        }), encoding="utf-8")
        (run_dir / "worklist.yml").write_text("""run:
  id: bad-run
  requirement: Bad worklist
  mode: STANDARD
  status: ACTIVE
  current_stage: environment_check
items:
  - id: W001
    title: Missing owner agent
    status: TODO
""", encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "bad-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("items[0].owner_agent 为必填项", out.getvalue())

    def test_status_uses_latest_run_when_run_id_is_omitted(self):
        self.controller.main([
            "init",
            "Check status",
            "--run-id",
            "status-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main(["status", "--project", str(self.tmp)], stdout=out)

        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("运行 ID：status-run", text)
        self.assertIn("当前阶段：requirement_intake", text)

    def test_status_tracking_outputs_full_stage_list(self):
        self.controller.main([
            "init",
            "Show tracking",
            "--run-id",
            "tracking-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "status",
            "tracking-run",
            "--tracking",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("LoopX 追踪", text)
        self.assertIn("运行: tracking-run", text)
        self.assertIn("需求规格: NOT_CREATED", text)
        self.assertIn("[x] 00 环境检查", text)
        self.assertIn("[>] 01 需求接收", text)
        self.assertIn("[ ] 02 需求采访", text)
        self.assertIn("[ ] 05 执行等级选择", text)

    def test_validate_rejects_stage_result_missing_required_fields(self):
        self.controller.main([
            "init",
            "Check stage result schema",
            "--run-id",
            "stage-result-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        result_path = self.tmp / "docs" / "loopx" / "runs" / "stage-result-run" / "stage-results" / "01-requirement-intake.json"
        result_path.write_text(json.dumps({
            "stage": "requirement_intake",
            "status": "PASS",
            "return_to": "",
            "next_action": "solution_design",
            "affected_work_items": [],
            "user_confirmation_required": False,
            "blocked_reason": "",
        }), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "stage-result-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("01-requirement-intake.json.evidence 为必填项", out.getvalue())

    def test_strict_validate_rejects_missing_interview_spec_mode_or_tracking_metadata(self):
        self.controller.main([
            "init",
            "Strict metadata",
            "--run-id",
            "strict-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "strict-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        del state["interview"]
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "strict-run",
            "--strict",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("严格检查要求 state.interview 存在", out.getvalue())

    def test_strict_validate_applies_front_gate_schemas(self):
        self.controller.main([
            "init",
            "Strict schema contracts",
            "--run-id",
            "strict-schema-run",
            "--mode",
            "LIGHT",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "strict-schema-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        del state["interview"]["artifact"]
        state["tracking"]["worklist"] = 123
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "strict-schema-run",
            "--strict",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("state.interview.artifact 为必填项", out.getvalue())
        self.assertIn("state.tracking.worklist 必须是 string", out.getvalue())

    def test_gate_command_runs_strict_validation(self):
        self.controller.main([
            "init",
            "Run strict gate",
            "--run-id",
            "gate-run",
            "--mode",
            "LIGHT",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "gate",
            "gate-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        self.assertIn("PASS 流程检查通过：gate-run", out.getvalue())
        self.assertIn("严格检查", out.getvalue())

        run_dir = self.tmp / "docs" / "loopx" / "runs" / "gate-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        del state["tracking"]
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "gate",
            "gate-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("FAIL 流程检查未通过：gate-run", out.getvalue())
        self.assertIn("严格检查要求 state.tracking 存在", out.getvalue())

    def test_strict_validate_requires_git_gate_metadata(self):
        self.controller.main([
            "init",
            "Strict git gate",
            "--run-id",
            "strict-git-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "strict-git-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        del state["git_gate"]
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "strict-git-run",
            "--strict",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("严格检查要求 state.git_gate 存在", out.getvalue())

    def test_strict_validate_rejects_skipped_full_mode_required_stages(self):
        self.controller.main([
            "init",
            "Strict full mode",
            "--run-id",
            "strict-full-run",
            "--mode",
            "FULL",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "strict-full-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["stages"]["solution_review"] = "SKIPPED"
        state["stages"]["test_review"] = "SKIPPED"
        state["stages"]["health_gate"] = "SKIPPED"
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "strict-full-run",
            "--strict",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("FULL 执行等级不允许将阶段 solution_review 设为 SKIPPED", out.getvalue())
        self.assertIn("FULL 执行等级不允许将阶段 test_review 设为 SKIPPED", out.getvalue())
        self.assertIn("FULL 执行等级不允许将阶段 health_gate 设为 SKIPPED", out.getvalue())

    def test_strict_validate_rejects_interview_pass_with_unanswered_questions(self):
        self.controller.main([
            "init",
            "Strict interview gate",
            "--run-id",
            "strict-interview-run",
            "--mode",
            "LIGHT",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        self.controller.main([
            "interview",
            "strict-interview-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        self.force_interview_pass_state("strict-interview-run", unanswered_questions=3)

        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "strict-interview-run",
            "--strict",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("requirement_interview 已通过时，state.interview.unanswered_questions 必须为 0", out.getvalue())

    def test_strict_validate_rejects_interview_pass_with_unanswered_placeholders(self):
        self.controller.main([
            "init",
            "Strict interview placeholders",
            "--run-id",
            "strict-interview-placeholder-run",
            "--mode",
            "LIGHT",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        self.controller.main([
            "interview",
            "strict-interview-placeholder-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        self.force_interview_pass_state("strict-interview-placeholder-run", unanswered_questions=0)

        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "strict-interview-placeholder-run",
            "--strict",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("interview.md 仍包含未回答占位内容", out.getvalue())

    def test_strict_validate_rejects_spec_review_pass_with_missing_required_sections(self):
        self.controller.main([
            "init",
            "Strict spec gate",
            "--run-id",
            "strict-spec-run",
            "--mode",
            "LIGHT",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "strict-spec-run"
        artifact = run_dir / "artifacts" / "spec.md"
        artifact.write_text("# Requirement Spec\n\n## Summary\n\nOnly a summary.\n", encoding="utf-8")
        self.force_current_stage("strict-spec-run", "spec_review")
        self.controller.main([
            "record-stage",
            "--run-id",
            "strict-spec-run",
            "--stage",
            "spec_review",
            "--status",
            "PASS",
            "--evidence",
            "docs/loopx/runs/strict-spec-run/artifacts/spec.md",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "strict-spec-run",
            "--strict",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("spec.md 缺少必需章节：Acceptance Criteria", out.getvalue())
        self.assertIn("spec.md 缺少必需章节：Test Strategy", out.getvalue())

    def test_strict_validate_rejects_spec_review_pass_with_empty_required_sections(self):
        self.controller.main([
            "init",
            "Strict spec content gate",
            "--run-id",
            "strict-empty-spec-run",
            "--mode",
            "LIGHT",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "strict-empty-spec-run"
        artifact = run_dir / "artifacts" / "spec.md"
        artifact.write_text("""# Requirement Spec

## Summary

Has summary.

## Expected Behavior

Expected.

## Acceptance Criteria

## Scope

In scope.

## Out of Scope

Out of scope.

## Edge Cases

Edges.

## Test Strategy

## Execution Mode

LIGHT.
""", encoding="utf-8")
        self.force_current_stage("strict-empty-spec-run", "spec_review")
        self.controller.main([
            "record-stage",
            "--run-id",
            "strict-empty-spec-run",
            "--stage",
            "spec_review",
            "--status",
            "PASS",
            "--evidence",
            "docs/loopx/runs/strict-empty-spec-run/artifacts/spec.md",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "strict-empty-spec-run",
            "--strict",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("spec.md 的必需章节为空：Acceptance Criteria", out.getvalue())
        self.assertIn("spec.md 的必需章节为空：Test Strategy", out.getvalue())

    def test_strict_validate_rejects_final_report_pass_without_git_gate_and_diff_summary(self):
        self.controller.main([
            "init",
            "Strict final report gate",
            "--run-id",
            "strict-final-run",
            "--mode",
            "LIGHT",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        self.force_current_stage("strict-final-run", "final_report")
        self.controller.main([
            "record-stage",
            "--run-id",
            "strict-final-run",
            "--stage",
            "final_report",
            "--status",
            "PASS",
            "--evidence",
            "docs/final.md",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "strict-final-run",
            "--strict",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("final_report 记录为 PASS 前，state.git_gate.status 必须为 PASS", out.getvalue())
        self.assertIn("final_report 记录为 PASS 前，必须填写 state.git_gate.diff_summary", out.getvalue())

        run_dir = self.tmp / "docs" / "loopx" / "runs" / "strict-final-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["git_gate"]["status"] = "PASS"
        state["git_gate"]["diff_summary"] = "M loopx/tools/loopx_controller.py"
        state["current_stage"] = "release_readiness"
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        self.controller.main([
            "record-stage",
            "--run-id",
            "strict-final-run",
            "--stage",
            "release_readiness",
            "--status",
            "PASS",
            "--evidence",
            "stage-results/15-release-readiness.json",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())


        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "strict-final-run",
            "--strict",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("final_report 记录为 PASS 前，state.compound_capture.decision 必须为 captured 或 skipped", out.getvalue())

        self.controller.main([
            "compound",
            "strict-final-run",
            "--decision",
            "skipped",
            "--reason",
            "最终报告收口测试不产生长期复用学习。",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "strict-final-run",
            "--strict",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)

    def test_git_gate_command_records_git_status_summary(self):
        subprocess.run(["git", "init"], cwd=self.tmp, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (self.tmp / "tracked-change.txt").write_text("changed\n", encoding="utf-8")
        self.controller.main([
            "init",
            "Git gate",
            "--run-id",
            "git-gate-run",
            "--mode",
            "LIGHT",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "git-gate",
            "git-gate-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        self.assertIn("PASS Git 变更检查通过：git-gate-run", out.getvalue())
        state = json.loads((self.tmp / "docs" / "loopx" / "runs" / "git-gate-run" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["git_gate"]["status"], "PASS")
        self.assertIn("tracked-change.txt", state["git_gate"]["diff_summary"])

    def test_git_gate_command_marks_need_human_outside_git_repo(self):
        self.controller.main([
            "init",
            "Git gate no repo",
            "--run-id",
            "git-gate-no-repo-run",
            "--mode",
            "LIGHT",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "git-gate",
            "git-gate-no-repo-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("NEED_HUMAN Git 变更检查需要用户处理：git-gate-no-repo-run", out.getvalue())
        state = json.loads((self.tmp / "docs" / "loopx" / "runs" / "git-gate-no-repo-run" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["git_gate"]["status"], "NEED_HUMAN")

    def test_compound_records_skip_decision_in_run_artifact(self):
        self.controller.main([
            "init",
            "Docs only update",
            "--run-id",
            "compound-skip-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "compound",
            "compound-skip-run",
            "--decision",
            "skipped",
            "--reason",
            "文档路径改名，没有可复用缺陷或流程经验。",
            "--project",
            str(self.tmp),
        ], stdout=out)

        artifact = self.run_dir("compound-skip-run") / "artifacts" / "compound-capture.md"
        state = self.read_state("compound-skip-run")
        self.assertEqual(code, 0)
        self.assertTrue(artifact.exists())
        self.assertIn("decision: skipped", artifact.read_text(encoding="utf-8"))
        self.assertIn("文档路径改名", artifact.read_text(encoding="utf-8"))
        self.assertEqual(state["compound_capture"]["decision"], "skipped")
        self.assertEqual(state["compound_capture"]["artifact"], "docs/loopx/runs/compound-skip-run/artifacts/compound-capture.md")
        self.assertEqual(state["compound_capture"]["project_doc"], "")
        self.assertFalse((self.tmp / "docs" / "loopx" / "solutions").exists())
        self.assertIn("PASS 已记录经验沉淀决定：compound-skip-run", out.getvalue())
        self.assertIn("决定：skipped", out.getvalue())

    def test_compound_writes_project_learning_doc_when_explicitly_enabled(self):
        self.controller.main([
            "init",
            "Prevent interview auto pass",
            "--run-id",
            "compound-captured-run",
            "--risk-tags",
            "api_contract",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "compound",
            "compound-captured-run",
            "--decision",
            "captured",
            "--category",
            "workflow",
            "--title",
            "Interview gate requires answered artifact",
            "--summary",
            "需求采访必须先展示问题并写入回答。",
            "--learning",
            "只生成 spec 文档会绕过需求确认，必须让 interview artifact 不含未回答占位。",
            "--prevention",
            "record-stage requirement_interview PASS 前校验 interview.md 的回答状态。",
            "--applies-to",
            "loopx/tools",
            "--write-project-doc",
            "--project",
            str(self.tmp),
        ], stdout=out)

        artifact = self.run_dir("compound-captured-run") / "artifacts" / "compound-capture.md"
        project_doc = self.tmp / "docs" / "loopx" / "solutions" / "workflow" / "interview-gate-requires-answered-artifact.md"
        state = self.read_state("compound-captured-run")
        self.assertEqual(code, 0)
        self.assertTrue(artifact.exists())
        self.assertTrue(project_doc.exists())
        text = project_doc.read_text(encoding="utf-8")
        self.assertIn("decision: captured", text)
        self.assertIn("risk_tags:", text)
        self.assertIn("  - api_contract", text)
        self.assertIn("applies_to:", text)
        self.assertIn("  - loopx/tools", text)
        self.assertIn("## 经验", text)
        self.assertEqual(state["compound_capture"]["decision"], "captured")
        self.assertEqual(state["compound_capture"]["project_doc"], "docs/loopx/solutions/workflow/interview-gate-requires-answered-artifact.md")
        self.assertIn("项目文档：docs/loopx/solutions/workflow/interview-gate-requires-answered-artifact.md", out.getvalue())

    def test_validate_learning_rejects_missing_required_frontmatter(self):
        bad_doc = self.tmp / "bad-learning.md"
        bad_doc.write_text("""---
run_id: bad-run
decision: captured
---

# Missing fields
""", encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "validate-learning",
            str(bad_doc),
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("FAIL 经验文档检查未通过", out.getvalue())
        self.assertIn("frontmatter.title 为必填项", out.getvalue())
        self.assertIn("frontmatter.summary 为必填项", out.getvalue())

    def test_strict_validate_rejects_worklist_stage_status_drift(self):
        self.controller.main([
            "init",
            "Strict tracking drift",
            "--run-id",
            "strict-tracking-run",
            "--mode",
            "LIGHT",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "strict-tracking-run"
        self.force_current_stage("strict-tracking-run", "solution_design")
        self.controller.main([
            "record-stage",
            "--run-id",
            "strict-tracking-run",
            "--stage",
            "solution_design",
            "--status",
            "PASS",
            "--evidence",
            "docs/solution.md",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        worklist_path = run_dir / "worklist.yml"
        worklist = worklist_path.read_text(encoding="utf-8")
        worklist = worklist.replace("stage: solution_design\n    name: \"方案设计\"\n    status: PASS", "stage: solution_design\n    name: \"方案设计\"\n    status: PENDING")
        worklist_path.write_text(worklist, encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "strict-tracking-run",
            "--strict",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("worklist.stages[solution_design].status 必须与 state.stages.solution_design 一致", out.getvalue())

    def test_strict_validate_rejects_final_report_pass_when_full_required_stages_are_missing(self):
        self.controller.main([
            "init",
            "Strict full final gate",
            "--run-id",
            "strict-full-final-run",
            "--mode",
            "FULL",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "strict-full-final-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["stages"]["final_report"] = "PASS"
        state["current_stage"] = "final_report"
        state["git_gate"]["status"] = "PASS"
        state["git_gate"]["diff_summary"] = "M README.md"
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        result = self.controller.record_stage_result(
            self.tmp,
            "strict-full-final-run",
            "final_report",
            "PASS",
            ["docs/final.md"],
        )
        self.assertEqual(result["status"], "PASS")

        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "strict-full-final-run",
            "--strict",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("FULL 执行等级要求阶段 solution_review 在 final_report 通过前为 PASS", out.getvalue())
        self.assertIn("FULL 执行等级要求阶段 health_gate 在 final_report 通过前为 PASS", out.getvalue())

    def test_close_command_requires_gate_and_final_report_before_marking_run_closed(self):
        self.controller.main([
            "init",
            "Close run",
            "--run-id",
            "close-run",
            "--mode",
            "LIGHT",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "close",
            "close-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("FAIL 运行无法收口：close-run", out.getvalue())
        self.assertIn("收口前，final_report 必须为 PASS", out.getvalue())

        self.force_current_stage("close-run", "final_report")
        self.controller.main([
            "record-stage",
            "--run-id",
            "close-run",
            "--stage",
            "final_report",
            "--status",
            "PASS",
            "--evidence",
            "docs/final.md",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "close-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["git_gate"]["status"] = "PASS"
        state["git_gate"]["diff_summary"] = "M README.md"
        state["current_stage"] = "release_readiness"
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        self.controller.main([
            "record-stage",
            "--run-id",
            "close-run",
            "--stage",
            "release_readiness",
            "--status",
            "PASS",
            "--evidence",
            "stage-results/15-release-readiness.json",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        self.controller.main([
            "compound",
            "close-run",
            "--decision",
            "skipped",
            "--reason",
            "关闭流程测试不产生长期复用学习。",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "close",
            "close-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        self.assertIn("PASS 运行已收口：close-run", out.getvalue())
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "PASS")
        self.assertEqual(state["current_stage"], "final_report")
        self.assertEqual(state["next_action"], "closed")
        evidence = json.loads((run_dir / "artifacts" / "close-evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(evidence["run_id"], "close-run")
        self.assertEqual(evidence["git_gate"]["diff_summary"], "M README.md")
        self.assertEqual(evidence["compound_capture"]["decision"], "skipped")
        self.assertIn("final_report", evidence["evidence_matrix"])
        self.assertIn("本地收口未覆盖 CI 或远端验证", evidence["uncovered"])
        self.assertTrue((run_dir / "artifacts" / "close-evidence.json").exists())
        self.assertFalse((run_dir / "events.jsonl").exists(), "events.jsonl should be archived after close")
        self.assertFalse((run_dir / "artifacts" / "repair-tickets").exists(), "repair-tickets should be archived after close")
        self.assertTrue((run_dir / "artifacts" / "archive" / "events.jsonl").exists())
        self.assertTrue((run_dir / "artifacts" / "archive" / "repair-tickets").exists())
        self.assertTrue((run_dir / "stage-results" / "00-environment-check.json").exists(), "stage-results should be preserved after close")

    def test_advance_to_solution_design_requires_interview_spec_and_mode_gates(self):
        self.controller.main([
            "init",
            "Front gate advance",
            "--run-id",
            "front-gate-advance-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "front-gate-advance-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["stages"] = {
            "environment_check": "PASS",
            "requirement_intake": "PASS",
        }
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "advance",
            "--run-id",
            "front-gate-advance-run",
            "--to",
            "solution_design",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        text = out.getvalue()
        self.assertIn("进入 solution_design 前，阶段 requirement_interview 必须为 PASS", text)
        self.assertIn("进入 solution_design 前，阶段 spec_review 必须为 PASS", text)
        self.assertIn("进入 solution_design 前，阶段 mode_selection 必须为 PASS", text)

    def test_light_skipped_stage_allows_advance_and_syncs_worklist(self):
        self.controller.main([
            "init",
            "Light skip gate",
            "--run-id",
            "light-skip-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "light-skip-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["stages"] = {
            "environment_check": "PASS",
            "requirement_intake": "PASS",
            "requirement_interview": "PASS",
            "spec_draft": "PASS",
            "spec_review": "SKIPPED",
            "mode_selection": "PASS",
        }
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "advance",
            "--run-id",
            "light-skip-run",
            "--to",
            "solution_design",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("PASS 已进入阶段：solution_design", out.getvalue())
        worklist_text = (run_dir / "worklist.yml").read_text(encoding="utf-8")
        self.assertIn("current_stage: solution_design", worklist_text)

    def test_skipped_stage_blocks_advance_outside_allowed_mode(self):
        self.controller.main([
            "init",
            "Standard skip gate",
            "--run-id",
            "standard-skip-run",
            "--mode",
            "STANDARD",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "standard-skip-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["stages"] = {
            "environment_check": "PASS",
            "requirement_intake": "PASS",
            "requirement_interview": "PASS",
            "spec_draft": "PASS",
            "spec_review": "SKIPPED",
            "mode_selection": "PASS",
        }
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "advance",
            "--run-id",
            "standard-skip-run",
            "--to",
            "solution_design",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("进入 solution_design 前，阶段 spec_review 必须为 PASS", out.getvalue())

    def test_light_cannot_skip_hard_required_stage(self):
        self.controller.main([
            "init",
            "Light hard stage",
            "--run-id",
            "light-hard-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "light-hard-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["stages"] = {
            "environment_check": "PASS",
            "requirement_intake": "PASS",
            "requirement_interview": "PASS",
            "spec_draft": "SKIPPED",
        }
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "advance",
            "--run-id",
            "light-hard-run",
            "--to",
            "spec_review",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("进入 spec_review 前，阶段 spec_draft 必须为 PASS", out.getvalue())

    def test_strict_validate_rejects_skipped_outside_allowed_mode(self):
        self.controller.main([
            "init",
            "Strict skip",
            "--run-id",
            "strict-skip-run",
            "--mode",
            "STANDARD",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "strict-skip-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["stages"]["spec_review"] = "SKIPPED"
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "strict-skip-run",
            "--strict",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("STANDARD 执行等级不允许将阶段 spec_review 设为 SKIPPED", out.getvalue())

    def test_light_skipped_stages_pass_full_gate_to_solution_design(self):
        self.controller.main([
            "init",
            "Light full gate",
            "--run-id",
            "light-gate-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        self.answer_interview_artifact("light-gate-run")

        def record(stage, status):
            self.force_current_stage("light-gate-run", stage)
            out = io.StringIO()
            code = self.controller.main([
                "record-stage",
                "--run-id",
                "light-gate-run",
                "--stage",
                stage,
                "--status",
                status,
                "--evidence",
                f"docs/loopx/runs/light-gate-run/artifacts/{stage}.md",
                "--project",
                str(self.tmp),
            ], stdout=out)
            self.assertEqual(code, 0, out.getvalue())

        record("requirement_intake", "PASS")
        record("requirement_interview", "PASS")
        out = io.StringIO()
        self.controller.main([
            "confirm-stage",
            "--run-id",
            "light-gate-run",
            "--stage",
            "requirement_interview",
            "--evidence",
            "user confirmed",
            "--project",
            str(self.tmp),
        ], stdout=out)
        record("spec_draft", "PASS")
        record("spec_review", "SKIPPED")
        self.force_current_stage("light-gate-run", "mode_selection")

        out = io.StringIO()
        code = self.controller.main([
            "mode",
            "light-gate-run",
            "--select",
            "LIGHT",
            "--project",
            str(self.tmp),
        ], stdout=out)
        self.assertEqual(code, 0, out.getvalue())

        out = io.StringIO()
        code = self.controller.main([
            "advance",
            "--run-id",
            "light-gate-run",
            "--to",
            "solution_design",
            "--project",
            str(self.tmp),
        ], stdout=out)
        self.assertEqual(code, 0, out.getvalue())

        for cmd in ("validate", "gate"):
            out = io.StringIO()
            code = self.controller.main([
                cmd,
                "light-gate-run",
                "--project",
                str(self.tmp),
            ], stdout=out)
            self.assertEqual(code, 0, out.getvalue())

    def test_light_skipped_release_readiness_allows_final_report_close(self):
        self.controller.main([
            "init",
            "Light release skip",
            "--run-id",
            "light-release-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "light-release-run"

        self.controller.main([
            "record-stage",
            "--run-id",
            "light-release-run",
            "--stage",
            "final_report",
            "--status",
            "PASS",
            "--evidence",
            "docs/final.md",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        self.controller.main([
            "record-stage",
            "--run-id",
            "light-release-run",
            "--stage",
            "release_readiness",
            "--status",
            "SKIPPED",
            "--evidence",
            "LIGHT 无发布窗口",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["git_gate"]["status"] = "PASS"
        state["git_gate"]["diff_summary"] = "M docs"
        state["mode_decision"] = {
            "recommended": "LIGHT",
            "selected": "LIGHT",
            "selection_status": "CONFIRMED",
            "selected_by": "user",
            "reason": [],
            "accepted_risk": {"selected_lower_than_recommended": False, "reason": ""},
        }
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        self.controller.main([
            "compound",
            "light-release-run",
            "--decision",
            "skipped",
            "--reason",
            "收口测试无复用学习点。",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        for cmd in ("validate", "gate"):
            out = io.StringIO()
            code = self.controller.main([
                cmd,
                "light-release-run",
                "--project",
                str(self.tmp),
            ], stdout=out)
            self.assertEqual(code, 0, out.getvalue())

    def test_light_skipped_review_gates_allow_business_writes(self):
        self.controller.main([
            "init",
            "Light business write",
            "--run-id",
            "light-write-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "light-write-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["current_stage"] = "development"
        state["stages"] = {
            "environment_check": "PASS",
            "requirement_intake": "PASS",
            "requirement_interview": "PASS",
            "spec_draft": "PASS",
            "spec_review": "SKIPPED",
            "mode_selection": "PASS",
            "solution_design": "PASS",
            "solution_review": "SKIPPED",
            "test_design": "PASS",
            "test_review": "SKIPPED",
        }
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "can-write",
            "--run-id",
            "light-write-run",
            "--kind",
            "business",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("PASS 允许写入业务文件", out.getvalue())

    def test_fail_review_blocks_stage_after_max_auto_repair(self):
        self.controller.main([
            "init",
            "Repair limit",
            "--run-id",
            "repair-limit-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "repair-limit-run"
        for _ in range(3):
            self.force_current_stage("repair-limit-run", "solution_review")
            out = io.StringIO()
            code = self.controller.main([
                "fail-review",
                "--run-id",
                "repair-limit-run",
                "--from",
                "solution_review",
                "--return-to",
                "solution_design",
                "--item",
                "W1",
                "--reason",
                "review failure",
                "--project",
                str(self.tmp),
            ], stdout=out)
            self.assertEqual(code, 0, out.getvalue())

        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        ticket = json.loads((run_dir / "artifacts" / "repair-tickets" / "W1.json").read_text(encoding="utf-8"))
        self.assertEqual(state["loop_attempts"]["solution_review"], 3)
        self.assertEqual(state["stages"]["solution_review"], "BLOCKED")
        self.assertEqual(ticket["stage_status"], "BLOCKED")

        out = io.StringIO()
        code = self.controller.main([
            "advance",
            "--run-id",
            "repair-limit-run",
            "--to",
            "test_design",
            "--project",
            str(self.tmp),
        ], stdout=out)
        self.assertEqual(code, 1)
        self.assertIn("阶段 solution_review 状态为 BLOCKED，推进前必须返回处理", out.getvalue())

        # fail_review 后 worklist 与 state 同步，strict 校验不报 stage 状态漂移。
        out = io.StringIO()
        self.controller.main([
            "validate",
            "repair-limit-run",
            "--strict",
            "--project",
            str(self.tmp),
        ], stdout=out)
        self.assertNotIn("worklist.stages[solution_review].status must match", out.getvalue())

    def test_can_write_allows_dev_repair_ticket_but_blocks_without_one(self):
        self.controller.main([
            "init",
            "Repair write gate",
            "--run-id",
            "repair-write-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "repair-write-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["current_stage"] = "development"
        state["stages"] = {
            "environment_check": "PASS",
            "requirement_intake": "PASS",
            "requirement_interview": "PASS",
            "spec_draft": "PASS",
            "spec_review": "PASS",
            "mode_selection": "PASS",
            "solution_design": "PASS",
            "solution_review": "PASS",
            "test_design": "PASS",
            "test_review": "PASS",
            "development": "PASS",
            "quality_audit": "PASS",
            "code_review": "PASS",
            "test_execution": "PASS",
            "health_gate": "CHANGES_REQUIRED",
        }
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        # 无返工单：CHANGES_REQUIRED 仍锁定写入（防止越级写入）。
        out = io.StringIO()
        code = self.controller.main([
            "can-write",
            "--run-id",
            "repair-write-run",
            "--kind",
            "business",
            "--project",
            str(self.tmp),
        ], stdout=out)
        self.assertEqual(code, 1)
        self.assertIn("阶段 health_gate 状态为 CHANGES_REQUIRED", out.getvalue())

        # 登记指向 development 的返工单后：修复性写入放行（不再死锁）。
        self.force_current_stage("repair-write-run", "health_gate")
        self.controller.main([
            "fail-review",
            "--run-id",
            "repair-write-run",
            "--from",
            "health_gate",
            "--return-to",
            "development",
            "--item",
            "W4",
            "--reason",
            "分片协调器未完成",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        out = io.StringIO()
        code = self.controller.main([
            "can-write",
            "--run-id",
            "repair-write-run",
            "--kind",
            "business",
            "--project",
            str(self.tmp),
        ], stdout=out)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("PASS 允许写入业务文件", out.getvalue())

    def test_can_write_stays_locked_when_stage_blocked_despite_repair_ticket(self):
        self.controller.main([
            "init",
            "Blocked write gate",
            "--run-id",
            "blocked-write-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "blocked-write-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["current_stage"] = "development"
        state["stages"] = {
            "environment_check": "PASS",
            "requirement_intake": "PASS",
            "requirement_interview": "PASS",
            "spec_draft": "PASS",
            "spec_review": "PASS",
            "mode_selection": "PASS",
            "solution_design": "PASS",
            "solution_review": "PASS",
            "test_design": "PASS",
            "test_review": "PASS",
            "health_gate": "BLOCKED",
        }
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        # 即使有指向 development 的开放返工单，BLOCKED 也必须锁定写入（等待人工处理）。
        self.force_current_stage("blocked-write-run", "health_gate")
        self.controller.main([
            "fail-review",
            "--run-id",
            "blocked-write-run",
            "--from",
            "health_gate",
            "--return-to",
            "development",
            "--item",
            "W5",
            "--reason",
            "环境问题待人工处理",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["stages"]["health_gate"] = "BLOCKED"
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "can-write",
            "--run-id",
            "blocked-write-run",
            "--kind",
            "business",
            "--project",
            str(self.tmp),
        ], stdout=out)
        self.assertEqual(code, 1)
        self.assertIn("阶段 health_gate 状态为 BLOCKED", out.getvalue())

    def test_close_repair_clears_blocked_stage_for_retry(self):
        self.controller.main([
            "init",
            "Repair recover",
            "--run-id",
            "repair-recover-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "repair-recover-run"
        for _ in range(3):
            self.force_current_stage("repair-recover-run", "solution_review")
            self.controller.main([
                "fail-review",
                "--run-id",
                "repair-recover-run",
                "--from",
                "solution_review",
                "--return-to",
                "solution_design",
                "--item",
                "W1",
                "--reason",
                "review failure",
                "--project",
                str(self.tmp),
            ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "close-repair",
            "--run-id",
            "repair-recover-run",
            "--item",
            "W1",
            "--artifact",
            "stage-results/06-solution-design.json",
            "--revision",
            "2",
            "--change",
            "fixed",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0, out.getvalue())
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertNotIn("solution_review", state["stages"])
        ticket = json.loads((run_dir / "artifacts" / "repair-tickets" / "W1.json").read_text(encoding="utf-8"))
        self.assertEqual(ticket["status"], "CLOSED")

    def test_interview_command_generates_artifact_and_updates_current_stage(self):
        self.controller.main([
            "init",
            "采访后再写规格",
            "--run-id",
            "interview-command-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "interview",
            "interview-command-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "interview-command-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        artifact = run_dir / "artifacts" / "interview.md"
        worklist = (run_dir / "worklist.yml").read_text(encoding="utf-8")

        self.assertTrue(artifact.exists())
        artifact_text = artifact.read_text(encoding="utf-8")
        self.assertIn("需求采访", artifact_text)
        self.assertIn("这个需求要解决的具体问题是什么", artifact_text)
        self.assertIn("状态：未回答", artifact_text)
        self.assertEqual(state["current_stage"], "requirement_interview")
        self.assertEqual(state["interview"]["status"], "IN_PROGRESS")
        self.assertGreater(state["interview"]["unanswered_questions"], 0)
        self.assertIn("这个需求要解决的具体问题是什么", "\n".join(state["interview"]["blocking_questions"]))
        self.assertIn("请回答以下需求采访问题", out.getvalue())
        self.assertIn("问题 1：", out.getvalue())
        self.assertIn("已生成需求采访", out.getvalue())
        self.assertIn("当前阶段：requirement_interview", out.getvalue())
        self.assertIn("current_stage: requirement_interview", worklist)

    def test_spec_command_requires_passed_interview_then_generates_artifact(self):
        self.controller.main([
            "init",
            "采访通过后生成规格",
            "--run-id",
            "spec-command-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "spec",
            "spec-command-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("进入 spec_draft 前，requirement_interview 必须为 PASS", out.getvalue())

        self.controller.main([
            "interview",
            "spec-command-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "record-stage",
            "--run-id",
            "spec-command-run",
            "--stage",
            "requirement_interview",
            "--status",
            "PASS",
            "--evidence",
            "docs/loopx/runs/spec-command-run/artifacts/interview.md",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("需求采访问题尚未全部回答", out.getvalue())

        self.answer_interview_artifact("spec-command-run")
        out = io.StringIO()
        code = self.controller.main([
            "record-stage",
            "--run-id",
            "spec-command-run",
            "--stage",
            "requirement_interview",
            "--status",
            "PASS",
            "--evidence",
            "docs/loopx/runs/spec-command-run/artifacts/interview.md",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        self.assertIn("NEED_HUMAN 已记录阶段：requirement_interview", out.getvalue())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "spec-command-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        result = json.loads((run_dir / "stage-results" / "02-requirement-interview.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stages"]["requirement_interview"], "NEED_HUMAN")
        self.assertEqual(state["interview"]["status"], "NEED_HUMAN")
        self.assertEqual(result["status"], "NEED_HUMAN")
        self.assertTrue(result["user_confirmation_required"])

        out = io.StringIO()
        code = self.controller.main([
            "spec",
            "spec-command-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("阶段 requirement_interview 正在等待用户确认", out.getvalue())

        out = io.StringIO()
        code = self.controller.main([
            "confirm-stage",
            "--run-id",
            "spec-command-run",
            "--stage",
            "requirement_interview",
            "--evidence",
            "user confirmed interview",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        self.assertIn("PASS 已确认阶段：requirement_interview", out.getvalue())

        out = io.StringIO()
        code = self.controller.main([
            "spec",
            "spec-command-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        artifact = run_dir / "artifacts" / "spec.md"

        self.assertTrue(artifact.exists())
        self.assertIn("需求规格", artifact.read_text(encoding="utf-8"))
        self.assertEqual(state["current_stage"], "spec_draft")
        self.assertEqual(state["spec"]["status"], "DRAFT")
        self.assertIn("已生成需求规格", out.getvalue())

    def test_mode_command_records_selection_and_requires_downgrade_reason(self):
        self.controller.main([
            "init",
            "高风险模式选择",
            "--run-id",
            "mode-command-run",
            "--mode",
            "auto",
            "--risk-tags",
            "tenant_scope",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "mode-command-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["stages"] = {
            "environment_check": "PASS",
            "requirement_intake": "PASS",
            "requirement_interview": "PASS",
            "spec_draft": "PASS",
            "spec_review": "PASS",
        }
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "mode",
            "mode-command-run",
            "--select",
            "STANDARD",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("必须说明接受风险的理由", out.getvalue())

        out = io.StringIO()
        code = self.controller.main([
            "mode",
            "mode-command-run",
            "--select",
            "STANDARD",
            "--accepted-risk",
            "用户确认本次降低执行等级",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        result = json.loads((run_dir / "stage-results" / "05-mode-selection.json").read_text(encoding="utf-8"))

        self.assertEqual(state["mode"], "STANDARD")
        self.assertEqual(state["mode_decision"]["recommended"], "FULL")
        self.assertEqual(state["mode_decision"]["selected"], "STANDARD")
        self.assertTrue(state["mode_decision"]["accepted_risk"]["selected_lower_than_recommended"])
        self.assertEqual(state["stages"]["mode_selection"], "ACCEPTED_RISK")
        self.assertEqual(result["status"], "ACCEPTED_RISK")
        self.assertIn("已选择执行等级：STANDARD", out.getvalue())

    def test_next_advances_to_default_next_stage_when_gates_pass(self):
        self.controller.main([
            "init",
            "下一阶段推进",
            "--run-id",
            "next-command-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "next-command-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["current_stage"] = "mode_selection"
        state["stages"] = {
            "environment_check": "PASS",
            "requirement_intake": "PASS",
            "requirement_interview": "PASS",
            "spec_draft": "PASS",
            "spec_review": "PASS",
            "mode_selection": "PASS",
        }
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "next",
            "next-command-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["current_stage"], "solution_design")
        self.assertIn("PASS 已进入阶段：solution_design", out.getvalue())

    def test_next_does_not_partially_write_before_atomic_commit(self):
        run_id = "next-atomic-run"
        self.controller.main([
            "init",
            "下一阶段原子推进",
            "--run-id",
            run_id,
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.run_dir(run_id)
        state = self.read_state(run_id)
        state["current_stage"] = "mode_selection"
        state["stages"] = {
            "environment_check": "PASS",
            "requirement_intake": "PASS",
            "requirement_interview": "PASS",
            "spec_draft": "PASS",
            "spec_review": "PASS",
            "mode_selection": "PASS",
        }
        self.write_state(run_id, state)
        tracked = [run_dir / "state.json", run_dir / "worklist.yml", run_dir / "events.jsonl"]
        before = {path: path.read_bytes() for path in tracked}

        with mock.patch(
            "loopx_controller_core.update_worklist_state_data",
            side_effect=KeyboardInterrupt("模拟进程中断"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.controller.main([
                    "next",
                    run_id,
                    "--project",
                    str(self.tmp),
                ], stdout=io.StringIO())

        self.assertEqual(before, {path: path.read_bytes() for path in tracked})

    def test_record_stage_writes_machine_readable_result_and_state(self):
        self.controller.main([
            "init",
            "Record stage",
            "--run-id",
            "record-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        self.force_current_stage("record-run", "solution_design")
        out = io.StringIO()
        code = self.controller.main([
            "record-stage",
            "--run-id",
            "record-run",
            "--stage",
            "solution_design",
            "--status",
            "PASS",
            "--evidence",
            "docs/solution.md",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "record-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        result = json.loads((run_dir / "stage-results" / "06-solution-design.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stages"]["solution_design"], "PASS")
        self.assertEqual(result["stage"], "solution_design")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["evidence"], ["docs/solution.md"])
        worklist = (run_dir / "worklist.yml").read_text(encoding="utf-8")
        self.assertIn("stage: solution_design", worklist)
        self.assertIn("status: PASS", worklist)
        self.assertIn('evidence: "docs/loopx/runs/record-run/stage-results/06-solution-design.json"', worklist)
        self.assertIn("PASS 已记录阶段：solution_design", out.getvalue())

    def test_advance_blocks_when_prior_stage_is_not_pass(self):
        self.controller.main([
            "init",
            "Advance gate",
            "--run-id",
            "advance-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "advance",
            "--run-id",
            "advance-run",
            "--to",
            "development",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("FAIL 阶段推进被阻止", out.getvalue())
        self.assertIn("进入 development 前，阶段 solution_review 必须为 PASS", out.getvalue())

    def test_can_write_business_requires_development_solution_and_test_review_pass(self):
        self.controller.main([
            "init",
            "Business gate",
            "--run-id",
            "write-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "can-write",
            "--run-id",
            "write-run",
            "--kind",
            "business",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("FAIL 业务文件写入仍被锁定", out.getvalue())
        self.assertIn("当前阶段必须为 development", out.getvalue())

        run_dir = self.tmp / "docs" / "loopx" / "runs" / "write-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["current_stage"] = "development"
        state["stages"]["solution_review"] = "PASS"
        state["stages"]["test_review"] = "PASS"
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "can-write",
            "--run-id",
            "write-run",
            "--kind",
            "business",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        self.assertIn("PASS 允许写入业务文件", out.getvalue())

    def test_confirmation_gate_record_pass_waits_for_confirm_stage_and_blocks_next(self):
        self.controller.main([
            "init",
            "Solution review needs human confirmation",
            "--run-id",
            "confirm-solution-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        state = self.read_state("confirm-solution-run")
        state["current_stage"] = "solution_review"
        state["stages"] = {
            "environment_check": "PASS",
            "requirement_intake": "PASS",
            "requirement_interview": "PASS",
            "spec_draft": "PASS",
            "spec_review": "PASS",
            "mode_selection": "PASS",
            "solution_design": "PASS",
        }
        self.write_state("confirm-solution-run", state)

        out = io.StringIO()
        code = self.controller.main([
            "record-stage",
            "--run-id",
            "confirm-solution-run",
            "--stage",
            "solution_review",
            "--status",
            "PASS",
            "--evidence",
            "stage-results/07-solution-review.json",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        run_dir = self.run_dir("confirm-solution-run")
        state = self.read_state("confirm-solution-run")
        result = json.loads((run_dir / "stage-results" / "07-solution-review.json").read_text(encoding="utf-8"))
        worklist = (run_dir / "worklist.yml").read_text(encoding="utf-8")
        self.assertEqual(state["stages"]["solution_review"], "NEED_HUMAN")
        self.assertEqual(result["status"], "NEED_HUMAN")
        self.assertEqual(result["agent_result"], "PASS")
        self.assertTrue(result["user_confirmation_required"])
        self.assertEqual(result["next_action"], "confirm-stage --stage solution_review")
        self.assertIn("NEED_HUMAN 已记录阶段：solution_review", out.getvalue())
        self.assertIn('stage: solution_review\n    name: "方案审核"\n    status: NEED_HUMAN', worklist)

        out = io.StringIO()
        code = self.controller.main([
            "next",
            "confirm-solution-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("阶段 solution_review 正在等待用户确认；请执行 confirm-stage --stage solution_review", out.getvalue())

    def test_confirm_stage_changes_waiting_gate_to_pass_and_allows_advance(self):
        self.controller.main([
            "init",
            "Confirm solution review",
            "--run-id",
            "confirm-command-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        state = self.read_state("confirm-command-run")
        state["current_stage"] = "solution_review"
        state["stages"] = {
            "environment_check": "PASS",
            "requirement_intake": "PASS",
            "requirement_interview": "PASS",
            "spec_draft": "PASS",
            "spec_review": "PASS",
            "mode_selection": "PASS",
            "solution_design": "PASS",
        }
        self.write_state("confirm-command-run", state)
        self.controller.main([
            "record-stage",
            "--run-id",
            "confirm-command-run",
            "--stage",
            "solution_review",
            "--status",
            "PASS",
            "--evidence",
            "stage-results/07-solution-review.json",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "confirm-stage",
            "--run-id",
            "confirm-command-run",
            "--stage",
            "solution_review",
            "--evidence",
            "user confirmed",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        run_dir = self.run_dir("confirm-command-run")
        state = self.read_state("confirm-command-run")
        result = json.loads((run_dir / "stage-results" / "07-solution-review.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stages"]["solution_review"], "PASS")
        self.assertEqual(state["confirmations"]["solution_review"]["confirmation_evidence"], ["user confirmed"])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["confirmed_by"], "user")
        self.assertEqual(result["confirmation_evidence"], ["user confirmed"])
        self.assertFalse(result["user_confirmation_required"])
        self.assertIn("PASS 已确认阶段：solution_review", out.getvalue())

        out = io.StringIO()
        code = self.controller.main([
            "next",
            "confirm-command-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        self.assertEqual(self.read_state("confirm-command-run")["current_stage"], "test_design")
        self.assertIn("PASS 已进入阶段：test_design", out.getvalue())

    def test_test_review_pass_auto_advances_to_development_and_allows_business_writes(self):
        """test_review is no longer a confirmation gate - PASS auto-advances."""
        self.controller.main([
            "init",
            "Test review auto advances",
            "--run-id",
            "auto-test-review-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        state = self.read_state("auto-test-review-run")
        state["current_stage"] = "test_review"
        state["stages"] = {
            "environment_check": "PASS",
            "requirement_intake": "PASS",
            "requirement_interview": "PASS",
            "spec_draft": "PASS",
            "spec_review": "PASS",
            "mode_selection": "PASS",
            "solution_design": "PASS",
            "solution_review": "PASS",
            "test_design": "PASS",
        }
        self.write_state("auto-test-review-run", state)
        self.controller.main([
            "record-stage",
            "--run-id",
            "auto-test-review-run",
            "--stage",
            "test_review",
            "--status",
            "PASS",
            "--evidence",
            "stage-results/09-test-review.json",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        state = self.read_state("auto-test-review-run")
        self.assertEqual(state["stages"]["test_review"], "PASS")

        out = io.StringIO()
        code = self.controller.main([
            "advance",
            "--run-id",
            "auto-test-review-run",
            "--to",
            "development",
            "--project",
            str(self.tmp),
        ], stdout=out)
        self.assertEqual(code, 0)

        state = self.read_state("auto-test-review-run")
        state["current_stage"] = "development"
        self.write_state("auto-test-review-run", state)
        out = io.StringIO()
        code = self.controller.main([
            "can-write",
            "--run-id",
            "auto-test-review-run",
            "--kind",
            "business",
            "--project",
            str(self.tmp),
        ], stdout=out)
        self.assertEqual(code, 0)

    def test_code_review_pass_advances_to_test_execution_without_confirmation(self):
        self.controller.main([
            "init",
            "Code review does not need confirmation",
            "--run-id",
            "auto-code-review-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        state = self.read_state("auto-code-review-run")
        state["current_stage"] = "code_review"
        state["stages"] = {
            "environment_check": "PASS",
            "requirement_intake": "PASS",
            "requirement_interview": "PASS",
            "spec_draft": "PASS",
            "spec_review": "PASS",
            "mode_selection": "PASS",
            "solution_design": "PASS",
            "solution_review": "PASS",
            "test_design": "PASS",
            "test_review": "PASS",
            "development": "PASS",
            "quality_audit": "PASS",
        }
        self.write_state("auto-code-review-run", state)
        record_out = io.StringIO()
        self.controller.main([
            "record-stage",
            "--run-id",
            "auto-code-review-run",
            "--stage",
            "code_review",
            "--status",
            "PASS",
            "--evidence",
            "stage-results/12-code-review.json",
            "--project",
            str(self.tmp),
        ], stdout=record_out)

        self.assertIn("PASS 已记录阶段：code_review", record_out.getvalue())
        self.assertIn("下一步：test_execution", record_out.getvalue())
        self.assertEqual(self.read_state("auto-code-review-run")["stages"]["code_review"], "PASS")

        out = io.StringIO()
        code = self.controller.main([
            "next",
            "auto-code-review-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        self.assertEqual(self.read_state("auto-code-review-run")["current_stage"], "test_execution")
        self.assertIn("PASS 已进入阶段：test_execution", out.getvalue())

    def test_release_readiness_auto_advances_to_final_report(self):
        """release_readiness is no longer a confirmation gate - PASS auto-advances."""
        self.controller.main([
            "init",
            "Release readiness auto advances",
            "--run-id",
            "auto-release-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        state = self.read_state("auto-release-run")
        state["current_stage"] = "release_readiness"
        state["stages"] = {
            "environment_check": "PASS",
            "requirement_intake": "PASS",
            "requirement_interview": "PASS",
            "spec_draft": "PASS",
            "spec_review": "PASS",
            "mode_selection": "PASS",
            "solution_design": "PASS",
            "solution_review": "PASS",
            "test_design": "PASS",
            "test_review": "PASS",
            "development": "PASS",
            "quality_audit": "PASS",
            "code_review": "PASS",
            "test_execution": "PASS",
            "health_gate": "PASS",
        }
        self.write_state("auto-release-run", state)
        self.controller.main([
            "record-stage",
            "--run-id",
            "auto-release-run",
            "--stage",
            "release_readiness",
            "--status",
            "PASS",
            "--evidence",
            "stage-results/15-release-readiness.json",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        state = self.read_state("auto-release-run")
        self.assertEqual(state["stages"]["release_readiness"], "PASS")

        out = io.StringIO()
        code = self.controller.main([
            "next",
            "auto-release-run",
            "--project",
            str(self.tmp),
        ], stdout=out)
        self.assertEqual(code, 0)

    def test_strict_validate_rejects_confirmed_gate_without_confirmation_metadata(self):
        self.controller.main([
            "init",
            "Strict confirmation metadata",
            "--run-id",
            "strict-confirmation-run",
            "--mode",
            "LIGHT",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.run_dir("strict-confirmation-run")
        state = self.read_state("strict-confirmation-run")
        state["current_stage"] = "solution_review"
        state["stages"]["solution_review"] = "PASS"
        self.write_state("strict-confirmation-run", state)
        self.controller.update_worklist_state(self.tmp, state, "solution_review", "PASS")
        result = {
            "stage": "solution_review",
            "status": "PASS",
            "mode": "LIGHT",
            "summary": "",
            "return_to": "",
            "next_action": "test_design",
            "affected_work_items": [],
            "evidence": ["stage-results/07-solution-review.json"],
            "tracking_snapshot": self.controller.build_tracking_snapshot(state),
            "gate": {
                "result": "PASS",
                "blocking_issues": [],
                "non_blocking_issues": [],
            },
            "user_confirmation_required": False,
            "blocked_reason": "",
        }
        (run_dir / "stage-results" / "07-solution-review.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "validate",
            "strict-confirmation-run",
            "--strict",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("阶段 solution_review 记录为 PASS 时必须包含确认信息", out.getvalue())

    def test_review_feedback_marks_changes_required_and_returns_to_stage(self):
        self.controller.main([
            "init",
            "Feedback gate",
            "--run-id",
            "feedback-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "feedback-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["current_stage"] = "development"
        state["stages"] = {
            "solution_design": "PASS",
            "solution_review": "PASS",
            "development": "PASS",
        }
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        (run_dir / "worklist.yml").write_text("""run:
  id: feedback-run
  requirement: Feedback gate
  mode: STANDARD
  status: ACTIVE
  current_stage: development
items:
  - id: W1
    title: Exception package
    status: TODO
    risk_tags: []
    owner_agent: solution-designer
    read_scope: []
    write_scope: []
    dependencies: []
    validation: []
    evidence: []
    failed_by: ""
    return_to: ""
    required_changes: []
""", encoding="utf-8")

        out = io.StringIO()
        code = self.controller.main([
            "review-feedback",
            "--run-id",
            "feedback-run",
            "--item",
            "W1",
            "--return-to",
            "solution_design",
            "--reason",
            "exception package wrong",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        review = json.loads((run_dir / "stage-results" / "07-solution-review.json").read_text(encoding="utf-8"))
        worklist = (run_dir / "worklist.yml").read_text(encoding="utf-8")
        self.assertEqual(state["current_stage"], "solution_design")
        self.assertEqual(state["stages"]["solution_review"], "CHANGES_REQUIRED")
        self.assertNotIn("development", state["stages"])
        self.assertEqual(review["status"], "CHANGES_REQUIRED")
        self.assertEqual(review["return_to"], "solution_design")
        self.assertIn("exception package wrong", review["evidence"])
        self.assertIn("status: CHANGES_REQUIRED", worklist)
        self.assertIn("return_to: solution_design", worklist)

    def test_fail_review_creates_repair_ticket_for_return_stage_owner(self):
        self.controller.main([
            "init",
            "Repair loop",
            "--run-id",
            "repair-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        self.force_current_stage("repair-run", "solution_review")
        out = io.StringIO()
        code = self.controller.main([
            "fail-review",
            "--run-id",
            "repair-run",
            "--from",
            "solution_review",
            "--return-to",
            "solution_design",
            "--item",
            "W1",
            "--reason",
            "ShopLimitExceededException must be in com.crosscomm.admin.exception",
            "--reason",
            "GlobalExceptionHandler must remain in controller.handler",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "repair-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        ticket = json.loads((run_dir / "artifacts" / "repair-tickets" / "W1.json").read_text(encoding="utf-8"))

        self.assertEqual(state["current_stage"], "solution_design")
        self.assertEqual(state["active_agent"], "solution-designer")
        self.assertEqual(state["next_action"], "repair_solution_design")
        self.assertEqual(state["loop_attempts"]["solution_review"], 1)
        self.assertEqual(state["stages"]["solution_review"], "CHANGES_REQUIRED")
        self.assertEqual(ticket["type"], "review_failed")
        self.assertEqual(ticket["from_stage"], "solution_review")
        self.assertEqual(ticket["return_to"], "solution_design")
        self.assertEqual(ticket["assigned_to"], "solution-designer")
        self.assertEqual(ticket["attempt"], 1)
        self.assertEqual(ticket["status"], "OPEN")
        self.assertEqual(ticket["stage_status"], "CHANGES_REQUIRED")
        self.assertEqual(ticket["required_changes"], [
            "ShopLimitExceededException must be in com.crosscomm.admin.exception",
            "GlobalExceptionHandler must remain in controller.handler",
        ])
        self.assertIn("返工单：W1", out.getvalue())

    def test_claim_stage_returns_open_repair_ticket_for_owner_role(self):
        self.controller.main([
            "init",
            "Claim repair",
            "--run-id",
            "claim-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        self.force_current_stage("claim-run", "solution_review")
        self.controller.main([
            "fail-review",
            "--run-id",
            "claim-run",
            "--from",
            "solution_review",
            "--return-to",
            "solution_design",
            "--item",
            "W1",
            "--reason",
            "exception package wrong",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "claim-stage",
            "solution_design",
            "--run-id",
            "claim-run",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("PASS 已领取阶段：solution_design", text)
        self.assertIn("负责人：solution-designer", text)
        self.assertIn("返工单：W1", text)
        self.assertIn("必需修改：exception package wrong", text)

    def test_close_repair_marks_ticket_closed_and_requires_revision(self):
        self.controller.main([
            "init",
            "Close repair",
            "--run-id",
            "close-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        self.force_current_stage("close-run", "solution_review")
        self.controller.main([
            "fail-review",
            "--run-id",
            "close-run",
            "--from",
            "solution_review",
            "--return-to",
            "solution_design",
            "--item",
            "W1",
            "--reason",
            "exception package wrong",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "close-repair",
            "--run-id",
            "close-run",
            "--item",
            "W1",
            "--artifact",
            "stage-results/06-solution-design.json",
            "--revision",
            "2",
            "--change",
            "fixed exception package boundary",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        ticket = json.loads((self.tmp / "docs" / "loopx" / "runs" / "close-run" / "artifacts" / "repair-tickets" / "W1.json").read_text(encoding="utf-8"))
        self.assertEqual(ticket["status"], "CLOSED")
        self.assertEqual(ticket["artifact"], "stage-results/06-solution-design.json")
        self.assertEqual(ticket["revision"], 2)
        self.assertEqual(ticket["changes_from_review"], ["fixed exception package boundary"])
        self.assertIn("PASS 已关闭返工单：W1", out.getvalue())

    def test_advance_blocks_until_return_stage_repair_ticket_is_closed(self):
        self.controller.main([
            "init",
            "Repair advance",
            "--run-id",
            "repair-advance-run",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        run_dir = self.tmp / "docs" / "loopx" / "runs" / "repair-advance-run"
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        state["stages"] = {
            "environment_check": "PASS",
            "requirement_intake": "PASS",
            "requirement_interview": "PASS",
            "spec_draft": "PASS",
            "spec_review": "PASS",
            "mode_selection": "PASS",
            "solution_design": "PASS",
        }
        state["current_stage"] = "solution_review"
        (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        self.controller.main([
            "fail-review",
            "--run-id",
            "repair-advance-run",
            "--from",
            "solution_review",
            "--return-to",
            "solution_design",
            "--item",
            "W1",
            "--reason",
            "exception package wrong",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "advance",
            "--run-id",
            "repair-advance-run",
            "--to",
            "solution_review",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("进入 solution_review 前必须关闭返工单 W1", out.getvalue())

        self.controller.main([
            "close-repair",
            "--run-id",
            "repair-advance-run",
            "--item",
            "W1",
            "--artifact",
            "stage-results/06-solution-design.json",
            "--revision",
            "2",
            "--change",
            "fixed exception package boundary",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())
        self.controller.main([
            "record-stage",
            "--run-id",
            "repair-advance-run",
            "--stage",
            "solution_design",
            "--status",
            "PASS",
            "--evidence",
            "stage-results/06-solution-design.json",
            "--project",
            str(self.tmp),
        ], stdout=io.StringIO())

        out = io.StringIO()
        code = self.controller.main([
            "advance",
            "--run-id",
            "repair-advance-run",
            "--to",
            "solution_review",
            "--project",
            str(self.tmp),
        ], stdout=out)

        self.assertEqual(code, 0)
        self.assertIn("PASS 已进入阶段：solution_review", out.getvalue())


if __name__ == "__main__":
    unittest.main()
