import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "loopx" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from loopx_controller_io import validate_schema  # noqa: E402
from loopx_controller_policy import (  # noqa: E402
    build_policy_snapshot,
    canonical_digest,
    load_catalog,
    load_policy_snapshot,
    load_project_policy,
    load_risk_config,
    validate_catalog,
    validate_risk_profiles,
)
from loopx_controller_yaml import YamlSubsetError, parse_yaml_subset  # noqa: E402


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.catalog, self.catalog_path = load_catalog(ROOT)

    def test_valid_catalog(self):
        self.assertEqual(self.catalog["catalog_version"], "3")
        self.assertEqual(validate_catalog(self.catalog, self.catalog_path), [])
        self.assertEqual(
            set(self.catalog["rule_sets"]),
            {"common", "architecture", "security", "performance", "reliability", "observability", "testing"},
        )
        ids = [rule["id"] for rule in self.catalog["rules"]]
        self.assertEqual(len(ids), len(set(ids)))
        for rule in self.catalog["rules"]:
            self.assertIn(rule["check"]["type"], {"schema", "builtin", "command", "review"})
            self.assertTrue(rule["evidence_types"])

    def test_invalid_catalog_matrix(self):
        mutations = []

        unknown_root = copy.deepcopy(self.catalog)
        unknown_root["typo"] = True
        mutations.append((unknown_root, "catalog.typo 不允许出现"))

        duplicate = copy.deepcopy(self.catalog)
        duplicate["rules"][1]["id"] = duplicate["rules"][0]["id"]
        mutations.append((duplicate, "id 重复"))

        dangling_set = copy.deepcopy(self.catalog)
        dangling_set["rule_sets"]["common"].append("UNKNOWN-RULE")
        mutations.append((dangling_set, "引用了未知规则"))

        unknown_stage = copy.deepcopy(self.catalog)
        unknown_stage["rules"][0]["stages"] = ["unknown_stage"]
        mutations.append((unknown_stage, "未知阶段"))

        unknown_check = copy.deepcopy(self.catalog)
        unknown_check["rules"][0]["check"]["id"] = "unknown_check"
        mutations.append((unknown_check, "不是已登记检查"))

        unknown_artifact = copy.deepcopy(self.catalog)
        unknown_artifact["rules"][0]["evidence_types"] = ["unknown_artifact"]
        mutations.append((unknown_artifact, "未知产物类型"))

        missing_source = copy.deepcopy(self.catalog)
        missing_source["rules"][0]["source"] = "standards/not-found.md"
        mutations.append((missing_source, "source 不存在"))

        for catalog, message in mutations:
            with self.subTest(message=message):
                errors = validate_catalog(catalog, self.catalog_path)
                self.assertTrue(errors)
                self.assertIn(message, "\n".join(errors))


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.catalog, _ = load_catalog(ROOT)
        self.risk = load_risk_config(ROOT)

    def test_risk_profile_bijection(self):
        self.assertEqual(validate_risk_profiles(self.catalog, self.risk), [])

        missing = copy.deepcopy(self.risk)
        missing["risk_profiles"].pop("auth")
        self.assertIn("缺少 profile", "\n".join(validate_risk_profiles(self.catalog, missing)))

        extra = copy.deepcopy(self.risk)
        extra["risk_profiles"]["undeclared"] = {"minimum_mode": "LIGHT", "rule_sets": ["common"]}
        self.assertIn("未声明标签", "\n".join(validate_risk_profiles(self.catalog, extra)))

        empty = copy.deepcopy(self.risk)
        empty["risk_profiles"]["docs_only"]["rule_sets"] = []
        self.assertIn("必须说明理由", "\n".join(validate_risk_profiles(self.catalog, empty)))

        dangling = copy.deepcopy(self.risk)
        dangling["risk_profiles"]["docs_only"]["rule_sets"] = ["unknown"]
        self.assertIn("未知规则集合", "\n".join(validate_risk_profiles(self.catalog, dangling)))

    def test_mode_and_risk_rule_selection(self):
        # 用隔离的空项目目录构建快照：本测试只验证模式与风险标签的规则选择，
        # 不能依赖仓库根目录自身的特征文件（如 pyproject.toml），
        # 否则会命中 project-profiles 的自动检测导致期望值漂移。
        with tempfile.TemporaryDirectory(prefix="loopx-policy-") as raw:
            isolated = Path(raw)
            cases = [
                ("LIGHT", ["docs_only"], {"common"}, {"COMMON-EVIDENCE-001"}),
                ("STANDARD", ["performance"], {"common", "performance", "testing"}, {"PERF-TARGET-001", "TEST-MAPPING-001"}),
                ("FULL", ["core_state_transition"], {"common", "architecture", "reliability", "testing"}, {"ARCH-SIMPLE-001", "REL-RECOVERY-001"}),
            ]
            for mode, tags, expected_sets, expected_rules in cases:
                with self.subTest(mode=mode, tags=tags):
                    snapshot = build_policy_snapshot(isolated, mode, tags)
                    self.assertEqual(set(snapshot["rule_sets"]), expected_sets)
                    selected = {rule["id"] for rule in snapshot["rules"]}
                    self.assertTrue(expected_rules.issubset(selected))
                    if tags == ["docs_only"]:
                        self.assertNotIn("SEC-CONTROLS-001", selected)

    def test_full_without_risk_still_requires_complete_solution_review(self):
        with tempfile.TemporaryDirectory(prefix="loopx-policy-") as raw:
            snapshot = build_policy_snapshot(Path(raw), "FULL", [])
        selected = {rule["id"] for rule in snapshot["rules"]}
        self.assertIn("ARCH-REVIEW-COVERAGE-004", selected)

    def test_policy_precedence_and_downgrade(self):
        with tempfile.TemporaryDirectory(prefix="loopx-std-") as raw:
            project = Path(raw)
            policy = project / "loopx-policy.yml"
            policy.write_text(
                """policy_version: \"1\"
rule_sets: []
rule_overrides:
  - rule_id: COMMON-EVIDENCE-001
    level: required
    unavailable: BLOCKED
    reason: 项目保持公共要求
thresholds:
  - id: coverage
    value: \"90%\"
    source: 项目质量基线
    scope: src
commands:
  - id: project-test
    argv:
      - python3
      - -m
      - unittest
    timeout_seconds: 300
    required: true
    ci_only: false
ci_checks: []
extensions: {}
""",
                encoding="utf-8",
            )
            snapshot = build_policy_snapshot(project, "STANDARD", ["ambiguous_requirement"])
            self.assertEqual(snapshot["thresholds"][0]["source"], "项目质量基线")
            self.assertEqual(snapshot["commands"][0]["argv"], ["python3", "-m", "unittest"])
            common = next(rule for rule in snapshot["rules"] if rule["id"] == "COMMON-EVIDENCE-001")
            self.assertEqual(common["level"], "required")

            text = policy.read_text(encoding="utf-8").replace("level: required", "level: recommended")
            policy.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "不能静默降低"):
                build_policy_snapshot(project, "STANDARD", ["ambiguous_requirement"])

    def test_snapshot_is_fixed_and_hashed(self):
        with tempfile.TemporaryDirectory(prefix="loopx-std-") as raw:
            project = Path(raw)
            run_id = "snapshot-run"
            snapshot = build_policy_snapshot(project, "LIGHT", ["docs_only"])
            path = project / "docs" / "loopx" / "runs" / run_id / "artifacts" / "policy-snapshot.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            state = {
                "contract_version": "2",
                "catalog_version": snapshot["catalog_version"],
                "mode": "LIGHT",
                "policy_snapshot": path.relative_to(project).as_posix(),
                "policy_snapshot_sha256": snapshot["digest"],
            }
            loaded = load_policy_snapshot(project, state)
            self.assertEqual(loaded["digest"], snapshot["digest"])

            tampered = dict(snapshot)
            tampered["mode"] = "FULL"
            path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "摘要校验失败"):
                load_policy_snapshot(project, state)

            payload = dict(snapshot)
            digest = payload.pop("digest")
            self.assertEqual(digest, canonical_digest(payload))


class SchemaSubsetTest(unittest.TestCase):
    def test_supported_schema_subset(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "items"],
            "properties": {
                "name": {"type": "string", "minLength": 2},
                "items": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            },
        }
        self.assertEqual(validate_schema({"name": "ok", "items": ["x"]}, schema), [])
        errors = validate_schema({"name": "", "items": [], "typo": True}, schema)
        self.assertIn("name 长度必须大于或等于 2", "\n".join(errors))
        self.assertIn("items 至少需要 1 项", "\n".join(errors))
        self.assertIn("typo 不允许出现", "\n".join(errors))

        unsupported = validate_schema({}, {"type": "object", "$ref": "other"})
        self.assertIn("不支持的关键字", "\n".join(unsupported))


class YamlSafetyTest(unittest.TestCase):
    def test_duplicate_keys_and_unknown_policy_fields(self):
        with self.assertRaisesRegex(YamlSubsetError, "duplicate key"):
            parse_yaml_subset("name: one\nname: two\n")
        with self.assertRaisesRegex(YamlSubsetError, "duplicate key in list item"):
            parse_yaml_subset("items:\n  - id: one\n    id: two\n")

        with tempfile.TemporaryDirectory(prefix="loopx-std-") as raw:
            project = Path(raw)
            (project / "loopx-policy.yml").write_text(
                """policy_version: \"1\"
rule_sets: []
rule_overrides: []
thresholds: []
commands: []
ci_checks: []
extensions: {}
commandz: []
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "commandz 不允许出现"):
                load_project_policy(project)


class QualityThresholdTest(unittest.TestCase):
    def test_project_owned_thresholds(self):
        quality = (ROOT / "loopx" / "standards" / "quality-standard.md").read_text(encoding="utf-8")
        self.assertNotIn("源文件超过 500 行", quality)
        self.assertNotIn("函数或方法超过 60 行", quality)
        self.assertIn("项目策略", quality)
        self.assertIn("未配置时不参与通过判断", quality)


if __name__ == "__main__":
    unittest.main()
