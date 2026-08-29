import importlib.util
import hashlib
import io
import json
import os
import shutil
import stat
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
    spec = importlib.util.spec_from_file_location("loopx_controller_store_test", CONTROLLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExternalStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="loopx-store-test-"))
        self.project = self.temp / "project"
        self.state_root = self.temp / "state"
        self.project.mkdir()
        self.run_id = f"store-{uuid.uuid4().hex[:10]}"
        self.environment = mock.patch.dict(
            os.environ,
            {"LOOPX_STATE_DIR": str(self.state_root), "LOOPX_STATE_BACKEND": ""},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.controller = load_controller()
        import loopx_controller_store

        self.store = loopx_controller_store

    def tearDown(self):
        shutil.rmtree(self.temp)
        self.assertFalse(self.temp.exists())

    def call(self, *args):
        output = io.StringIO()
        code = self.controller.main([*args, "--project", str(self.project)], stdout=output)
        return code, output.getvalue()

    def init(self):
        code, output = self.call(
            "init",
            "验证用户级单文件状态",
            "--run-id",
            self.run_id,
            "--mode",
            "LIGHT",
        )
        self.assertEqual(code, 0, output)
        return output

    @property
    def bundle(self):
        return self.store.bundle_path(self.project, self.run_id)

    def read_container(self):
        return json.loads(self.bundle.read_text(encoding="utf-8"))

    def create_owned_lock(self, pid, token):
        lock = self.bundle.with_name("run.lock")
        owner = self.bundle.with_name(f".run.{token}.owner")
        payload = json.dumps({"pid": pid, "token": token})
        owner.write_text(payload, encoding="utf-8")
        os.link(owner, lock)
        return lock, owner

    def read_run_json(self, relative):
        with self.store.ExternalRunSession(self.project, self.run_id) as session:
            return json.loads((session.directory / relative).read_text(encoding="utf-8"))

    def force_current_stage(self, stage):
        with self.store.ExternalRunSession(self.project, self.run_id) as session:
            state_path = session.directory / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["current_stage"] = stage
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            session.commit()

    def write_project_text(self, relative, content):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return relative

    def write_temp_json(self, name, value):
        path = self.temp / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def rule_result(rule_id, evidence):
        return {"rule_id": rule_id, "status": "PASS", "evidence": [evidence], "reason": ""}

    def quality_attributes(self, evidence):
        return {
            name: {"status": "APPLICABLE", "approach": f"已验证 {name}", "reason": "", "evidence": [evidence]}
            for name in (
                "simplicity", "module_boundaries", "security", "performance",
                "extensibility", "compatibility", "reliability", "observability",
            )
        }

    def build_solution(self, stage, evidence, document):
        artifact = {
            "artifact_type": "solution", "artifact_version": "1", "run_id": self.run_id,
            "stage": stage, "document": document, "requirement_ids": ["AC-001"],
            "rule_results": [
                self.rule_result(rule_id, evidence)
                for rule_id in ("ARCH-BOUNDARY-002", "ARCH-COMPAT-003", "REL-RECOVERY-001", "ARCH-SIMPLE-001")
            ],
            "decisions": [{
                "id": "D1", "requirement_ids": ["AC-001"], "summary": "使用单文件状态容器",
                "rationale": "保持现有流程契约", "alternatives": ["项目目录存储"],
            }],
            "impact": {
                "modules": ["loopx/tools"], "contracts": ["CLI 不变"], "data": ["run.json"],
                "configuration": ["LOOPX_STATE_DIR"], "dependencies": [],
            },
            "quality_attributes": self.quality_attributes(evidence),
            "performance_targets": [],
            "rollback": {
                "strategy": "使用项目后端", "steps": ["设置环境变量"],
                "validation": ["执行旧回归"], "reason": "",
            },
            "verification_refs": [evidence],
            "work_items": [{
                "id": "W1", "title": "验证外部状态完整流程", "risk_tags": ["core_state_transition"],
                "owner_agent": "development", "read_scope": ["loopx/tools"],
                "write_scope": ["loopx/tools"], "dependencies": [],
                "validation": ["python3 -m unittest tests.test_loopx_store"],
            }],
            "extensions": self.requirement_extensions(),
        }
        if stage == "solution_review":
            artifact["rule_results"].append(self.rule_result("ARCH-REVIEW-COVERAGE-004", evidence))
            verdict = {"status": "PASS", "evidence": [evidence], "reason": ""}
            artifact["extensions"]["review_assurance"] = {
                "reviewed_snapshot_id": self.recorded_solution_digest(),
                "review_kind": "FULL",
                "baseline_snapshot_id": "",
                "review_scope": ["FULL 流程方案"],
                "checked_dimensions": {
                    name: dict(verdict) for name in (
                        "requirement_coverage", "minimal_modification", "existing_behavior_impact",
                        "interface_contract", "verification_deployment",
                    )
                },
                "blocking_findings": [],
                "unknowns": [],
                "completeness_attestation": "已检查全部适用维度。",
            }
        return artifact

    def recorded_solution_digest(self):
        with self.store.ExternalRunSession(self.project, self.run_id) as session:
            result = json.loads(
                (session.directory / "stage-results" / "06-solution-design.json").read_text(encoding="utf-8")
            )
            relative = next(item["path"] for item in result["artifacts"] if item["type"] == "solution")
            prefix = f"docs/loopx/runs/{self.run_id}/"
            self.assertTrue(relative.startswith(prefix))
            content = (session.directory / relative[len(prefix):]).read_bytes()
        return hashlib.sha256(content).hexdigest()

    def requirement_extensions(self):
        digest = getattr(self, "requirement_manifest_sha256", "")
        return {"requirement_manifest_sha256": digest} if digest else {}

    def build_test_plan(self, stage, evidence, document, rule_ids):
        return {
            "artifact_type": "test_plan", "artifact_version": "1", "run_id": self.run_id,
            "stage": stage, "document": document, "requirement_ids": ["AC-001"],
            "rule_results": [
                self.rule_result("TEST-MAPPING-001", evidence),
                self.rule_result("TEST-CLEANUP-002", evidence),
            ],
            "mappings": [{
                "requirement_id": "AC-001", "acceptance_ids": ["AC-001"],
                "rule_ids": rule_ids, "test_case_ids": ["TC-001"],
            }],
            "cases": [{
                "id": "TC-001", "covers": ["AC-001"], "risk_tags": ["core_state_transition"],
                "preconditions": ["FULL 运行已初始化"],
                "data_setup": {"run_id_strategy": "uuid", "records": ["临时项目和状态根"]},
                "execution": {"entrypoint": "loopx_controller.py", "steps": ["执行完整流程"]},
                "assertions": ["17 阶段通过", "项目无控制 JSON"],
                "cleanup": {"steps": ["删除临时目录"], "verification": ["测试前缀不存在"]},
                "expected_result": "PASS",
            }],
            "environment": {"local": ["Python 3"], "ci_required": [], "manual": ["用户确认"]},
            "extensions": self.requirement_extensions(),
        }

    def build_development(self, evidence, document):
        return {
            "artifact_type": "development_evidence", "artifact_version": "1", "run_id": self.run_id,
            "stage": "development", "document": document, "requirement_ids": ["AC-001"],
            "rule_results": [self.rule_result("COMMON-EVIDENCE-001", evidence)],
            "changed_files": ["loopx/tools/loopx_controller_store.py"], "write_scope": ["loopx/tools"],
            "dependency_changes": [],
            "acceptance_mapping": [{
                "requirement_id": "AC-001", "files": ["loopx/tools/loopx_controller_store.py"],
                "tests": ["tests.test_loopx_store"],
            }],
            "commands": [{
                "argv": ["python3", "-m", "unittest"], "status": "PASS", "exit_code": 0,
                "evidence": [evidence], "ci_required": False,
            }],
            "residual_risks": [],
            "extensions": self.requirement_extensions(),
        }

    def build_quality(self, evidence, document):
        return {
            "artifact_type": "quality_result", "artifact_version": "1", "run_id": self.run_id,
            "stage": "quality_audit", "document": document, "requirement_ids": ["AC-001"],
            "rule_results": [self.rule_result("COMMON-EVIDENCE-001", evidence)],
            "unresolved_items": [], "ci_gaps": [], "accepted_risks": [],
            "diff_scope": {
                "allowed": ["loopx/tools"], "actual": ["loopx/tools/loopx_controller_store.py"], "outside": [],
            },
            "extensions": self.requirement_extensions(),
        }

    def test_init_only_writes_one_user_state_file(self):
        output = self.init()
        self.assertIn(f"PASS 已创建运行：{self.run_id}", output)
        self.assertTrue(self.bundle.is_file())
        self.assertEqual([path.name for path in self.bundle.parent.iterdir()], ["run.json"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.bundle.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.bundle.parent.stat().st_mode), 0o700)
        self.assertFalse((self.project / "docs" / "loopx" / "runs" / self.run_id).exists())
        self.assertEqual(list(self.project.rglob("*.json")), [])
        container = self.read_container()
        self.assertEqual(container["storage_version"], "1")
        self.assertIn("state.json", container["files"])
        self.assertIn("stage-results/00-environment-check.json", container["files"])

    def test_commands_round_trip_without_rewriting_read_only_state(self):
        self.init()
        before = self.bundle.read_bytes()
        code, output = self.call("status", self.run_id)
        self.assertEqual(code, 0, output)
        self.assertIn("当前阶段：requirement_intake", output)
        self.assertEqual(self.bundle.read_bytes(), before)
        code, output = self.call("interview", self.run_id)
        self.assertEqual(code, 0, output)
        container = self.read_container()
        self.assertIn("artifacts/interview.md", container["files"])
        self.assertFalse((self.project / "docs" / "loopx" / "runs").exists())

    def test_latest_external_run_is_selected_without_run_id(self):
        self.init()
        code, output = self.call("status")
        self.assertEqual(code, 0, output)
        self.assertIn(f"运行 ID：{self.run_id}", output)

    def test_invalid_run_id_returns_chinese_error_without_traceback(self):
        code, output = self.call("status", "../invalid-run")
        self.assertEqual(code, 1)
        self.assertIn("运行 ID 不能包含路径片段", output)
        self.assertNotIn("Traceback", output)
        code, output = self.call("status", "..\\invalid-run")
        self.assertEqual(code, 1)
        self.assertIn("运行 ID 不能包含路径片段", output)

    def test_invalid_run_id_cannot_escape_through_existing_legacy_path(self):
        (self.project / "docs" / "loopx" / "runs").mkdir(parents=True)
        victim = self.temp / "victim"
        victim.mkdir()
        source = self.temp / "escaped.txt"
        source.write_text("不得写出项目", encoding="utf-8")
        code, output = self.call(
            "import-artifact", "../../../../victim",
            "--source", str(source), "--target", "artifacts/escaped.txt",
        )
        self.assertEqual(code, 1)
        self.assertIn("运行 ID 不能包含路径片段", output)
        self.assertNotIn("Traceback", output)
        self.assertFalse((victim / "artifacts" / "escaped.txt").exists())

    def test_tampered_container_is_rejected(self):
        self.init()
        container = self.read_container()
        container["files"]["state.json"]["content"] += " "
        self.bundle.write_text(json.dumps(container, ensure_ascii=False), encoding="utf-8")
        code, output = self.call("status", self.run_id)
        self.assertEqual(code, 1)
        self.assertIn("运行容器摘要校验失败", output)
        self.assertFalse(self.bundle.with_name("run.lock").exists())

    def test_file_directory_collision_returns_error_without_traceback(self):
        self.init()
        container = self.read_container()
        container["directories"].append("state.json")
        payload = {key: value for key, value in container.items() if key != "digest"}
        container["digest"] = self.store._canonical_digest(payload)
        self.bundle.write_text(json.dumps(container, ensure_ascii=False), encoding="utf-8")
        code, output = self.call("status", self.run_id)
        self.assertEqual(code, 1)
        self.assertIn("无法还原运行容器", output)
        self.assertNotIn("Traceback", output)
        self.assertEqual([path.name for path in self.bundle.parent.iterdir()], ["run.json"])

    def test_commit_failure_keeps_original_and_hides_buffered_success(self):
        self.init()
        before = self.bundle.read_bytes()
        with mock.patch.object(self.store.os, "replace", side_effect=OSError("disk full")):
            code, output = self.call("interview", self.run_id)
        self.assertEqual(code, 1)
        self.assertIn("运行容器提交失败", output)
        self.assertNotIn("已生成需求采访", output)
        self.assertEqual(self.bundle.read_bytes(), before)
        self.assertEqual([path.name for path in self.bundle.parent.iterdir()], ["run.json"])

    def test_temporary_cleanup_failure_still_releases_lock(self):
        self.init()
        session = self.store.ExternalRunSession(self.project, self.run_id)
        session.__enter__()
        temporary = session.temp
        with mock.patch.object(temporary, "cleanup", side_effect=OSError("cleanup failed")):
            with self.assertRaises(OSError):
                session._cleanup()
        self.assertFalse(session.lock.exists())
        self.assertFalse(session.owner.exists())
        temporary.cleanup()

    def test_cli_temporary_cleanup_failure_has_no_traceback(self):
        self.init()
        real_temporary_directory = tempfile.TemporaryDirectory
        created = []

        class FailingTemporaryDirectory:
            def __init__(inner_self, *args, **kwargs):
                inner_self.delegate = real_temporary_directory(*args, **kwargs)
                inner_self.name = inner_self.delegate.name
                created.append(inner_self.delegate)

            def cleanup(inner_self):
                raise OSError("cleanup failed")

        with mock.patch.object(self.store.tempfile, "TemporaryDirectory", FailingTemporaryDirectory):
            code, output = self.call("status", self.run_id)
        for directory in created:
            directory.cleanup()
        self.assertEqual(code, 1)
        self.assertIn("状态存储错误：cleanup failed", output)
        self.assertNotIn("Traceback", output)
        self.assertEqual([path.name for path in self.bundle.parent.iterdir()], ["run.json"])

    def test_live_lock_blocks_and_stale_lock_recovers(self):
        self.init()
        live_token = uuid.uuid4().hex
        lock, owner = self.create_owned_lock(os.getpid(), live_token)
        code, output = self.call("status", self.run_id)
        self.assertEqual(code, 1)
        self.assertIn("正在被进程", output)
        self.assertEqual(
            sorted(path.name for path in self.bundle.parent.iterdir()),
            [f".run.{live_token}.owner", "run.json", "run.lock"],
        )
        lock.unlink()
        owner.unlink()
        lock, _ = self.create_owned_lock(2_147_483_647, uuid.uuid4().hex)
        code, output = self.call("status", self.run_id)
        self.assertEqual(code, 0, output)
        self.assertFalse(lock.exists())
        self.assertEqual([path.name for path in self.bundle.parent.iterdir()], ["run.json"])

    def test_project_backend_uses_same_per_run_lock(self):
        first = self.store.ProjectRunSession(self.project, self.run_id, create=True)
        second = self.store.ProjectRunSession(self.project, self.run_id, create=True)
        first.__enter__()
        try:
            with self.assertRaisesRegex(self.store.StoreError, "正在被进程"):
                second.__enter__()
        finally:
            first.__exit__(None, None, None)
        self.assertFalse(first.lock.exists())

    def test_stale_lock_without_matching_owner_is_not_deleted(self):
        self.init()
        lock = self.bundle.with_name("run.lock")
        stale_token = uuid.uuid4().hex
        lock.write_text(json.dumps({"pid": 2_147_483_647, "token": stale_token}), encoding="utf-8")
        code, output = self.call("status", self.run_id)
        self.assertEqual(code, 1)
        self.assertIn("无法获取运行锁", output)
        self.assertEqual(json.loads(lock.read_text(encoding="utf-8"))["token"], stale_token)
        self.assertFalse(any(path.name.endswith(".owner") for path in self.bundle.parent.iterdir()))

    def test_malformed_lock_returns_error_without_orphan_owner(self):
        self.init()
        lock = self.bundle.with_name("run.lock")
        lock.write_text("{", encoding="utf-8")
        code, output = self.call("status", self.run_id)
        self.assertEqual(code, 1)
        self.assertIn("缺少 token", output)
        self.assertNotIn("Traceback", output)
        self.assertTrue(lock.exists())
        self.assertFalse(any(path.name.endswith(".owner") for path in self.bundle.parent.iterdir()))

    def test_lock_token_with_path_separator_returns_error_without_escape(self):
        self.init()
        lock = self.bundle.with_name("run.lock")
        lock.write_text(
            json.dumps({"pid": 2_147_483_647, "token": "../escape"}),
            encoding="utf-8",
        )
        code, output = self.call("status", self.run_id)
        self.assertEqual(code, 1)
        self.assertIn("token 非法", output)
        self.assertNotIn("Traceback", output)
        self.assertTrue(lock.exists())
        self.assertFalse((self.bundle.parent.parent / "escape.owner").exists())
        self.assertFalse(any(path.name.endswith(".owner") for path in self.bundle.parent.iterdir()))

    def test_recovery_continues_after_previous_reclaimer_crash(self):
        self.init()
        stale_token = uuid.uuid4().hex
        lock, owner = self.create_owned_lock(2_147_483_647, stale_token)
        crashed_claim = self.bundle.with_name(f".run.{stale_token}.crashed.reclaim")
        owner.rename(crashed_claim)
        self.assertTrue(os.path.samestat(lock.stat(), crashed_claim.stat()))
        code, output = self.call("status", self.run_id)
        self.assertEqual(code, 0, output)
        self.assertFalse(lock.exists())
        self.assertEqual([path.name for path in self.bundle.parent.iterdir()], ["run.json"])

    def test_binary_file_round_trip(self):
        self.init()
        payload = b"\x00\xff\x10LoopX"
        with self.store.ExternalRunSession(self.project, self.run_id) as session:
            target = session.directory / "artifacts" / "binary.dat"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            session.commit()
        self.assertEqual(self.read_container()["files"]["artifacts/binary.dat"]["encoding"], "base64")
        with self.store.ExternalRunSession(self.project, self.run_id) as session:
            self.assertEqual((session.directory / "artifacts" / "binary.dat").read_bytes(), payload)

    def test_explicit_artifact_import_is_stored_inside_container(self):
        self.init()
        source = self.temp / "solution.json"
        source.write_text('{"artifact_type":"solution"}', encoding="utf-8")
        with self.store.ExternalRunSession(self.project, self.run_id) as session:
            values = self.controller.import_artifact_files(
                self.project,
                self.run_id,
                "solution_design",
                [f"solution={source}"],
            )
            self.assertEqual(
                values,
                [f"solution=docs/loopx/runs/{self.run_id}/artifacts/imported/solution_design-solution.json"],
            )
            session.commit()
        self.assertIn("artifacts/imported/solution_design-solution.json", self.read_container()["files"])
        self.assertFalse((self.project / "docs" / "loopx" / "runs").exists())

    def test_import_artifact_command_replaces_editable_run_artifact(self):
        self.init()
        source = self.temp / "answered-interview.md"
        source.write_text("已回答的需求采访\n", encoding="utf-8")
        code, output = self.call(
            "import-artifact",
            self.run_id,
            "--source",
            str(source),
            "--target",
            "artifacts/interview.md",
        )
        self.assertEqual(code, 0, output)
        entry = self.read_container()["files"]["artifacts/interview.md"]
        self.assertEqual(entry, {"encoding": "utf-8", "content": "已回答的需求采访\n"})
        self.assertFalse((self.project / "docs" / "loopx" / "runs").exists())

    def test_failed_stage_record_does_not_keep_imported_artifact(self):
        self.init()
        self.force_current_stage("solution_design")
        source = self.temp / "invalid-solution.json"
        source.write_text('{"artifact_type":"solution"}\n', encoding="utf-8")
        code, output = self.call(
            "record-stage",
            "--run-id", self.run_id,
            "--stage", "solution_design",
            "--status", "PASS",
            "--artifact-file", f"solution={source}",
        )
        self.assertEqual(code, 1)
        self.assertIn("结构校验失败", output)
        self.assertNotIn(
            "artifacts/imported/solution_design-solution.json",
            self.read_container()["files"],
        )

    def test_legacy_multiple_artifact_import_failure_restores_first_file(self):
        with mock.patch.dict(os.environ, {"LOOPX_STATE_BACKEND": "project"}):
            output = io.StringIO()
            code = self.controller.main([
                "init", "验证旧目录导入原子性", "--run-id", self.run_id,
                "--mode", "LIGHT", "--project", str(self.project),
            ], stdout=output)
        self.assertEqual(code, 0, output.getvalue())

        source = self.temp / "solution.json"
        source.write_text('{"artifact_type":"solution"}\n', encoding="utf-8")
        missing = self.temp / "missing-test-plan.json"
        code, output = self.call(
            "record-stage", "--run-id", self.run_id,
            "--stage", "solution_design", "--status", "PASS",
            "--artifact-file", f"solution={source}",
            "--artifact-file", f"test_plan={missing}",
        )
        self.assertEqual(code, 1)
        self.assertIn("无法导入结构化产物", output)
        imported = (
            self.project / "docs" / "loopx" / "runs" / self.run_id
            / "artifacts" / "imported" / "solution_design-solution.json"
        )
        self.assertFalse(imported.exists())

    def test_existing_project_run_keeps_legacy_backend(self):
        with mock.patch.dict(os.environ, {"LOOPX_STATE_BACKEND": "project"}):
            output = io.StringIO()
            code = self.controller.main([
                "init", "验证旧格式", "--run-id", self.run_id, "--mode", "LIGHT",
                "--project", str(self.project),
            ], stdout=output)
        self.assertEqual(code, 0, output.getvalue())
        legacy = self.project / "docs" / "loopx" / "runs" / self.run_id
        self.assertTrue((legacy / "state.json").is_file())
        code, output = self.call("status", self.run_id)
        self.assertEqual(code, 0, output)
        self.assertFalse(self.bundle.exists())

    def test_existing_external_run_remains_readable_with_project_override(self):
        self.init()
        with mock.patch.dict(os.environ, {"LOOPX_STATE_BACKEND": "project"}):
            code, output = self.call("status", self.run_id)
        self.assertEqual(code, 0, output)
        self.assertIn(f"运行 ID：{self.run_id}", output)
        self.assertFalse((self.project / "docs" / "loopx" / "runs").exists())

    def test_project_harness_reads_external_container(self):
        self.init()
        import loopx_check

        report = loopx_check.evaluate_project(self.project)
        run_check = next(check for check in report.checks if check.name == "loopx_run_state")
        self.assertEqual(run_check.status, "PASS")
        self.assertIn(self.run_id, run_check.message)
        self.assertEqual([path.name for path in self.bundle.parent.iterdir()], ["run.json"])

    def test_validate_learning_reads_external_compound_artifact(self):
        self.init()
        code, output = self.call(
            "compound", self.run_id, "--decision", "skipped",
            "--reason", "当前测试只验证逻辑运行产物读取。",
        )
        self.assertEqual(code, 0, output)
        logical = f"docs/loopx/runs/{self.run_id}/artifacts/compound-capture.md"
        code, output = self.call("validate-learning", logical)
        self.assertEqual(code, 0, output)
        self.assertIn("PASS 经验文档检查通过", output)
        self.assertFalse((self.project / logical).exists())

    def test_close_archive_failure_is_reported_without_raising(self):
        import loopx_controller_release

        directory = self.temp / "legacy-run"
        (directory / "artifacts" / "repair-tickets").mkdir(parents=True)
        (directory / "events.jsonl").write_text("{}\n", encoding="utf-8")
        with mock.patch.object(Path, "rename", side_effect=OSError("disk full")):
            warnings = loopx_controller_release.archive_intermediate_state(directory)
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all("无法归档" in warning for warning in warnings))

    def test_full_external_store_workflow_closes_without_project_control_json(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True, capture_output=True, text=True)
        self.write_project_text(".github/workflows/ci.yml", "name: LoopX CI\n")
        evidence = self.write_project_text("docs/loopx/e2e/evidence.md", "# 验证证据\n\n本地检查通过。\n")
        solution_doc = self.write_project_text("docs/loopx/e2e/solution.md", "# 方案\n\n保持功能不变。\n")
        test_doc = self.write_project_text("docs/loopx/e2e/test-plan.md", "# 测试\n\n完整流程测试。\n")
        development_doc = self.write_project_text("docs/loopx/e2e/development.md", "# 开发\n\n实现与验证完成。\n")
        quality_doc = self.write_project_text("docs/loopx/e2e/quality.md", "# 质量\n\n未发现阻塞项。\n")

        code, output = self.call(
            "init", "验证外部单文件 FULL 流程", "--run-id", self.run_id,
            "--mode", "FULL", "--risk-tags", "core_state_transition",
        )
        self.assertEqual(code, 0, output)

        def command(*args, expected=0):
            code, output = self.call(*args)
            self.assertEqual(code, expected, output)
            self.assertFalse((self.project / "docs" / "loopx" / "runs").exists())
            return output

        def record(stage, *, artifact_file=None, item=None, stage_evidence=evidence):
            args = [
                "record-stage", "--run-id", self.run_id, "--stage", stage,
                "--status", "PASS", "--evidence", stage_evidence,
            ]
            if artifact_file:
                args.extend(["--artifact-file", artifact_file])
            if item:
                args.extend(["--item", item])
            command(*args)

        def next_stage():
            command("next", self.run_id)

        record("requirement_intake")
        next_stage()
        command("interview", self.run_id)
        answered_interview = self.temp / "interview.md"
        answered_interview.write_text("""# 需求采访

## 运行信息
已识别本次运行。

## 已确认事实
目标是减少项目内控制文件且功能不变。

## 问题与回答
所有问题均已明确回答。

## 未决问题
无。

## 结论
可以继续。
""", encoding="utf-8")
        command(
            "import-artifact", self.run_id, "--source", str(answered_interview),
            "--target", "artifacts/interview.md",
        )
        interview_logical = f"docs/loopx/runs/{self.run_id}/artifacts/interview.md"
        record("requirement_interview", stage_evidence=interview_logical)
        command(
            "confirm-stage", "--run-id", self.run_id, "--stage", "requirement_interview",
            "--evidence", evidence,
        )
        next_stage()

        command("spec", self.run_id)
        spec = self.temp / "spec.md"
        spec.write_text("""# 需求规格

## 摘要
验证外部单文件 FULL 流程。

## 期望行为
全部流程功能保持。

## 验收标准
AC-001：运行完成并收口。

## 范围内
控制器和本地状态存储。

## 范围外
远端发布。

## 边界情况
损坏和冲突必须失败。

## 测试策略
执行完整流程和异常测试。

## 执行等级决策
使用 FULL。
""", encoding="utf-8")
        command("import-artifact", self.run_id, "--source", str(spec), "--target", "artifacts/spec.md")
        manifest = self.write_temp_json("requirement-manifest.json", {
            "version": "1",
            "requirement_ids": ["AC-001"],
            "acceptance_ids": ["AC-001"],
            "delivery_units": [{
                "id": "DU-001", "source_refs": ["test"], "requirement_ids": ["AC-001"],
                "acceptance_ids": ["AC-001"], "modules": ["loopx/tools"], "deploy_targets": [],
                "depends_on": [], "independently_releasable": True,
            }],
            "deferred": [], "delivery_strategy": "SINGLE_RUN", "coupled_reason": "",
        })
        command(
            "import-artifact", self.run_id, "--source", str(manifest),
            "--target", "artifacts/requirement-manifest.json",
        )
        spec_logical = f"docs/loopx/runs/{self.run_id}/artifacts/spec.md"
        record("spec_draft", stage_evidence=spec_logical)
        next_stage()
        record("spec_review", stage_evidence=spec_logical)
        self.requirement_manifest_sha256 = self.read_run_json("state.json")["spec"]["extensions"][
            "requirement_manifest_sha256"
        ]
        next_stage()
        command("mode", self.run_id, "--select", "FULL")
        next_stage()

        solution = self.write_temp_json("solution.json", self.build_solution("solution_design", evidence, solution_doc))
        record("solution_design", artifact_file=f"solution={solution}", item="W1")
        next_stage()
        review_solution = self.write_temp_json(
            "solution-review.json", self.build_solution("solution_review", evidence, solution_doc),
        )
        record("solution_review", artifact_file=f"solution={review_solution}", item="W1")
        command(
            "confirm-stage", "--run-id", self.run_id, "--stage", "solution_review",
            "--evidence", solution_doc,
        )
        next_stage()

        snapshot = self.read_run_json("artifacts/policy-snapshot.json")
        required_rules = [rule["id"] for rule in snapshot["rules"] if rule["level"] == "required"]
        test_plan = self.write_temp_json(
            "test-plan.json", self.build_test_plan("test_design", evidence, test_doc, required_rules),
        )
        record("test_design", artifact_file=f"test_plan={test_plan}", item="W1")
        next_stage()
        test_review = self.write_temp_json(
            "test-plan-review.json", self.build_test_plan("test_review", evidence, test_doc, required_rules),
        )
        record("test_review", artifact_file=f"test_plan={test_review}", item="W1")
        next_stage()

        development = self.write_temp_json(
            "development.json", self.build_development(evidence, development_doc),
        )
        record("development", artifact_file=f"development_evidence={development}", item="W1")
        next_stage()
        quality = self.write_temp_json("quality.json", self.build_quality(evidence, quality_doc))
        record("quality_audit", artifact_file=f"quality_result={quality}", item="W1")
        next_stage()
        record("code_review", item="W1")
        next_stage()

        cleanup = self.temp / "cleanup.json"
        cleanup.write_text('{"cleanup_verified":true}\n', encoding="utf-8")
        command(
            "import-artifact", self.run_id, "--source", str(cleanup),
            "--target", "artifacts/test-cleanup.json",
        )
        cleanup_logical = f"docs/loopx/runs/{self.run_id}/artifacts/test-cleanup.json"
        record("test_execution", item="W1", stage_evidence=cleanup_logical)
        next_stage()

        health_output = command("health", self.run_id)
        self.assertIn("健康检查结果：PASS", health_output)
        health_logical = f"docs/loopx/runs/{self.run_id}/artifacts/health-result.json"
        record("health_gate", stage_evidence=health_logical)
        next_stage()
        record("release_readiness")
        next_stage()

        command("git-gate", self.run_id)
        command(
            "compound", self.run_id, "--decision", "skipped", "--reason",
            "本测试仅验证存储兼容，不形成项目经验。",
        )
        compound = f"docs/loopx/runs/{self.run_id}/artifacts/compound-capture.md"
        record("final_report", stage_evidence=compound)
        command("validate", self.run_id, "--strict")
        command("close", self.run_id)

        state = self.read_run_json("state.json")
        self.assertEqual(state["status"], "PASS")
        self.assertEqual(set(state["stages"]), set(self.controller.STAGE_SEQUENCE))
        self.assertTrue(all(status == "PASS" for status in state["stages"].values()))
        container = self.read_container()
        self.assertIn("artifacts/close-evidence.json", container["files"])
        self.assertEqual([path.name for path in self.bundle.parent.iterdir()], ["run.json"])
        self.assertEqual(list((self.project / "docs" / "loopx").rglob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
