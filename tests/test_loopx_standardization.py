import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK_PATH = ROOT / "loopx" / "tools" / "loopx_check.py"


def load_check_module():
    spec = importlib.util.spec_from_file_location("loopx_check", CHECK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LoopXStandardizationTest(unittest.TestCase):
    def setUp(self):
        self.check = load_check_module()

    def test_standardization_assets_pass_package_harness(self):
        report = self.check.evaluate_package(ROOT)
        messages = "\n".join(f"{item.name}: {item.message}" for item in report.checks)
        self.assertEqual(report.status, self.check.PASS, messages)

    def test_each_required_standard_declares_check_language(self):
        base = ROOT / "loopx" / "standards"
        for name in self.check.REQUIRED_STANDARDS:
            with self.subTest(name=name):
                text = (base / name).read_text(encoding="utf-8")
                self.assertIn("通过标准", text)
                self.assertTrue("失败" in text or "返回规则" in text)
                self.assertIn("证据", text)

    def test_required_skills_are_small_contracts(self):
        base = ROOT / "loopx" / "skills"
        for name in self.check.REQUIRED_SKILLS:
            with self.subTest(name=name):
                text = (base / name).read_text(encoding="utf-8")
                self.assertIn("目的", text)
                self.assertIn("输入", text)
                self.assertIn("输出", text)
                self.assertIn("通过标准", text)
                self.assertIn("失败处理", text)


    def test_required_front_gate_schemas_are_first_class_contracts(self):
        required = {
            "interview.schema.json",
            "spec.schema.json",
            "mode.schema.json",
            "tracking.schema.json",
        }
        self.assertTrue(required.issubset(set(self.check.REQUIRED_SCHEMAS)))
        base = ROOT / "loopx" / "schemas"
        for name in required:
            with self.subTest(name=name):
                text = (base / name).read_text(encoding="utf-8")
                self.assertIn('"type": "object"', text)

    def test_front_gate_agent_docs_define_role_boundaries(self):
        required = [
            "requirement-interviewer-agent.md",
            "spec-writer-agent.md",
            "spec-reviewer-agent.md",
            "mode-selector-agent.md",
        ]
        self.assertTrue(set(required).issubset(set(self.check.REQUIRED_AGENT_DOCS)))
        base = ROOT / "loopx" / "agents"
        for name in required:
            with self.subTest(name=name):
                text = (base / name).read_text(encoding="utf-8")
                for term in ("职责", "输入", "输出", "检查", "禁止事项"):
                    self.assertIn(term, text)
                self.assertIn("不得", text)

    def test_process_standard_assets_and_schemas_are_complete(self):
        standards = {
            "principles.md",
            "architecture-standard.md",
            "security-standard.md",
            "performance-standard.md",
            "reliability-observability-standard.md",
            "catalog.yml",
        }
        schemas = {
            "standard-catalog.schema.json",
            "project-policy.schema.json",
            "requirement-manifest.schema.json",
            "solution.schema.json",
            "test-plan.schema.json",
            "development-evidence.schema.json",
            "quality-result.schema.json",
            "performance-result.schema.json",
            "security-result.schema.json",
        }
        self.assertTrue(standards.issubset({path.name for path in (ROOT / "loopx" / "standards").iterdir()}))
        self.assertTrue(schemas.issubset({path.name for path in (ROOT / "loopx" / "schemas").iterdir()}))
        for name in schemas:
            with self.subTest(schema=name):
                schema = json.loads((ROOT / "loopx" / "schemas" / name).read_text(encoding="utf-8"))
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema.get("additionalProperties", True))

        for manifest_path, prefix in ((ROOT / "manifest.json", "loopx/"), (ROOT / "loopx" / "manifest.json", "")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertIn(f"{prefix}schemas/requirement-manifest.schema.json", manifest["resources"]["schemas"])

    def test_catalog_references_existing_rules_sources_and_artifacts(self):
        tools = ROOT / "loopx" / "tools"
        if str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        from loopx_controller_policy import load_catalog

        catalog, _ = load_catalog(ROOT)
        rules = {rule["id"]: rule for rule in catalog["rules"]}
        artifact_schemas = {
            "solution": "solution.schema.json",
            "test_plan": "test-plan.schema.json",
            "development_evidence": "development-evidence.schema.json",
            "quality_result": "quality-result.schema.json",
            "performance_result": "performance-result.schema.json",
            "security_result": "security-result.schema.json",
        }
        for name, rule_ids in catalog["rule_sets"].items():
            with self.subTest(rule_set=name):
                self.assertTrue(rule_ids)
                self.assertTrue(set(rule_ids).issubset(rules))
        for rule in rules.values():
            with self.subTest(rule=rule["id"]):
                self.assertTrue((ROOT / "loopx" / rule["source"]).is_file())
                for artifact_type in rule["evidence_types"]:
                    self.assertTrue((ROOT / "loopx" / "schemas" / artifact_schemas[artifact_type]).is_file())

    def test_user_visible_documents_use_natural_process_language(self):
        files = [
            ROOT / "README.md",
            ROOT / "README.zh-CN.md",
            ROOT / "loopx" / "README.md",
            ROOT / "loopx" / "SKILL.md",
            ROOT / "loopx" / "workflow.md",
            *sorted((ROOT / "loopx" / "templates").glob("*.md")),
            *sorted((ROOT / "loopx" / "agents").glob("*.md")),
            *sorted((ROOT / "loopx" / "skills").glob("*.md")),
        ]
        forbidden = ("质量门", "评审门", "审核门", "人工确认门", "健康门", "闸门")
        for path in files:
            text = path.read_text(encoding="utf-8")
            for phrase in forbidden:
                with self.subTest(path=path.name, phrase=phrase):
                    self.assertNotIn(phrase, text)

        normal_security_term = "安全\u95e8禁系统"
        self.assertTrue(all(phrase not in normal_security_term for phrase in forbidden))

    def test_stage_templates_use_contract_ids_and_chinese_learning_headings(self):
        expected_stages = {
            "01-assignment.md": "requirement_intake",
            "03-solution-review.md": "solution_review",
            "05-test-review.md": "test_review",
            "08-code-review.md": "code_review",
        }
        for filename, stage in expected_stages.items():
            with self.subTest(filename=filename):
                text = (ROOT / "loopx" / "templates" / filename).read_text(encoding="utf-8")
                self.assertIn(f"  stage: {stage}", text)

        learning = (ROOT / "loopx" / "templates" / "13-compound-capture.md").read_text(encoding="utf-8")
        for heading in ("## 摘要", "## 经验", "## 预防措施"):
            self.assertIn(heading, learning)


class LoopxCliLanguageTest(unittest.TestCase):
    COMMANDS = (
        "init",
        "status",
        "interview",
        "spec",
        "mode",
        "next",
        "validate",
        "gate",
        "health",
        "import-artifact",
        "close",
        "git-gate",
        "record-stage",
        "confirm-stage",
        "advance",
        "can-write",
        "fail-review",
        "claim-stage",
        "close-repair",
        "review-feedback",
        "compound",
        "validate-learning",
    )

    def run_cli(self, *args, cwd=ROOT):
        environment = dict(os.environ)
        environment["LOOPX_STATE_BACKEND"] = "project"
        return subprocess.run(
            [sys.executable, str(ROOT / "loopx" / "tools" / "loopx_controller.py"), *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

    def test_commands_and_messages(self):
        completed = self.run_cli("--help")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("用法：", completed.stdout)
        for command in self.COMMANDS:
            self.assertIn(command, completed.stdout)

        for command in self.COMMANDS:
            with self.subTest(command=command):
                help_result = self.run_cli(command, "--help")
                self.assertEqual(help_result.returncode, 0, help_result.stderr)
                self.assertIn("用法：", help_result.stdout)
                self.assertIn("选项：", help_result.stdout)
                self.assertNotIn("show this help message and exit", help_result.stdout)

        with tempfile.TemporaryDirectory(prefix="loopx-cli-language-") as raw:
            project = Path(raw)
            created = self.run_cli(
                "init",
                "验证自然中文命令输出",
                "--run-id",
                "cli-language-run",
                "--mode",
                "LIGHT",
                "--project",
                str(project),
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertIn("PASS 已创建运行：cli-language-run", created.stdout)
            self.assertIn("执行等级：LIGHT", created.stdout)

            process_error = self.run_cli(
                "spec",
                "cli-language-run",
                "--project",
                str(project),
            )
            self.assertEqual(process_error.returncode, 1)
            self.assertIn("FAIL 需求规格生成被阻止", process_error.stdout)
            self.assertIn("进入 spec_draft 前", process_error.stdout)

        argument_error = self.run_cli("record-stage", "--status", "PASS")
        self.assertEqual(argument_error.returncode, 2)
        self.assertIn("参数错误：缺少必需参数：--stage", argument_error.stderr)
        invalid_choice = self.run_cli("record-stage", "--stage", "unknown", "--status", "PASS")
        self.assertEqual(invalid_choice.returncode, 2)
        self.assertIn("参数错误：参数 --stage：选项值不合法", invalid_choice.stderr)
        self.assertNotIn("invalid choice", invalid_choice.stderr)

        tools = ROOT / "loopx" / "tools"
        if str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        from loopx_controller_contracts import STAGE_DISPLAY_NAMES

        self.assertEqual(STAGE_DISPLAY_NAMES["health_gate"], "健康检查")
        display = "\n".join(STAGE_DISPLAY_NAMES.values())
        for phrase in ("质量门", "评审门", "审核门", "健康门", "闸门"):
            self.assertNotIn(phrase, display)

        normal_security_term = "安全\u95e8禁系统"
        self.assertIn("安全", normal_security_term)
        self.assertTrue(all(phrase not in normal_security_term for phrase in ("质量门", "审核门", "健康门", "闸门")))


if __name__ == "__main__":
    unittest.main()
