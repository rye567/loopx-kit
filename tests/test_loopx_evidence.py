import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "loopx" / "tools"
CONTROLLER = TOOLS / "loopx_controller.py"


def load_controller():
    spec = importlib.util.spec_from_file_location("loopx_controller_v2", CONTROLLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class V2Fixture(unittest.TestCase):
    def setUp(self):
        self.backend_patch = mock.patch.dict(os.environ, {"LOOPX_STATE_BACKEND": "project"})
        self.backend_patch.start()
        self.addCleanup(self.backend_patch.stop)
        self.controller = load_controller()
        self.root = Path(tempfile.mkdtemp(prefix=f"loopx-std-evidence-{uuid.uuid4().hex[:8]}-"))
        self.run_id = f"loopx-std-{uuid.uuid4().hex[:10]}"

    def tearDown(self):
        root = self.root
        shutil.rmtree(root)
        self.assertFalse(root.exists())

    @property
    def run_dir(self):
        return self.root / "docs" / "loopx" / "runs" / self.run_id

    def init(self, mode="LIGHT", risk_tags=None, automation_policy=None):
        args = [
            "init",
            "验证 LoopX v2 结构化证据",
            "--run-id",
            self.run_id,
            "--mode",
            mode,
        ]
        if risk_tags:
            args.extend(["--risk-tags", *risk_tags])
        if automation_policy:
            args.extend(["--automation-policy", automation_policy])
        args.extend(["--project", str(self.root)])
        out = io.StringIO()
        self.assertEqual(self.controller.main(args, stdout=out), 0, out.getvalue())
        return json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))

    def write_text(self, relative, text="证据\n"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return relative

    def write_json(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return relative

    def evidence_path(self):
        return self.write_text(f"docs/loopx/runs/{self.run_id}/artifacts/check.log")

    def init_git_repository(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        subprocess.run(
            [
                "git", "-c", "user.name=LoopX Test", "-c", "user.email=loopx@example.invalid",
                "commit", "--allow-empty", "-q", "-m", "baseline",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
        )

    def document_path(self, name="solution.md"):
        return self.write_text(f"docs/loopx/{self.run_id}/{name}", "# 已审核文档\n\n内容完整。\n")

    def quality_attributes(self, evidence):
        return {
            name: {
                "status": "APPLICABLE",
                "approach": f"已验证 {name}",
                "reason": "",
                "evidence": [evidence],
            }
            for name in (
                "simplicity",
                "module_boundaries",
                "security",
                "performance",
                "extensibility",
                "compatibility",
                "reliability",
                "observability",
            )
        }

    def requirement_extensions(self):
        state_path = self.run_dir / "state.json"
        if not state_path.is_file():
            return {}
        state = json.loads(state_path.read_text(encoding="utf-8"))
        digest = (state.get("spec") or {}).get("extensions", {}).get("requirement_manifest_sha256")
        return {"requirement_manifest_sha256": digest} if digest else {}

    def review_assurance(self, evidence):
        verdict = {"status": "PASS", "evidence": [evidence], "reason": ""}
        return {
            "reviewed_snapshot_id": self.recorded_solution_digest(),
            "review_kind": "FULL",
            "baseline_snapshot_id": "",
            "review_scope": ["需求、方案、影响范围与验证策略"],
            "checked_dimensions": {
                name: dict(verdict)
                for name in (
                    "requirement_coverage",
                    "minimal_modification",
                    "existing_behavior_impact",
                    "interface_contract",
                    "verification_deployment",
                )
            },
            "blocking_findings": [],
            "unknowns": [],
            "completeness_attestation": "已一次性检查全部适用维度和阻塞问题。",
        }

    def make_review_solution(self, solution, evidence):
        artifact = json.loads(json.dumps(solution))
        artifact["stage"] = "solution_review"
        artifact.setdefault("extensions", {})["review_assurance"] = self.review_assurance(evidence)
        state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        snapshot = json.loads((self.root / state["policy_snapshot"]).read_text(encoding="utf-8"))
        selected_rule_ids = {item["id"] for item in snapshot.get("rules") or []}
        rule_ids = {item["rule_id"] for item in artifact["rule_results"]}
        for rule_id in ("ARCH-SIMPLE-001", "ARCH-REVIEW-COVERAGE-004"):
            if rule_id in selected_rule_ids and rule_id not in rule_ids:
                artifact["rule_results"].append(self.rule_result(rule_id, evidence))
        return artifact

    def force_current_stage(self, stage):
        state_path = self.run_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["current_stage"] = stage
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def recorded_solution_digest(self):
        result = json.loads(
            (self.run_dir / "stage-results" / "06-solution-design.json").read_text(encoding="utf-8")
        )
        relative = next(item["path"] for item in result["artifacts"] if item["type"] == "solution")
        return hashlib.sha256((self.root / relative).read_bytes()).hexdigest()

    def solution_artifact(self, evidence=None, document=None, rule_results=None, work_items=None):
        evidence = evidence or self.evidence_path()
        document = document or self.document_path()
        rule_results = rule_results or [{
            "rule_id": "COMMON-EVIDENCE-001",
            "status": "PASS",
            "evidence": [evidence],
            "reason": "",
        }]
        work_items = work_items or [{
            "id": "W1",
            "title": "实现结构化证据",
            "risk_tags": [],
            "owner_agent": "development",
            "read_scope": ["loopx/tools"],
            "write_scope": ["loopx/tools"],
            "dependencies": [],
            "validation": ["python3 -m unittest tests.test_loopx_evidence"],
        }]
        return {
            "artifact_type": "solution",
            "artifact_version": "1",
            "run_id": self.run_id,
            "stage": "solution_design",
            "document": document,
            "requirement_ids": ["AC-001"],
            "rule_results": rule_results,
            "decisions": [{
                "id": "D1",
                "requirement_ids": ["AC-001"],
                "summary": "沿用现有控制器",
                "rationale": "保持兼容并减少变更面",
                "alternatives": ["另建流程引擎"],
            }],
            "impact": {
                "modules": ["loopx/tools"],
                "contracts": ["stage-result"],
                "data": [],
                "configuration": ["catalog.yml"],
                "dependencies": [],
            },
            "quality_attributes": self.quality_attributes(evidence),
            "performance_targets": [],
            "rollback": {
                "strategy": "恢复旧控制器路径",
                "steps": ["恢复 v1 分派"],
                "validation": ["运行 v1 回归测试"],
                "reason": "",
            },
            "verification_refs": [evidence],
            "work_items": work_items,
            "extensions": self.requirement_extensions(),
        }

    def write_solution(self, artifact=None, filename="solution.json"):
        artifact = artifact or self.solution_artifact()
        relative = f"docs/loopx/runs/{self.run_id}/artifacts/{filename}"
        self.write_json(relative, artifact)
        return relative

    def rule_result(self, rule_id, evidence):
        return {"rule_id": rule_id, "status": "PASS", "evidence": [evidence], "reason": ""}

    def build_test_plan_artifact(self, evidence, document, rule_ids):
        return {
            "artifact_type": "test_plan",
            "artifact_version": "1",
            "run_id": self.run_id,
            "stage": "test_design",
            "document": document,
            "requirement_ids": ["AC-001"],
            "rule_results": [
                self.rule_result("TEST-MAPPING-001", evidence),
                self.rule_result("TEST-CLEANUP-002", evidence),
            ],
            "mappings": [{
                "requirement_id": "AC-001",
                "acceptance_ids": ["AC-001"],
                "rule_ids": list(rule_ids),
                "test_case_ids": ["TC-001"],
            }],
            "cases": [{
                "id": "TC-001",
                "covers": ["AC-001"],
                "risk_tags": ["core_state_transition"],
                "preconditions": ["FULL v2 运行已初始化"],
                "data_setup": {"run_id_strategy": "uuid", "records": ["临时运行目录"]},
                "execution": {"entrypoint": "python3 -m unittest", "steps": ["执行完整流程测试"]},
                "assertions": ["全部阶段通过", "工作项已解决"],
                "cleanup": {"steps": ["退出临时目录"], "verification": ["临时目录不存在"]},
                "expected_result": "PASS",
            }],
            "environment": {"local": ["Python 3"], "ci_required": [], "manual": ["用户确认"]},
            "extensions": self.requirement_extensions(),
        }

    def development_artifact(self, evidence, document):
        return {
            "artifact_type": "development_evidence",
            "artifact_version": "1",
            "run_id": self.run_id,
            "stage": "development",
            "document": document,
            "requirement_ids": ["AC-001"],
            "rule_results": [self.rule_result("COMMON-EVIDENCE-001", evidence)],
            "changed_files": ["loopx/tools/example.py"],
            "write_scope": ["loopx/tools"],
            "dependency_changes": [],
            "acceptance_mapping": [{
                "requirement_id": "AC-001",
                "files": ["loopx/tools/example.py"],
                "tests": ["tests.test_loopx_evidence"],
            }],
            "commands": [{
                "argv": ["python3", "-m", "unittest"],
                "status": "PASS",
                "exit_code": 0,
                "evidence": [evidence],
                "ci_required": False,
            }],
            "residual_risks": [],
            "extensions": self.requirement_extensions(),
        }

    def quality_artifact(self, evidence, document):
        return {
            "artifact_type": "quality_result",
            "artifact_version": "1",
            "run_id": self.run_id,
            "stage": "quality_audit",
            "document": document,
            "requirement_ids": ["AC-001"],
            "rule_results": [self.rule_result("COMMON-EVIDENCE-001", evidence)],
            "unresolved_items": [],
            "ci_gaps": [],
            "accepted_risks": [],
            "diff_scope": {
                "allowed": ["loopx/tools"],
                "actual": ["loopx/tools/example.py"],
                "outside": [],
            },
            "extensions": self.requirement_extensions(),
        }

    def controller_command(self, *args, expected=0):
        out = io.StringIO()
        code = self.controller.main([*args, "--project", str(self.root)], stdout=out)
        self.assertEqual(code, expected, out.getvalue())
        return out.getvalue()

    def record_solution(self, artifact_path, item="W1"):
        out = io.StringIO()
        code = self.controller.main([
            "record-stage",
            "--run-id",
            self.run_id,
            "--stage",
            "solution_design",
            "--status",
            "PASS",
            "--artifact",
            f"solution={artifact_path}",
            "--item",
            item,
            "--project",
            str(self.root),
        ], stdout=out)
        return code, out.getvalue()

    def snapshot_files(self):
        paths = [
            self.run_dir / "state.json",
            self.run_dir / "worklist.yml",
            self.run_dir / "events.jsonl",
            self.run_dir / "stage-results" / "06-solution-design.json",
        ]
        result = {}
        for path in paths:
            result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "MISSING"
        return result


class EvidenceTest(V2Fixture):
    def test_solution_artifact_semantics(self):
        from loopx_controller_evidence import validate_solution_semantics

        artifact = self.solution_artifact()
        self.assertEqual(validate_solution_semantics(artifact), [])
        for name in tuple(artifact["quality_attributes"]):
            broken = json.loads(json.dumps(artifact))
            del broken["quality_attributes"][name]
            self.assertTrue(validate_solution_semantics(broken), name)
        broken = json.loads(json.dumps(artifact))
        broken["quality_attributes"]["security"] = {
            "status": "NOT_APPLICABLE",
            "approach": "",
            "reason": "",
            "evidence": [],
        }
        self.assertIn("具体理由", "\n".join(validate_solution_semantics(broken)))

    def test_test_plan_coverage_and_cleanup(self):
        from loopx_controller_evidence import validate_test_plan_semantics

        artifact = {
            "requirement_ids": ["AC-001"],
            "mappings": [{
                "requirement_id": "AC-001", "acceptance_ids": ["AC-001"],
                "rule_ids": ["R1"], "test_case_ids": ["TC1"],
            }],
            "cases": [{
                "id": "TC1",
                "covers": ["AC-001"],
                "risk_tags": [],
                "preconditions": [],
                "data_setup": {"run_id_strategy": "uuid", "records": []},
                "execution": {"entrypoint": "python3 -m unittest", "steps": ["执行"]},
                "assertions": ["退出码为 0"],
                "cleanup": {"steps": ["退出临时目录"], "verification": ["目录不存在"]},
                "expected_result": "PASS",
            }],
        }
        self.assertEqual(validate_test_plan_semantics(artifact, ["R1"]), [])
        artifact["mappings"][0]["acceptance_ids"] = ["AC-002"]
        self.assertIn("未覆盖验收标识：AC-002", "\n".join(validate_test_plan_semantics(artifact, ["R1"])))
        artifact["mappings"][0]["acceptance_ids"] = ["AC-001"]
        for field in ("data_setup", "execution", "assertions", "cleanup"):
            broken = json.loads(json.dumps(artifact))
            broken["cases"][0][field] = {} if field != "assertions" else []
            self.assertTrue(validate_test_plan_semantics(broken, ["R1"]), field)
        broken = json.loads(json.dumps(artifact))
        broken["mappings"][0]["rule_ids"] = []
        self.assertIn("R1", "\n".join(validate_test_plan_semantics(broken, ["R1"])))

    def test_delta_review_requires_digest_baseline(self):
        from loopx_controller_evidence_semantics import validate_review_assurance

        verdict = {"status": "PASS", "evidence": ["evidence.log"], "reason": ""}
        artifact = {
            "extensions": {"review_assurance": {
                "reviewed_snapshot_id": "a" * 64,
                "review_kind": "DELTA",
                "baseline_snapshot_id": "not-a-digest",
                "review_scope": ["方案差异"],
                "checked_dimensions": {
                    name: dict(verdict) for name in (
                        "requirement_coverage", "minimal_modification", "existing_behavior_impact",
                        "interface_contract", "verification_deployment",
                    )
                },
                "blocking_findings": [],
                "unknowns": [],
                "completeness_attestation": "已检查全部差异。",
            }},
        }
        self.assertIn("baseline_snapshot_id", "\n".join(validate_review_assurance(artifact)))

    def test_invalid_review_assurance_returns_structured_binding_errors(self):
        from loopx_controller_evidence import _validate_review_binding

        result = _validate_review_binding(
            self.root,
            {"run_id": self.run_id},
            {"extensions": {"review_assurance": {}}},
        )

        self.assertEqual(len(result), 4)
        self.assertTrue(result[0])

    def test_recover_prepared_multifile_transaction_restores_old_generation(self):
        from loopx_controller_io import recover_atomic_writes

        root = self.root / "journal-recovery"
        root.mkdir()
        first = root / "state.json"
        second = root / "result.json"
        backup = root / ".state.backup"
        first.write_text("new-state", encoding="utf-8")
        second.write_text("new-result", encoding="utf-8")
        backup.write_text("old-state", encoding="utf-8")
        journal = root / ".loopx-transaction-test.json"
        journal.write_text(json.dumps({
            "state": "PREPARED",
            "entries": [
                {"target": "state.json", "temporary": "", "backup": ".state.backup"},
                {"target": "result.json", "temporary": "", "backup": ""},
            ],
        }), encoding="utf-8")

        recover_atomic_writes(root)

        self.assertEqual(first.read_text(encoding="utf-8"), "old-state")
        self.assertFalse(second.exists())
        self.assertFalse(journal.exists())

    def test_recover_transaction_resumes_after_cleanup_interruption(self):
        from loopx_controller_io import recover_atomic_writes

        root = self.root / "journal-cleanup-retry"
        root.mkdir()
        targets = [root / f"target-{index}.json" for index in (1, 2)]
        backups = [root / f".target-{index}.bak" for index in (1, 2)]
        for index, (target, backup) in enumerate(zip(targets, backups), start=1):
            target.write_text(f"new-{index}", encoding="utf-8")
            backup.write_text(f"old-{index}", encoding="utf-8")
        journal = root / ".loopx-transaction-cleanup.json"
        entries = [
            {
                "target": target.name,
                "temporary": "",
                "backup": backup.name,
            }
            for target, backup in zip(targets, backups)
        ]
        journal.write_text(json.dumps({"state": "PREPARED", "entries": entries}), encoding="utf-8")
        original_unlink = Path.unlink
        backup_unlinks = 0

        def interrupt_second_backup(path, *args, **kwargs):
            nonlocal backup_unlinks
            if path.suffix == ".bak":
                backup_unlinks += 1
                if backup_unlinks == 2:
                    raise OSError("模拟恢复清理中断")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=interrupt_second_backup):
            with self.assertRaisesRegex(OSError, "清理中断"):
                recover_atomic_writes(root)

        self.assertEqual(json.loads(journal.read_text(encoding="utf-8"))["state"], "ROLLED_BACK")
        self.assertEqual([path.read_text(encoding="utf-8") for path in targets], ["old-1", "old-2"])
        recover_atomic_writes(root)
        self.assertFalse(journal.exists())
        self.assertEqual([path.read_text(encoding="utf-8") for path in targets], ["old-1", "old-2"])
        self.assertEqual(list(root.glob("*.bak")), [])

    def test_performance_risk_controls_solution_review(self):
        from loopx_controller_evidence import validate_solution_semantics

        self.init(mode="STANDARD", risk_tags=["performance"])
        artifact = self.solution_artifact()
        artifact["performance_targets"] = [{
            "metric": "p95",
            "unit": "ms",
            "target": "<=100",
            "target_source": "已批准规格",
            "load": "每秒 20 请求",
            "environment": "本地固定夹具",
            "baseline": "95ms",
            "allowed_variation": "+5%",
            "evidence": [self.evidence_path()],
        }]
        artifact["rule_results"] = [self.rule_result("PERF-TARGET-001", artifact["performance_targets"][0]["evidence"][0])]
        self.force_current_stage("solution_design")
        design_path = self.write_solution(artifact, "performance-design.json")
        self.assertEqual(self.record_solution(design_path)[0], 0)
        self.force_current_stage("solution_review")
        self.assertEqual(validate_solution_semantics(artifact, ["performance"]), [])
        for field in ("target_source", "load", "environment", "baseline", "allowed_variation"):
            broken = json.loads(json.dumps(artifact))
            broken["performance_targets"][0][field] = ""
            self.assertIn(field, "\n".join(validate_solution_semantics(broken, ["performance"])))

        broken = json.loads(json.dumps(artifact))
        broken["performance_targets"][0]["target_source"] = ""
        broken["stage"] = "solution_review"
        artifact_path = self.write_solution(broken, "performance-review.json")
        state_path = self.run_dir / "state.json"
        events_path = self.run_dir / "events.jsonl"
        result_path = self.run_dir / "stage-results" / "07-solution-review.json"
        before = (state_path.read_bytes(), events_path.read_bytes(), result_path.exists())
        out = io.StringIO()
        args = [
            "record-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--status", "PASS", "--artifact", f"solution={artifact_path}",
            "--project", str(self.root),
        ]
        self.assertEqual(self.controller.main(args, stdout=out), 1)
        self.assertIn("target_source", out.getvalue())
        self.assertEqual(before, (state_path.read_bytes(), events_path.read_bytes(), result_path.exists()))

        artifact = self.make_review_solution(artifact, artifact["performance_targets"][0]["evidence"][0])
        self.write_json(artifact_path, artifact)
        out = io.StringIO()
        self.assertEqual(self.controller.main(args, stdout=out), 0, out.getvalue())
        self.assertEqual(json.loads(result_path.read_text(encoding="utf-8"))["status"], "NEED_HUMAN")

    def test_security_controls_by_risk(self):
        from loopx_controller_evidence import validate_security_semantics

        controls = []
        for control in ("identity", "permission", "input", "sensitive_data", "dependency"):
            controls.append({
                "control": control,
                "status": "PASS",
                "verification": "已检查",
                "evidence": ["check.log"],
                "remaining_risk": "",
            })
        artifact = {"controls": controls}
        self.assertEqual(validate_security_semantics(artifact, ["auth", "permission"]), [])
        artifact["controls"] = [item for item in controls if item["control"] != "permission"]
        self.assertIn("permission", "\n".join(validate_security_semantics(artifact, ["permission"])))


class EvidencePathTest(V2Fixture):
    def test_resolved_path_boundary(self):
        from loopx_controller_evidence import resolve_project_file

        inside = self.write_text("evidence/inside.log")
        relative, _ = resolve_project_file(self.root, inside)
        self.assertEqual(relative, inside)
        for value in (str(self.root / inside), "../outside.log", "missing.log"):
            with self.assertRaises(ValueError):
                resolve_project_file(self.root, value)
        with self.assertRaises(ValueError):
            resolve_project_file(self.root, "evidence")

        outside_root = Path(tempfile.mkdtemp(prefix="loopx-std-outside-"))
        try:
            outside = outside_root / "outside.log"
            outside.write_text("outside", encoding="utf-8")
            link = self.root / "evidence" / "outside-link.log"
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("当前文件系统不支持符号链接")
            with self.assertRaises(ValueError):
                resolve_project_file(self.root, "evidence/outside-link.log")
        finally:
            shutil.rmtree(outside_root)


class LoopxControllerV2Test(V2Fixture):
    def test_init_can_retry_after_atomic_commit_interruption(self):
        args = [
            "init", "验证初始化重试", "--run-id", self.run_id,
            "--mode", "LIGHT", "--project", str(self.root),
        ]
        with mock.patch(
            "loopx_controller_intake.atomic_write_texts",
            side_effect=KeyboardInterrupt("模拟初始化提交中断"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.controller.main(args, stdout=io.StringIO())

        self.assertFalse(any(path.is_file() for path in self.run_dir.rglob("*")))
        out = io.StringIO()
        self.assertEqual(self.controller.main(args, stdout=out), 0, out.getvalue())
        state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["current_stage"], "requirement_intake")
        self.assertEqual(state["stages"]["environment_check"], "PASS")

    def test_init_v2(self):
        state = self.init()
        self.assertEqual(state["contract_version"], "2")
        self.assertEqual(state["catalog_version"], "3")
        snapshot = self.root / state["policy_snapshot"]
        self.assertTrue(snapshot.is_file())
        payload = json.loads(snapshot.read_text(encoding="utf-8"))
        self.assertEqual(payload["digest"], state["policy_snapshot_sha256"])

    def test_record_stage_rejects_non_current_stage(self):
        self.init()
        before = self.snapshot_files()
        out = io.StringIO()
        code = self.controller.main([
            "record-stage", "--run-id", self.run_id, "--stage", "code_review",
            "--status", "PASS", "--evidence", self.evidence_path(), "--project", str(self.root),
        ], stdout=out)
        self.assertEqual(code, 1)
        self.assertIn("当前阶段为 requirement_intake", out.getvalue())
        self.assertEqual(before, self.snapshot_files())

    def test_catalog_v2_solution_review_does_not_require_v3_assurance(self):
        self.init(mode="LIGHT")
        state_path = self.run_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        snapshot_path = self.root / state["policy_snapshot"]
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["catalog_version"] = "2"
        from loopx_controller_policy import canonical_digest

        snapshot.pop("digest", None)
        snapshot["digest"] = canonical_digest(snapshot)
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        state["catalog_version"] = "2"
        state["policy_snapshot_sha256"] = snapshot["digest"]
        state["current_stage"] = "solution_review"
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        artifact = self.solution_artifact()
        artifact["stage"] = "solution_review"
        path = self.write_solution(artifact, "catalog-v2-review.json")
        out = io.StringIO()
        code = self.controller.main([
            "record-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--status", "PASS", "--artifact", f"solution={path}", "--project", str(self.root),
        ], stdout=out)
        self.assertEqual(code, 0, out.getvalue())

    def test_record_stage_pass_with_artifacts(self):
        self.init()
        self.force_current_stage("solution_design")
        artifact_path = self.write_solution()
        code, output = self.record_solution(artifact_path)
        self.assertEqual(code, 0, output)
        result = json.loads((self.run_dir / "stage-results" / "06-solution-design.json").read_text(encoding="utf-8"))
        self.assertEqual(result["contract_version"], "2")
        self.assertEqual(result["artifacts"][0]["type"], "solution")
        self.assertEqual(result["artifacts"][0]["path"], artifact_path)
        self.assertEqual(result["artifacts"][0]["sha256"], hashlib.sha256((self.root / artifact_path).read_bytes()).hexdigest())
        self.assertTrue(result["evidence"])
        worklist = (self.run_dir / "worklist.yml").read_text(encoding="utf-8")
        self.assertIn("id: W1", worklist)
        self.assertIn("status: pending", worklist)

    def test_record_stage_rejects_invalid_evidence_matrix(self):
        self.init()
        before = self.snapshot_files()
        out = io.StringIO()
        self.assertEqual(self.controller.main([
            "record-stage", "--run-id", self.run_id, "--stage", "requirement_intake",
            "--status", "PASS", "--project", str(self.root),
        ], stdout=out), 1)
        self.assertIn("至少一个有效证据文件", out.getvalue())
        self.assertEqual(before, self.snapshot_files())

        evidence = self.evidence_path()
        out = io.StringIO()
        self.assertEqual(self.controller.main([
            "record-stage", "--run-id", self.run_id, "--stage", "requirement_intake",
            "--status", "ACCEPTED_RISK", "--evidence", evidence,
            "--project", str(self.root),
        ], stdout=out), 1)
        self.assertIn("不接受阶段级 ACCEPTED_RISK", out.getvalue())
        self.assertEqual(before, self.snapshot_files())

        self.force_current_stage("solution_design")
        before = self.snapshot_files()
        code, output = self.record_solution("missing.json")
        self.assertEqual(code, 1)
        self.assertIn("不存在", output)
        self.assertEqual(before, self.snapshot_files())

        artifact_directory = (self.run_dir / "artifacts").relative_to(self.root).as_posix()
        code, output = self.record_solution(artifact_directory)
        self.assertEqual(code, 1)
        self.assertIn("普通文件", output)
        self.assertEqual(before, self.snapshot_files())

        mutations = (
            ("artifact_version", "9", "artifact_version"),
            ("artifact_type", "test_plan", "artifact_type"),
            ("run_id", "another-run", "run_id"),
            ("stage", "solution_review", "阶段"),
        )
        for field, value, message in mutations:
            with self.subTest(field=field):
                artifact = self.solution_artifact()
                artifact[field] = value
                path = self.write_solution(artifact)
                code, output = self.record_solution(path)
                self.assertEqual(code, 1)
                self.assertIn(message, output)
                self.assertEqual(before, self.snapshot_files())

    def test_required_rule_failure_and_unconfirmed_acceptance_are_rejected(self):
        self.init(mode="STANDARD", risk_tags=["api_contract"])
        self.force_current_stage("solution_design")
        evidence = self.evidence_path()
        for status, reason, message in (
            ("CHANGES_REQUIRED", "兼容方案不完整", "未通过"),
            ("ACCEPTED_RISK", "接受兼容风险", "缺少用户确认"),
        ):
            with self.subTest(status=status):
                artifact = self.solution_artifact(evidence=evidence)
                artifact["rule_results"] = [
                    self.rule_result("ARCH-BOUNDARY-002", evidence),
                    {
                        "rule_id": "ARCH-COMPAT-003",
                        "status": status,
                        "evidence": [evidence],
                        "reason": reason,
                    },
                ]
                path = self.write_solution(artifact)
                before = self.snapshot_files()
                code, output = self.record_solution(path)
                self.assertEqual(code, 1)
                self.assertIn(message, output)
                self.assertEqual(before, self.snapshot_files())

    def test_record_stage_failure_is_atomic(self):
        self.init()
        self.force_current_stage("solution_design")
        path = self.write_solution()
        artifact = json.loads((self.root / path).read_text(encoding="utf-8"))
        artifact["quality_attributes"]["security"]["evidence"] = ["missing-security.log"]
        self.write_json(path, artifact)
        before = self.snapshot_files()
        code, _ = self.record_solution(path)
        self.assertEqual(code, 1)
        self.assertEqual(before, self.snapshot_files())

    def test_atomic_writer_restores_replaced_files_after_storage_error(self):
        from loopx_controller_io import atomic_write_texts

        targets = [self.root / f"target-{index}.txt" for index in range(1, 5)]
        original_replace = Path.replace
        for fail_index in (2, 3, 4):
            with self.subTest(fail_index=fail_index):
                for index, target in enumerate(targets, start=1):
                    target.write_text(f"old-{index}", encoding="utf-8")
                calls = 0

                def fail_selected(source, target):
                    nonlocal calls
                    calls += 1
                    if calls == fail_index:
                        raise OSError(f"模拟第 {fail_index} 个目标写入失败")
                    return original_replace(source, target)

                with mock.patch.object(Path, "replace", new=fail_selected):
                    with self.assertRaisesRegex(OSError, f"第 {fail_index} 个目标"):
                        atomic_write_texts({
                            target: f"new-{index}"
                            for index, target in enumerate(targets, start=1)
                        })

                for index, target in enumerate(targets, start=1):
                    self.assertEqual(target.read_text(encoding="utf-8"), f"old-{index}")
                self.assertEqual(list(self.root.glob(".*.tmp")), [])
                self.assertEqual(list(self.root.glob(".*.bak")), [])

    def test_atomic_writer_can_resume_after_partial_rollback_failure(self):
        import loopx_controller_io

        targets = [self.root / f"recover-target-{index}.txt" for index in range(1, 4)]
        for index, target in enumerate(targets, start=1):
            target.write_text(f"old-{index}", encoding="utf-8")
        original_replace = Path.replace
        original_restore = loopx_controller_io._restore_backup
        forward_calls = 0
        rollback_calls = 0

        def fail_third_forward(source, target):
            nonlocal forward_calls
            forward_calls += 1
            if forward_calls == 3:
                raise OSError("模拟前向第三次写入失败")
            return original_replace(source, target)

        def fail_second_rollback(backup, target):
            nonlocal rollback_calls
            rollback_calls += 1
            if rollback_calls == 2:
                raise OSError("模拟部分回滚失败")
            return original_restore(backup, target)

        with mock.patch.object(Path, "replace", new=fail_third_forward), mock.patch.object(
            loopx_controller_io, "_restore_backup", new=fail_second_rollback,
        ):
            with self.assertRaisesRegex(RuntimeError, "无法完整恢复"):
                loopx_controller_io.atomic_write_texts({
                    target: f"new-{index}"
                    for index, target in enumerate(targets, start=1)
                })

        self.assertEqual(len(list(self.root.glob(".loopx-transaction-*.json"))), 1)
        loopx_controller_io.recover_atomic_writes(self.root)
        for index, target in enumerate(targets, start=1):
            self.assertEqual(target.read_text(encoding="utf-8"), f"old-{index}")
        self.assertEqual(list(self.root.glob(".loopx-transaction-*.json")), [])
        self.assertEqual(list(self.root.glob(".*.bak")), [])
        self.assertEqual(list(self.root.glob(".*.restore")), [])

    def test_record_stage_restores_all_targets_after_storage_error(self):
        self.init()
        self.force_current_stage("solution_design")
        artifact_path = self.write_solution()
        before = self.snapshot_files()
        original_replace = Path.replace
        calls = 0

        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            return original_replace(source, target)

        out = io.StringIO()
        with mock.patch.object(Path, "replace", new=fail_second):
            code = self.controller.main([
                "record-stage", "--run-id", self.run_id, "--stage", "solution_design",
                "--status", "PASS", "--artifact", f"solution={artifact_path}",
                "--item", "W1", "--project", str(self.root),
            ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("阶段记录写入失败", out.getvalue())
        self.assertIn("disk full", out.getvalue())
        self.assertEqual(before, self.snapshot_files())
        self.assertEqual(list(self.run_dir.rglob("*.tmp")), [])
        self.assertEqual(list(self.run_dir.rglob("*.bak")), [])

    def test_mode_selection_updates_snapshot_and_fails_without_partial_writes(self):
        self.init(mode="LIGHT", risk_tags=["core_state_transition"])
        state_path = self.run_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("stages", {})["spec_review"] = "PASS"
        state["current_stage"] = "mode_selection"
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        out = io.StringIO()
        self.assertEqual(self.controller.main([
            "mode", self.run_id, "--select", "FULL", "--project", str(self.root),
        ], stdout=out), 0, out.getvalue())
        selected = json.loads(state_path.read_text(encoding="utf-8"))
        snapshot_path = self.root / selected["policy_snapshot"]
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(selected["mode"], "FULL")
        self.assertEqual(snapshot["mode"], "FULL")
        self.assertEqual(selected["policy_snapshot_sha256"], snapshot["digest"])
        self.assertIn("ARCH-BOUNDARY-002", {rule["id"] for rule in snapshot["rules"]})
        self.assertIn("mode: FULL", (self.run_dir / "worklist.yml").read_text(encoding="utf-8"))

        selected["current_stage"] = "solution_design"
        state_path.write_text(json.dumps(selected, ensure_ascii=False), encoding="utf-8")
        snapshot_before = snapshot_path.read_bytes()
        out = io.StringIO()
        self.assertEqual(self.controller.main([
            "mode", self.run_id, "--select", "STANDARD", "--project", str(self.root),
        ], stdout=out), 1)
        self.assertIn("只能在 mode_selection 阶段", out.getvalue())
        self.assertEqual(snapshot_before, snapshot_path.read_bytes())

        failed_run = f"{self.run_id}-failed"
        self.run_id = failed_run
        self.init(mode="LIGHT", risk_tags=["core_state_transition"])
        failed_state_path = self.run_dir / "state.json"
        failed_state = json.loads(failed_state_path.read_text(encoding="utf-8"))
        failed_state.setdefault("stages", {})["spec_review"] = "PASS"
        failed_state["current_stage"] = "mode_selection"
        failed_state_path.write_text(json.dumps(failed_state, ensure_ascii=False), encoding="utf-8")
        snapshot_path = self.root / failed_state["policy_snapshot"]
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["mode"] = "FULL"
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        before = self.snapshot_files()
        out = io.StringIO()
        self.assertEqual(self.controller.main([
            "mode", self.run_id, "--select", "FULL", "--project", str(self.root),
        ], stdout=out), 1)
        self.assertIn("摘要校验失败", out.getvalue())
        self.assertEqual(before, self.snapshot_files())
        self.assertFalse((self.run_dir / "artifacts" / "mode-decision.json").exists())
        self.assertFalse((self.run_dir / "stage-results" / "05-mode-selection.json").exists())

    def test_rule_acceptance_requires_matching_quality_confirmation(self):
        self.init(mode="STANDARD", risk_tags=["reliability"])
        evidence = self.evidence_path()
        confirmation = self.write_text(
            f"docs/loopx/runs/{self.run_id}/artifacts/risk-confirmation.txt",
            "用户确认接受 OBS-EVIDENCE-001 的剩余风险。\n",
        )
        document = self.document_path("quality.md")
        artifact = self.quality_artifact(evidence, document)
        artifact["rule_results"] = [{
            "rule_id": "OBS-EVIDENCE-001",
            "status": "ACCEPTED_RISK",
            "evidence": [evidence],
            "reason": "本地无法覆盖真实 CI 可观测性",
        }]
        state_path = self.run_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["mode_decision"]["accepted_risk"] = {
            "selected_lower_than_recommended": True,
            "reason": "整体等级降级确认不能替代逐规则确认",
        }
        state["current_stage"] = "quality_audit"
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        path = self.write_json(
            f"docs/loopx/runs/{self.run_id}/artifacts/quality-result.json",
            artifact,
        )
        out = io.StringIO()
        args = [
            "record-stage", "--run-id", self.run_id, "--stage", "quality_audit",
            "--status", "PASS", "--artifact", f"quality_result={path}",
            "--project", str(self.root),
        ]
        self.assertEqual(self.controller.main(args, stdout=out), 1)
        self.assertIn("缺少逐规则风险接受确认", out.getvalue())

        artifact["accepted_risks"] = [{
            "rule_id": "OBS-EVIDENCE-001",
            "reason": "真实 CI 可观测性由用户接受为剩余风险",
            "confirmation_evidence": confirmation,
        }]
        self.write_json(path, artifact)
        out = io.StringIO()
        self.assertEqual(self.controller.main(args, stdout=out), 0, out.getvalue())

    def test_strict_validate_v2_artifacts(self):
        self.init()
        self.force_current_stage("solution_design")
        artifact_path = self.write_solution()
        code, output = self.record_solution(artifact_path)
        self.assertEqual(code, 0, output)

        def strict_result():
            out = io.StringIO()
            code = self.controller.main([
                "validate", self.run_id, "--strict", "--project", str(self.root)
            ], stdout=out)
            return code, out.getvalue()

        self.assertEqual(strict_result()[0], 0)
        evidence = self.run_dir / "artifacts" / "check.log"
        evidence.unlink()
        code, output = strict_result()
        self.assertEqual(code, 1)
        self.assertIn("v2 证据复核失败", output)
        evidence.write_text("证据\n", encoding="utf-8")

        artifact = self.root / artifact_path
        original_artifact = artifact.read_text(encoding="utf-8")
        for field, value, expected in (
            ("artifact_version", "9", "artifact_version"),
            ("stage", "solution_review", "阶段"),
        ):
            with self.subTest(field=field):
                mutated = json.loads(original_artifact)
                mutated[field] = value
                artifact.write_text(json.dumps(mutated, ensure_ascii=False), encoding="utf-8")
                code, output = strict_result()
                self.assertEqual(code, 1)
                self.assertIn(expected, output)
                artifact.write_text(original_artifact, encoding="utf-8")

        mutated = json.loads(original_artifact)
        mutated["rule_results"][0]["rule_id"] = "UNKNOWN-RULE"
        artifact.write_text(json.dumps(mutated, ensure_ascii=False), encoding="utf-8")
        code, output = strict_result()
        self.assertEqual(code, 1)
        self.assertIn("未选择的规则", output)
        artifact.write_text(original_artifact, encoding="utf-8")

        state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        snapshot_path = self.root / state["policy_snapshot"]
        original_snapshot = snapshot_path.read_text(encoding="utf-8")
        snapshot = json.loads(original_snapshot)
        snapshot["mode"] = "FULL"
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        code, output = strict_result()
        self.assertEqual(code, 1)
        self.assertIn("摘要校验失败", output)
        snapshot_path.write_text(original_snapshot, encoding="utf-8")
        self.assertEqual(strict_result()[0], 0)

    def test_retry_and_duplicate_submission(self):
        self.init()
        self.force_current_stage("solution_design")
        missing = self.solution_artifact()
        missing["quality_attributes"]["security"]["evidence"] = ["not-created.log"]
        path = self.write_solution(missing)
        self.assertEqual(self.record_solution(path)[0], 1)
        self.write_text("not-created.log")
        self.assertEqual(self.record_solution(path)[0], 0)
        events = (self.run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertEqual(self.record_solution(path)[0], 0)
        self.assertEqual(events, (self.run_dir / "events.jsonl").read_text(encoding="utf-8"))

    def test_v2_confirmation_requires_project_file(self):
        self.init()
        evidence = self.evidence_path()
        self.force_current_stage("solution_review")
        out = io.StringIO()
        self.assertEqual(self.controller.main([
            "record-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--status", "PASS", "--evidence", evidence,
            "--project", str(self.root),
        ], stdout=out), 0, out.getvalue())
        state_path = self.run_dir / "state.json"
        before = state_path.read_bytes()
        out = io.StringIO()
        self.assertEqual(self.controller.main([
            "confirm-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--evidence", "用户已确认", "--project", str(self.root),
        ], stdout=out), 1)
        self.assertIn("不存在", out.getvalue())
        self.assertEqual(before, state_path.read_bytes())

        confirmation = self.write_text(
            f"docs/loopx/runs/{self.run_id}/artifacts/solution-confirmation.txt",
            "用户确认方案。\n",
        )
        tracked = [
            self.run_dir / "state.json",
            self.run_dir / "worklist.yml",
            self.run_dir / "events.jsonl",
            self.run_dir / "stage-results" / "07-solution-review.json",
        ]
        before_confirmation = {path: path.read_bytes() for path in tracked}
        with mock.patch(
            "loopx_controller_flow.atomic_write_texts",
            side_effect=KeyboardInterrupt("模拟确认提交中断"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.controller.main([
                    "confirm-stage", "--run-id", self.run_id, "--stage", "solution_review",
                    "--evidence", confirmation, "--project", str(self.root),
                ], stdout=io.StringIO())
        self.assertEqual(before_confirmation, {path: path.read_bytes() for path in tracked})

        out = io.StringIO()
        self.assertEqual(self.controller.main([
            "confirm-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--evidence", confirmation, "--project", str(self.root),
        ], stdout=out), 0, out.getvalue())


class LoopxControllerV2E2ETest(V2Fixture):
    def prepare_full_solution_design(self):
        self.init(mode="FULL", risk_tags=["core_state_transition"])
        self.force_current_stage("solution_design")
        evidence = self.evidence_path()
        solution = self.solution_artifact(evidence=evidence)
        solution["rule_results"] = [
            self.rule_result(rule_id, evidence)
            for rule_id in ("ARCH-BOUNDARY-002", "ARCH-COMPAT-003", "REL-RECOVERY-001", "ARCH-SIMPLE-001")
        ]
        path = self.write_solution(solution, "bound-design.json")
        self.assertEqual(self.record_solution(path)[0], 0)
        self.force_current_stage("solution_review")
        return evidence, solution

    def test_solution_review_snapshot_must_match_recorded_design(self):
        evidence, solution = self.prepare_full_solution_design()
        review = self.make_review_solution(solution, evidence)
        review["extensions"]["review_assurance"]["reviewed_snapshot_id"] = "0" * 64
        path = self.write_solution(review, "wrong-snapshot-review.json")
        out = io.StringIO()
        code = self.controller.main([
            "record-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--status", "PASS", "--artifact", f"solution={path}", "--project", str(self.root),
        ], stdout=out)
        self.assertEqual(code, 1)
        self.assertIn("与已记录的 solution_design 内容摘要不一致", out.getvalue())

    def test_solution_design_artifact_tamper_is_detected_before_review(self):
        evidence, solution = self.prepare_full_solution_design()
        result = json.loads(
            (self.run_dir / "stage-results" / "06-solution-design.json").read_text(encoding="utf-8")
        )
        design_path = self.root / result["artifacts"][0]["path"]
        tampered = json.loads(design_path.read_text(encoding="utf-8"))
        tampered["decisions"][0]["summary"] = "记录通过后被替换的方案"
        design_path.write_text(json.dumps(tampered, ensure_ascii=False, indent=2), encoding="utf-8")
        review = self.make_review_solution(tampered, evidence)
        path = self.write_solution(review, "tampered-design-review.json")
        out = io.StringIO()
        code = self.controller.main([
            "record-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--status", "PASS", "--artifact", f"solution={path}", "--project", str(self.root),
        ], stdout=out)
        self.assertEqual(code, 1)
        self.assertIn("solution_design 产物在记录通过后发生变化", out.getvalue())

    def test_delta_review_must_reference_previous_persisted_review(self):
        evidence, solution = self.prepare_full_solution_design()
        review = self.make_review_solution(solution, evidence)
        review["extensions"]["review_assurance"]["review_kind"] = "DELTA"
        review["extensions"]["review_assurance"]["baseline_snapshot_id"] = "1" * 64
        path = self.write_solution(review, "orphan-delta-review.json")
        out = io.StringIO()
        code = self.controller.main([
            "record-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--status", "PASS", "--artifact", f"solution={path}", "--project", str(self.root),
        ], stdout=out)
        self.assertEqual(code, 1)
        self.assertIn("必须引用上一次已记录的方案审核快照", out.getvalue())

    def test_delta_baseline_survives_fail_review_overwrite(self):
        self.init_git_repository()
        evidence, solution = self.prepare_full_solution_design()
        full_review = self.make_review_solution(solution, evidence)
        full_path = self.write_solution(full_review, "full-review.json")
        self.controller_command(
            "record-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--status", "PASS", "--artifact", f"solution={full_path}", "--item", "W1",
        )
        baseline = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))["last_solution_review"]
        self.controller_command(
            "fail-review", "--run-id", self.run_id, "--from", "solution_review",
            "--return-to", "solution_design", "--item", "W1", "--reason", "需要复核方案",
        )
        after_failure = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(after_failure["last_solution_review"], baseline)
        self.force_current_stage("solution_review")
        delta = self.make_review_solution(solution, evidence)
        delta["extensions"]["review_assurance"]["review_kind"] = "DELTA"
        delta["extensions"]["review_assurance"]["baseline_snapshot_id"] = baseline["reviewed_snapshot_id"]
        delta_path = self.write_solution(delta, "delta-after-failure.json")
        out = self.controller_command(
            "record-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--status", "PASS", "--artifact", f"solution={delta_path}", "--item", "W1",
        )
        self.assertIn("NEED_HUMAN 已记录阶段：solution_review", out)

    def test_delta_review_rejects_changed_git_source_snapshot(self):
        self.init_git_repository()
        evidence, solution = self.prepare_full_solution_design()
        full_review = self.make_review_solution(solution, evidence)
        full_path = self.write_solution(full_review, "source-full-review.json")
        self.controller_command(
            "record-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--status", "PASS", "--artifact", f"solution={full_path}", "--item", "W1",
        )
        state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        baseline = state["last_solution_review"]["reviewed_snapshot_id"]
        self.write_text("business.py", "VALUE = 2\n")
        subprocess.run(["git", "add", "business.py"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(
            [
                "git", "-c", "user.name=LoopX Test", "-c", "user.email=loopx@example.invalid",
                "commit", "-q", "-m", "change source",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        delta = self.make_review_solution(solution, evidence)
        delta["extensions"]["review_assurance"]["review_kind"] = "DELTA"
        delta["extensions"]["review_assurance"]["baseline_snapshot_id"] = baseline
        delta_path = self.write_solution(delta, "source-delta-review.json")
        out = io.StringIO()

        code = self.controller.main([
            "record-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--status", "PASS", "--artifact", f"solution={delta_path}", "--item", "W1",
            "--project", str(self.root),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("必须执行 FULL 方案审核", out.getvalue())

        # 开发阶段改源码是正常行为；严格复检应复用审核当时的冻结快照，
        # 只在再次提交 DELTA 时比较实时源码。
        from loopx_controller_evidence import validate_recorded_v2_stage
        from loopx_controller_io import load_worklist

        persisted_state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        _, worklist = load_worklist(self.root, persisted_state)
        recorded = json.loads(
            (self.run_dir / "stage-results" / "07-solution-review.json").read_text(encoding="utf-8")
        )
        validate_recorded_v2_stage(
            self.root, persisted_state, "solution_review", recorded, worklist,
        )

    def test_delta_review_rejects_unavailable_source_snapshot(self):
        evidence, solution = self.prepare_full_solution_design()
        full_review = self.make_review_solution(solution, evidence)
        full_path = self.write_solution(full_review, "unavailable-full-review.json")
        self.controller_command(
            "record-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--status", "PASS", "--artifact", f"solution={full_path}", "--item", "W1",
        )
        state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        delta = self.make_review_solution(solution, evidence)
        delta["extensions"]["review_assurance"]["review_kind"] = "DELTA"
        delta["extensions"]["review_assurance"]["baseline_snapshot_id"] = state[
            "last_solution_review"
        ]["reviewed_snapshot_id"]
        delta_path = self.write_solution(delta, "unavailable-delta-review.json")
        out = io.StringIO()

        code = self.controller.main([
            "record-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--status", "PASS", "--artifact", f"solution={delta_path}", "--item", "W1",
            "--project", str(self.root),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("源码快照不可用", out.getvalue())

    def test_frozen_manifest_tamper_blocks_downstream_record_immediately(self):
        self.init(mode="FULL")
        state_path = self.run_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.write_text(state["spec"]["artifact"], "# 需求规格\n\n## 验收标准\n\nAC-001\n")
        manifest_path = state["spec"]["extensions"]["requirement_manifest"]
        manifest = {
            "version": "1", "requirement_ids": ["REQ-001"], "acceptance_ids": ["AC-001"],
            "delivery_units": [{
                "id": "DU-001", "source_refs": ["test"], "requirement_ids": ["REQ-001"],
                "acceptance_ids": ["AC-001"], "modules": ["loopx/tools"], "deploy_targets": [],
                "depends_on": [], "independently_releasable": True,
            }],
            "deferred": [], "delivery_strategy": "SINGLE_RUN", "coupled_reason": "",
        }
        self.write_json(manifest_path, manifest)
        from loopx_controller_requirements import apply_requirement_freeze, prepare_requirement_freeze

        apply_requirement_freeze(state, prepare_requirement_freeze(self.root, state))
        state["stages"]["spec_review"] = "PASS"
        state["current_stage"] = "solution_design"
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        manifest["requirement_ids"] = ["REQ-002"]
        manifest["delivery_units"][0]["requirement_ids"] = ["REQ-002"]
        self.write_json(manifest_path, manifest)
        artifact = self.solution_artifact()
        artifact["requirement_ids"] = ["REQ-002"]
        artifact["extensions"] = self.requirement_extensions()
        path = self.write_solution(artifact, "tampered-manifest-solution.json")
        code, output = self.record_solution(path)
        self.assertEqual(code, 1)
        self.assertIn("规格审核通过后发生变化", output)

    def test_test_plan_distinguishes_requirement_and_acceptance_ids(self):
        self.init(mode="FULL")
        state_path = self.run_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.write_text(state["spec"]["artifact"], "# 需求规格\n\n## 验收标准\n\nAC-001\n")
        manifest = {
            "version": "1", "requirement_ids": ["REQ-001"], "acceptance_ids": ["AC-001"],
            "delivery_units": [{
                "id": "DU-001", "source_refs": ["test"], "requirement_ids": ["REQ-001"],
                "acceptance_ids": ["AC-001"], "modules": ["loopx/tools"], "deploy_targets": [],
                "depends_on": [], "independently_releasable": True,
            }],
            "deferred": [], "delivery_strategy": "SINGLE_RUN", "coupled_reason": "",
        }
        self.write_json(state["spec"]["extensions"]["requirement_manifest"], manifest)
        from loopx_controller_requirements import apply_requirement_freeze, prepare_requirement_freeze, validate_artifact_requirements

        apply_requirement_freeze(state, prepare_requirement_freeze(self.root, state))
        state["stages"]["spec_review"] = "PASS"
        artifact = {
            "requirement_ids": ["REQ-001"],
            "mappings": [{"requirement_id": "REQ-001", "acceptance_ids": ["AC-001"]}],
            "extensions": {"requirement_manifest_sha256": state["spec"]["extensions"]["requirement_manifest_sha256"]},
        }
        self.assertEqual(validate_artifact_requirements(self.root, state, "test_plan", artifact), [])
        artifact["mappings"][0]["acceptance_ids"] = []
        self.assertIn("AC-001", "\n".join(validate_artifact_requirements(self.root, state, "test_plan", artifact)))

    def test_init_structured_design_confirmation_and_strict_validation(self):
        self.init(mode="FULL", risk_tags=["core_state_transition"])
        self.force_current_stage("solution_design")
        evidence = self.evidence_path()
        solution = self.solution_artifact(evidence=evidence)
        solution["rule_results"] = [
            {"rule_id": rule_id, "status": "PASS", "evidence": [evidence], "reason": ""}
            for rule_id in (
                "ARCH-BOUNDARY-002",
                "ARCH-COMPAT-003",
                "REL-RECOVERY-001",
                "ARCH-SIMPLE-001",
            )
        ]
        artifact_path = self.write_solution(solution)
        code, output = self.record_solution(artifact_path)
        self.assertEqual(code, 0, output)

        self.force_current_stage("solution_review")
        review_solution = self.make_review_solution(solution, evidence)
        review_path = self.write_solution(review_solution, "solution-review.json")

        out = io.StringIO()
        self.assertEqual(self.controller.main([
            "record-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--status", "PASS", "--artifact", f"solution={review_path}",
            "--item", "W1", "--project", str(self.root),
        ], stdout=out), 0, out.getvalue())
        confirmation = self.write_text(
            f"docs/loopx/runs/{self.run_id}/artifacts/e2e-confirmation.txt",
            "用户确认结构化方案。\n",
        )
        out = io.StringIO()
        self.assertEqual(self.controller.main([
            "confirm-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--evidence", confirmation, "--project", str(self.root),
        ], stdout=out), 0, out.getvalue())

        out = io.StringIO()
        self.assertEqual(self.controller.main([
            "validate", self.run_id, "--strict", "--project", str(self.root),
        ], stdout=out), 0, out.getvalue())
        state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["contract_version"], "2")
        self.assertEqual(state["stages"]["solution_review"], "PASS")
        self.assertIn("id: W1", (self.run_dir / "worklist.yml").read_text(encoding="utf-8"))

    def test_frozen_requirement_manifest_rejects_downstream_shrink(self):
        self.init(mode="FULL", risk_tags=["core_state_transition"])
        self.force_current_stage("solution_design")
        evidence = self.evidence_path()
        state_path = self.run_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.write_text(state["spec"]["artifact"], "# 需求规格\n\n## 验收标准\n\nAC-001\n")
        self.write_json(state["spec"]["extensions"]["requirement_manifest"], {
            "version": "1",
            "requirement_ids": ["REQ-001", "REQ-002"],
            "acceptance_ids": ["AC-001", "AC-002"],
            "delivery_units": [{
                "id": "DU-001", "source_refs": ["test"],
                "requirement_ids": ["REQ-001", "REQ-002"],
                "acceptance_ids": ["AC-001", "AC-002"],
                "modules": ["loopx/tools"], "deploy_targets": [], "depends_on": [],
                "independently_releasable": True,
            }],
            "deferred": [], "delivery_strategy": "SINGLE_RUN", "coupled_reason": "",
        })
        from loopx_controller_requirements import apply_requirement_freeze, prepare_requirement_freeze

        apply_requirement_freeze(state, prepare_requirement_freeze(self.root, state))
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        artifact = self.solution_artifact(evidence=evidence)
        artifact["requirement_ids"] = ["REQ-001"]
        artifact["extensions"] = self.requirement_extensions()
        path = self.write_solution(artifact)

        code, output = self.record_solution(path)

        self.assertEqual(code, 1)
        self.assertIn("必须等于冻结的活动需求全集", output)

    def test_solution_review_rejects_unknown_sub_verdict(self):
        self.init(mode="FULL", risk_tags=["core_state_transition"])
        self.force_current_stage("solution_design")
        evidence = self.evidence_path()
        solution = self.solution_artifact(evidence=evidence)
        solution["rule_results"] = [
            self.rule_result(rule_id, evidence)
            for rule_id in ("ARCH-BOUNDARY-002", "ARCH-COMPAT-003", "REL-RECOVERY-001", "ARCH-SIMPLE-001")
        ]
        solution_path = self.write_solution(solution, "unknown-design.json")
        self.assertEqual(self.record_solution(solution_path)[0], 0)
        self.force_current_stage("solution_review")
        review = self.make_review_solution(solution, evidence)
        review["extensions"]["review_assurance"]["checked_dimensions"]["existing_behavior_impact"] = {
            "status": "UNKNOWN", "evidence": [], "reason": "调用方尚未核实",
        }
        path = self.write_solution(review, "unknown-review.json")

        out = io.StringIO()
        code = self.controller.main([
            "record-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--status", "PASS", "--artifact", f"solution={path}", "--project", str(self.root),
        ], stdout=out)

        self.assertEqual(code, 1)
        self.assertIn("UNKNOWN", out.getvalue())

    def test_full_v2_seventeen_stage_close(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True, text=True)
        self.write_text(".github/workflows/ci.yml", "name: LoopX CI\n")
        self.init(mode="FULL", risk_tags=["core_state_transition"])
        evidence = self.evidence_path()

        def record(stage, *, artifact=None, item=None, stage_evidence=None):
            args = [
                "record-stage", "--run-id", self.run_id, "--stage", stage,
                "--status", "PASS", "--evidence", stage_evidence or evidence,
            ]
            if artifact:
                args.extend(["--artifact", artifact])
            if item:
                args.extend(["--item", item])
            self.controller_command(*args)

        def next_stage():
            self.controller_command("next", self.run_id)

        record("requirement_intake")
        next_stage()

        self.controller_command("interview", self.run_id)
        interview_path = self.run_dir / "artifacts" / "interview.md"
        interview = interview_path.read_text(encoding="utf-8")
        for marker in ("待用户回答", "待采访确认", "待确认", "未回答", "TBD", "TODO"):
            interview = interview.replace(marker, "已明确")
        interview_path.write_text(interview, encoding="utf-8")
        record("requirement_interview", stage_evidence=interview_path.relative_to(self.root).as_posix())
        interview_confirmation = self.write_text(
            f"docs/loopx/runs/{self.run_id}/artifacts/interview-confirmation.txt",
            "用户确认需求采访。\n",
        )
        self.controller_command(
            "confirm-stage", "--run-id", self.run_id, "--stage", "requirement_interview",
            "--evidence", interview_confirmation,
        )
        next_stage()

        self.controller_command("spec", self.run_id)
        spec_relative = f"docs/loopx/runs/{self.run_id}/artifacts/spec.md"
        self.write_text(spec_relative, """# 需求规格

## 摘要
验证 FULL v2 完整流程。

## 期望行为
全部阶段由控制器推进并可严格复核。

## 验收标准
AC-001：运行完成并成功收口。

## 范围内
控制器、本地文件产物和临时 Git 仓库。

## 范围外
远端发布和外部系统调用。

## 边界情况
证据缺失、确认缺失和工作项未完成会阻塞。

## 测试策略
执行结构化产物、健康检查和严格检查。

## 执行等级决策
使用 FULL 等级并命中核心状态风险。
""")
        self.write_json(
            f"docs/loopx/runs/{self.run_id}/artifacts/requirement-manifest.json",
            {
                "version": "1",
                "requirement_ids": ["AC-001"],
                "acceptance_ids": ["AC-001"],
                "delivery_units": [{
                    "id": "DU-001",
                    "source_refs": ["test"],
                    "requirement_ids": ["AC-001"],
                    "acceptance_ids": ["AC-001"],
                    "modules": ["loopx/tools"],
                    "deploy_targets": [],
                    "depends_on": [],
                    "independently_releasable": True,
                }],
                "deferred": [],
                "delivery_strategy": "SINGLE_RUN",
                "coupled_reason": "",
            },
        )
        record("spec_draft", stage_evidence=spec_relative)
        next_stage()
        record("spec_review", stage_evidence=spec_relative)
        next_stage()

        self.controller_command("mode", self.run_id, "--select", "FULL")
        next_stage()

        solution = self.solution_artifact(evidence=evidence)
        solution["rule_results"] = [
            self.rule_result(rule_id, evidence)
            for rule_id in (
                "ARCH-BOUNDARY-002",
                "ARCH-COMPAT-003",
                "REL-RECOVERY-001",
                "ARCH-SIMPLE-001",
            )
        ]
        solution_path = self.write_solution(solution)
        record("solution_design", artifact=f"solution={solution_path}", item="W1")
        next_stage()
        review_solution = self.make_review_solution(solution, evidence)
        review_solution_path = self.write_solution(review_solution, "solution-review.json")
        record("solution_review", artifact=f"solution={review_solution_path}", item="W1")
        solution_confirmation = self.write_text(
            f"docs/loopx/runs/{self.run_id}/artifacts/solution-confirmation.txt",
            "用户确认方案审核结论。\n",
        )
        self.controller_command(
            "confirm-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--evidence", solution_confirmation,
        )
        next_stage()

        state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        snapshot = json.loads((self.root / state["policy_snapshot"]).read_text(encoding="utf-8"))
        required_rule_ids = [rule["id"] for rule in snapshot["rules"] if rule["level"] == "required"]
        test_document = self.document_path("test-plan.md")
        test_plan = self.build_test_plan_artifact(evidence, test_document, required_rule_ids)
        test_plan_path = self.write_json(
            f"docs/loopx/runs/{self.run_id}/artifacts/test-plan.json",
            test_plan,
        )
        record("test_design", artifact=f"test_plan={test_plan_path}", item="W1")
        next_stage()
        review_test_plan = json.loads(json.dumps(test_plan))
        review_test_plan["stage"] = "test_review"
        review_test_plan_path = self.write_json(
            f"docs/loopx/runs/{self.run_id}/artifacts/test-plan-review.json",
            review_test_plan,
        )
        record("test_review", artifact=f"test_plan={review_test_plan_path}", item="W1")
        next_stage()

        development_document = self.document_path("development.md")
        development = self.development_artifact(evidence, development_document)
        development_path = self.write_json(
            f"docs/loopx/runs/{self.run_id}/artifacts/development-evidence.json",
            development,
        )
        record(
            "development",
            artifact=f"development_evidence={development_path}",
            item="W1",
        )
        next_stage()

        quality_document = self.document_path("quality.md")
        quality = self.quality_artifact(evidence, quality_document)
        quality_path = self.write_json(
            f"docs/loopx/runs/{self.run_id}/artifacts/quality-result.json",
            quality,
        )
        record("quality_audit", artifact=f"quality_result={quality_path}", item="W1")
        next_stage()
        record("code_review", item="W1")
        next_stage()

        cleanup_path = self.write_json(
            f"docs/loopx/runs/{self.run_id}/artifacts/test-cleanup.json",
            {"cleanup_verified": True},
        )
        record("test_execution", item="W1", stage_evidence=cleanup_path)
        next_stage()

        health_output = self.controller_command("health", self.run_id)
        self.assertIn("健康检查结果：PASS", health_output)
        health_result = f"docs/loopx/runs/{self.run_id}/artifacts/health-result.json"
        record("health_gate", stage_evidence=health_result)
        next_stage()
        record("release_readiness")
        next_stage()

        self.controller_command("git-gate", self.run_id)
        self.controller_command(
            "compound", self.run_id, "--decision", "skipped",
            "--reason", "本次变更为控制器契约验证，没有新增可复用项目经验。",
        )
        compound_artifact = f"docs/loopx/runs/{self.run_id}/artifacts/compound-capture.md"
        record("final_report", stage_evidence=compound_artifact)

        self.controller_command("validate", self.run_id, "--strict")
        self.controller_command("close", self.run_id)
        state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "PASS")
        self.assertEqual(state["current_stage"], "final_report")
        self.assertEqual(set(state["stages"]), set(self.controller.STAGE_SEQUENCE))
        self.assertTrue(all(status == "PASS" for status in state["stages"].values()))
        self.assertTrue((self.run_dir / "artifacts" / "close-evidence.json").is_file())


class WorklistTest(V2Fixture):
    def test_solution_work_items_sync(self):
        from loopx_controller_evidence import runtime_work_items

        items = self.solution_artifact()["work_items"]
        items.append({
            "id": "W2",
            "title": "验证控制器",
            "risk_tags": [],
            "owner_agent": "test-runner",
            "read_scope": ["tests"],
            "write_scope": ["tests"],
            "dependencies": ["W1"],
            "validation": ["python3 -m unittest"],
        })
        result = runtime_work_items(items)
        self.assertEqual(result[0]["status"], "pending")
        self.assertEqual(result[1]["dependencies"], ["W1"])
        duplicate = json.loads(json.dumps(items))
        duplicate[1]["id"] = "W1"
        with self.assertRaises(ValueError):
            runtime_work_items(duplicate)
        cycle = json.loads(json.dumps(items))
        cycle[0]["dependencies"] = ["W2"]
        with self.assertRaises(ValueError):
            runtime_work_items(cycle)

    def test_runtime_work_items_preserve_state_and_retain_omitted_history(self):
        from loopx_controller_evidence_workitems import runtime_work_items

        existing = runtime_work_items(self.solution_artifact()["work_items"])
        existing[0].update({
            "status": "PASS",
            "evidence": ["docs/evidence.log"],
            "failed_by": "code_review",
            "return_to": "development",
            "required_changes": ["补回归测试"],
        })
        proposed = [{
            "id": "W2",
            "title": "新增工作项",
            "risk_tags": [],
            "owner_agent": "development",
            "read_scope": ["loopx/tools"],
            "write_scope": ["loopx/tools"],
            "dependencies": [],
            "validation": ["python3 -m unittest"],
        }]

        result = runtime_work_items(proposed, existing_items=existing)

        by_id = {item["id"]: item for item in result}
        self.assertEqual(by_id["W1"]["status"], "PASS")
        self.assertEqual(by_id["W1"]["lineage"]["state"], "SUPERSEDED")
        self.assertEqual(by_id["W2"]["status"], "pending")
        self.assertEqual(by_id["W2"]["lineage"]["state"], "ACTIVE")

    def test_work_item_definition_change_resets_old_pass_and_evidence(self):
        from loopx_controller_evidence_workitems import runtime_work_items

        original = self.solution_artifact()["work_items"]
        existing = runtime_work_items(original)
        existing[0]["status"] = "PASS"
        existing[0]["evidence"] = ["old-test.log"]
        changed = json.loads(json.dumps(original))
        changed[0]["write_scope"] = ["loopx/new-scope"]
        changed[0]["validation"] = ["python3 -m unittest tests.test_new_scope"]

        result = runtime_work_items(changed, existing_items=existing)

        self.assertEqual(result[0]["status"], "pending")
        self.assertEqual(result[0]["evidence"], [])

    def test_superseded_work_item_reactivation_resets_old_pass_and_evidence(self):
        from loopx_controller_evidence_workitems import runtime_work_items

        original = self.solution_artifact()["work_items"]
        first = runtime_work_items(original)
        first[0]["status"] = "PASS"
        first[0]["evidence"] = ["old-test.log"]
        replacement = [{
            **original[0],
            "id": "W2",
            "title": "替代工作项",
        }]
        second = runtime_work_items(replacement, existing_items=first)
        self.assertEqual(
            next(item for item in second if item["id"] == "W1")["lineage"]["state"],
            "SUPERSEDED",
        )

        third = runtime_work_items(original, existing_items=second)
        reactivated = next(item for item in third if item["id"] == "W1")

        self.assertEqual(reactivated["lineage"]["state"], "ACTIVE")
        self.assertEqual(reactivated["status"], "pending")
        self.assertEqual(reactivated["evidence"], [])

    def test_blocked_stage_syncs_state_worklist_and_next_action(self):
        self.init()
        evidence = self.evidence_path()

        self.controller_command(
            "record-stage", "--run-id", self.run_id,
            "--stage", "requirement_intake", "--status", "BLOCKED",
            "--evidence", evidence, "--blocked-reason", "等待用户补充需求边界",
        )

        state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        from loopx_controller_yaml import parse_yaml_subset

        worklist = parse_yaml_subset((self.run_dir / "worklist.yml").read_text(encoding="utf-8"))
        result = json.loads(
            (self.run_dir / "stage-results" / "01-requirement-intake.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "BLOCKED")
        self.assertEqual(state["current_stage"], "requirement_intake")
        self.assertEqual(state["next_action"], "await_user:requirement_intake")
        self.assertEqual(worklist["run"]["status"], "BLOCKED")
        self.assertEqual(worklist["run"]["next_action"], "await_user:requirement_intake")
        self.assertEqual(result["next_action"], "await_user:requirement_intake")
        self.assertIn("duration_ms", result["timing"])
        self.assertGreaterEqual(result["timing"]["attempt"], 1)

        self.controller_command(
            "record-stage", "--run-id", self.run_id,
            "--stage", "requirement_intake", "--status", "PASS",
            "--evidence", evidence,
        )
        retried = json.loads(
            (self.run_dir / "stage-results" / "01-requirement-intake.json").read_text(encoding="utf-8")
        )
        self.assertEqual(retried["timing"]["attempt"], result["timing"]["attempt"] + 1)

    def test_auto_until_blocked_waives_confirmation_without_skipping_stage(self):
        self.init(automation_policy="auto_until_blocked")
        evidence = self.write_text(
            f"docs/loopx/runs/{self.run_id}/artifacts/interview.md",
            "# 需求采访\n\n所有问题均已回答并确认。\n",
        )
        self.force_current_stage("requirement_interview")

        self.controller_command(
            "record-stage", "--run-id", self.run_id,
            "--stage", "requirement_interview", "--status", "PASS",
            "--evidence", evidence,
        )

        state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        result = json.loads(
            (self.run_dir / "stage-results" / "02-requirement-interview.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["stages"]["requirement_interview"], "PASS")
        self.assertFalse(result["user_confirmation_required"])
        self.assertTrue(result["confirmation_waived_by_init_authorization"])
        self.assertEqual(result["next_action"], "spec_draft")

    def test_all_commands_validate_item_reference(self):
        self.init()
        evidence = self.evidence_path()
        before = self.snapshot_files()
        out = io.StringIO()
        code = self.controller.main([
            "record-stage", "--run-id", self.run_id, "--stage", "requirement_intake",
            "--status", "BLOCKED", "--evidence", evidence, "--item", "UNKNOWN",
            "--project", str(self.root),
        ], stdout=out)
        self.assertEqual(code, 1)
        self.assertIn("工作项引用不存在", out.getvalue())
        self.assertEqual(before, self.snapshot_files())
        for command in (
            ["fail-review", "--from", "solution_review", "--return-to", "solution_design", "--item", "UNKNOWN", "--reason", "缺陷"],
            ["review-feedback", "--return-to", "solution_design", "--item", "UNKNOWN", "--reason", "缺陷"],
            ["close-repair", "--item", "UNKNOWN", "--artifact", evidence, "--revision", "2", "--change", "修正"],
        ):
            if command[0] == "fail-review":
                self.force_current_stage("solution_review")
            out = io.StringIO()
            args = [*command, "--run-id", self.run_id, "--project", str(self.root)]
            self.assertEqual(self.controller.main(args, stdout=out), 1)
            self.assertIn("工作项引用不存在", out.getvalue())

    def test_v2_repair_commands_do_not_partially_write_before_atomic_commit(self):
        self.init()
        self.force_current_stage("solution_design")
        evidence = self.evidence_path()
        solution_path = self.write_solution(self.solution_artifact(evidence=evidence))
        self.assertEqual(self.record_solution(solution_path)[0], 0)
        self.force_current_stage("code_review")

        def snapshot():
            return {
                path.relative_to(self.run_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.run_dir.rglob("*")
                if path.is_file() and not path.name.startswith(".loopx-transaction-")
            }

        before_failure = snapshot()
        with mock.patch(
            "loopx_controller_flow.atomic_write_texts",
            side_effect=KeyboardInterrupt("模拟返工提交中断"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.controller.main([
                    "fail-review", "--run-id", self.run_id, "--from", "code_review",
                    "--return-to", "development", "--item", "W1", "--reason", "需要修正",
                    "--project", str(self.root),
                ], stdout=io.StringIO())
        self.assertEqual(before_failure, snapshot())

        self.controller_command(
            "fail-review", "--run-id", self.run_id, "--from", "code_review",
            "--return-to", "development", "--item", "W1", "--reason", "需要修正",
        )
        before_close = snapshot()
        with mock.patch(
            "loopx_controller_repair.atomic_write_texts",
            side_effect=KeyboardInterrupt("模拟关闭返工单中断"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.controller.main([
                    "close-repair", "--run-id", self.run_id, "--item", "W1",
                    "--artifact", evidence, "--revision", "2", "--change", "已修正",
                    "--project", str(self.root),
                ], stdout=io.StringIO())
        self.assertEqual(before_close, snapshot())

    def test_development_pass_resolves_affected_work_item(self):
        self.init()
        self.force_current_stage("solution_design")
        evidence = self.evidence_path()
        solution_path = self.write_solution(self.solution_artifact(evidence=evidence))
        self.assertEqual(self.record_solution(solution_path)[0], 0)
        self.force_current_stage("code_review")
        self.controller_command(
            "fail-review", "--run-id", self.run_id, "--from", "code_review",
            "--return-to", "development", "--item", "W1", "--reason", "需要补充实现证据",
        )
        document = self.document_path("development.md")
        artifact = self.development_artifact(evidence, document)
        artifact_path = self.write_json(
            f"docs/loopx/runs/{self.run_id}/artifacts/development-evidence.json",
            artifact,
        )
        self.controller_command(
            "record-stage", "--run-id", self.run_id, "--stage", "development",
            "--status", "PASS", "--artifact", f"development_evidence={artifact_path}",
            "--item", "W1",
        )
        from loopx_controller_yaml import parse_yaml_subset

        worklist = parse_yaml_subset((self.run_dir / "worklist.yml").read_text(encoding="utf-8"))
        item = worklist["items"][0]
        self.assertEqual(item["status"], "PASS")
        self.assertTrue(item["evidence"])
        self.assertEqual(item["failed_by"], "")
        self.assertEqual(item["return_to"], "")
        self.assertEqual(item["required_changes"], [])

    def test_solution_rerecord_cannot_drop_item_with_open_repair(self):
        self.init()
        self.force_current_stage("solution_design")
        evidence = self.evidence_path()
        original = self.solution_artifact(evidence=evidence)
        original_path = self.write_solution(original)
        self.assertEqual(self.record_solution(original_path)[0], 0)
        self.force_current_stage("code_review")
        self.controller_command(
            "fail-review", "--run-id", self.run_id, "--from", "code_review",
            "--return-to", "development", "--item", "W1", "--reason", "需要修正实现",
        )
        replacement = self.solution_artifact(evidence=evidence, work_items=[{
            "id": "W2", "title": "替代工作项", "risk_tags": [], "owner_agent": "development",
            "read_scope": ["loopx/tools"], "write_scope": ["loopx/tools"],
            "dependencies": [], "validation": ["python3 -m unittest"],
        }])
        replacement_path = self.write_solution(replacement, "replacement-solution.json")

        self.force_current_stage("solution_design")
        code, output = self.record_solution(replacement_path)

        self.assertEqual(code, 1)
        self.assertIn("不能删除仍有关联开放返工单的工作项：W1", output)

    def test_identical_stage_retry_restores_state_after_review_return(self):
        self.init(mode="FULL", risk_tags=["core_state_transition"])
        self.force_current_stage("solution_design")
        evidence = self.evidence_path()
        artifact = self.solution_artifact(evidence=evidence)
        artifact["rule_results"] = [
            self.rule_result(rule_id, evidence)
            for rule_id in ("ARCH-BOUNDARY-002", "ARCH-COMPAT-003", "REL-RECOVERY-001", "ARCH-SIMPLE-001")
        ]
        solution_path = self.write_solution(artifact)
        self.assertEqual(self.record_solution(solution_path)[0], 0)
        # 审核返回会保留旧阶段结果文件并清除需要重做阶段的状态；这里直接构造该持久化边界。
        state_path = self.run_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["stages"].pop("solution_design")
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

        code, output = self.record_solution(solution_path)

        self.assertEqual(code, 0, output)
        state = json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stages"]["solution_design"], "PASS")


class LoopxLegacyCompatibilityTest(V2Fixture):
    def test_v1_end_to_end(self):
        self.init()
        state_path = self.run_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        for key in ("contract_version", "catalog_version", "policy_snapshot", "policy_snapshot_sha256"):
            state.pop(key, None)
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        out = io.StringIO()
        self.assertEqual(self.controller.main([
            "record-stage", "--run-id", self.run_id, "--stage", "requirement_intake",
            "--status", "PASS", "--evidence", "legacy free-form evidence",
            "--project", str(self.root),
        ], stdout=out), 0, out.getvalue())
        result = json.loads((self.run_dir / "stage-results" / "01-requirement-intake.json").read_text(encoding="utf-8"))
        self.assertNotIn("contract_version", result)
        self.assertEqual(result["evidence"], ["legacy free-form evidence"])


if __name__ == "__main__":
    unittest.main()
