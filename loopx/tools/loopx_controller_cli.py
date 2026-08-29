#!/usr/bin/env python3
"""LoopX controller CLI 装配（参数解析、命令注册与 main 调度）。

 只做“解析参数 -> 绑定命令 -> 调度执行”的装配层；
 命令实现分布在 core / intake / repair / release / compound 模块。
"""

import argparse
import io
import re
import sys
from pathlib import Path, PurePosixPath

from loopx_controller_compound import cmd_compound, cmd_validate_learning
from loopx_controller_contracts import (
    CONFIRMATION_GATE_STAGES,
    STAGE_SEQUENCE,
    STAGE_STATUSES,
)
from loopx_controller_intake import cmd_init, cmd_interview, cmd_spec
from loopx_controller_release import cmd_close, cmd_git_gate
from loopx_controller_repair import (
    cmd_claim_stage,
    cmd_close_repair,
    cmd_fail_review,
    cmd_review_feedback,
)
from loopx_controller_state import default_run_id, resolve_run_id
from loopx_controller_store import ExternalRunSession, ProjectRunSession, StoreError, uses_project_backend
from loopx_controller_io import get_run_dir, recover_atomic_writes

# core 模块承载其余命令实现；此处集中 import 供 build_parser 绑定。
from loopx_controller_core import (
    cmd_advance,
    cmd_can_write,
    cmd_confirm_stage,
    cmd_gate,
    cmd_health,
    cmd_import_artifact,
    cmd_mode,
    cmd_next,
    cmd_record_stage,
    cmd_status,
    cmd_validate,
    import_artifact_files,
    restore_imported_artifacts,
)


def _translate_argument_error(message):
    replacements = (
        (r"the following arguments are required: ", "缺少必需参数："),
        (r"unrecognized arguments: ", "无法识别的参数："),
        (r"invalid choice: ", "选项值不合法："),
        (r"invalid int value: ", "整数值不合法："),
        (r"argument ([^:]+): ", r"参数 \1："),
        (r"\(choose from ", "（可选值："),
        (r"expected one argument", "需要一个值"),
        (r"expected at least one argument", "至少需要一个值"),
    )
    translated = message
    for pattern, replacement in replacements:
        translated = re.sub(pattern, replacement, translated)
    if "（可选值：" in translated and translated.endswith(")"):
        translated = translated[:-1] + "）"
    return translated


class ChineseArgumentParser(argparse.ArgumentParser):
    """保留兼容命令和参数名，仅把 argparse 的用户提示本地化。"""

    @staticmethod
    def _localize(text):
        return (
            text.replace("usage:", "用法：")
            .replace("positional arguments:", "位置参数：")
            .replace("options:", "选项：")
            .replace("show this help message and exit", "显示帮助并退出")
        )

    def format_help(self):
        return self._localize(super().format_help())

    def format_usage(self):
        return self._localize(super().format_usage())

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: 参数错误：{_translate_argument_error(message)}\n")


def build_parser():
    parser = ChineseArgumentParser(description="LoopX 状态控制器。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="创建本地 LoopX 运行。")
    init.add_argument("requirement")
    init.add_argument("--run-id")
    init.add_argument("--mode", choices=["auto", "LIGHT", "STANDARD", "FULL"], default="auto")
    init.add_argument("--risk-tags", nargs="*", default=[])
    init.add_argument(
        "--automation-policy",
        choices=["gated", "auto_until_blocked"],
        default="gated",
        help="gated 保留人工确认；auto_until_blocked 仅凭本次 init 显式授权跳过确认门，遇阻塞仍停止。",
    )
    init.add_argument("--project", default=".")
    init.set_defaults(func=cmd_init)

    status = subparsers.add_parser("status", help="查看 LoopX 运行状态。")
    status.add_argument("run_id", nargs="?")
    status.add_argument("--tracking", action="store_true")
    status.add_argument("--project", default=".")
    status.set_defaults(func=cmd_status)

    interview = subparsers.add_parser("interview", help="生成需求采访产物。")
    interview.add_argument("run_id", nargs="?")
    interview.add_argument("--project", default=".")
    interview.set_defaults(func=cmd_interview)

    spec = subparsers.add_parser("spec", help="在需求采访确认后生成需求规格。")
    spec.add_argument("run_id", nargs="?")
    spec.add_argument("--project", default=".")
    spec.set_defaults(func=cmd_spec)

    mode = subparsers.add_parser("mode", help="记录选定的 LoopX 执行等级。")
    mode.add_argument("run_id", nargs="?")
    mode.add_argument("--select", required=True, choices=["LIGHT", "STANDARD", "FULL"])
    mode.add_argument("--accepted-risk")
    mode.add_argument("--project", default=".")
    mode.set_defaults(func=cmd_mode)

    next_stage = subparsers.add_parser("next", help="检查通过后进入默认下一阶段。")
    next_stage.add_argument("run_id", nargs="?")
    next_stage.add_argument("--project", default=".")
    next_stage.set_defaults(func=cmd_next)

    validate = subparsers.add_parser("validate", help="检查 LoopX 运行状态和工作清单。")
    validate.add_argument("run_id", nargs="?")
    validate.add_argument("--strict", action="store_true")
    validate.add_argument("--project", default=".")
    validate.set_defaults(func=cmd_validate)

    gate = subparsers.add_parser("gate", help="执行严格的 LoopX 流程检查。")
    gate.add_argument("run_id", nargs="?")
    gate.add_argument("--project", default=".")
    gate.set_defaults(func=cmd_gate)

    health = subparsers.add_parser("health", help="执行配置驱动的健康检查并写入运行报告。")
    health.add_argument("run_id", nargs="?")
    health.add_argument("--project", default=".")
    health.set_defaults(func=cmd_health)

    import_artifact = subparsers.add_parser("import-artifact", help="把外部文件收纳到当前运行的 artifacts 目录。")
    import_artifact.add_argument("run_id", nargs="?")
    import_artifact.add_argument("--source", required=True)
    import_artifact.add_argument("--target", required=True)
    import_artifact.add_argument("--project", default=".")
    import_artifact.set_defaults(func=cmd_import_artifact)

    close_run = subparsers.add_parser("close", help="最终报告和严格检查通过后收口运行。")
    close_run.add_argument("run_id", nargs="?")
    close_run.add_argument("--project", default=".")
    close_run.set_defaults(func=cmd_close)

    git_gate = subparsers.add_parser("git-gate", help="记录供最终报告检查使用的 Git 变更证据。")
    git_gate.add_argument("run_id", nargs="?")
    git_gate.add_argument("--project", default=".")
    git_gate.set_defaults(func=cmd_git_gate)

    record = subparsers.add_parser("record-stage", help="记录机器可读的 LoopX 阶段结果。")
    record.add_argument("--run-id")
    record.add_argument("--stage", required=True, choices=STAGE_SEQUENCE)
    record.add_argument("--status", required=True, choices=sorted(STAGE_STATUSES))
    record.add_argument("--evidence", action="append", default=[])
    record.add_argument("--artifact", action="append", default=[], help="结构化产物，格式为“类型=项目内相对路径”，可重复。")
    record.add_argument("--artifact-file", action="append", default=[], help="导入结构化产物文件并收纳到运行状态，格式为“类型=文件路径”，可重复。")
    record.add_argument("--return-to", choices=STAGE_SEQUENCE)
    record.add_argument("--next-action")
    record.add_argument("--item", action="append")
    record.add_argument("--blocked-reason")
    record.add_argument("--project", default=".")
    record.set_defaults(func=cmd_record_stage)

    confirm = subparsers.add_parser("confirm-stage", help="记录用户对 LoopX 阶段的确认。")
    confirm.add_argument("--run-id")
    confirm.add_argument("--stage", required=True, choices=sorted(CONFIRMATION_GATE_STAGES))
    confirm.add_argument("--evidence", action="append", required=True)
    confirm.add_argument("--confirmed-by", default="user")
    confirm.add_argument("--project", default=".")
    confirm.set_defaults(func=cmd_confirm_stage)

    advance = subparsers.add_parser("advance", help="仅在前置检查通过后进入指定阶段。")
    advance.add_argument("--run-id")
    advance.add_argument("--to", required=True, choices=STAGE_SEQUENCE)
    advance.add_argument("--project", default=".")
    advance.set_defaults(func=cmd_advance)

    can_write = subparsers.add_parser("can-write", help="检查当前是否允许写入。")
    can_write.add_argument("--run-id")
    can_write.add_argument("--kind", choices=["business", "loopx"], default="business")
    can_write.add_argument("--project", default=".")
    can_write.set_defaults(func=cmd_can_write)

    fail_review_parser = subparsers.add_parser("fail-review", help="根据审核失败创建返工单并返回责任阶段。")
    fail_review_parser.add_argument("--run-id")
    fail_review_parser.add_argument("--from", dest="from_stage", required=True, choices=STAGE_SEQUENCE)
    fail_review_parser.add_argument("--return-to", required=True, choices=STAGE_SEQUENCE)
    fail_review_parser.add_argument("--item", required=True)
    fail_review_parser.add_argument("--reason", action="append", required=True)
    fail_review_parser.add_argument("--project", default=".")
    fail_review_parser.set_defaults(func=cmd_fail_review)

    claim = subparsers.add_parser("claim-stage", help="领取当前阶段并显示责任角色的待处理返工单。")
    claim.add_argument("stage", choices=STAGE_SEQUENCE)
    claim.add_argument("--run-id")
    claim.add_argument("--project", default=".")
    claim.set_defaults(func=cmd_claim_stage)

    close = subparsers.add_parser("close-repair", help="更新原产物版本后关闭返工单。")
    close.add_argument("--run-id")
    close.add_argument("--item", required=True)
    close.add_argument("--artifact", required=True)
    close.add_argument("--revision", required=True, type=int)
    close.add_argument("--change", action="append", required=True)
    close.add_argument("--project", default=".")
    close.set_defaults(func=cmd_close_repair)

    feedback = subparsers.add_parser("review-feedback", help="记录用户审核反馈并返回先前阶段。")
    feedback.add_argument("--run-id")
    feedback.add_argument("--item", required=True)
    feedback.add_argument("--return-to", required=True, choices=STAGE_SEQUENCE)
    feedback.add_argument("--reason", required=True)
    feedback.add_argument("--project", default=".")
    feedback.set_defaults(func=cmd_review_feedback)

    compound = subparsers.add_parser("compound", help="记录可复用经验或跳过沉淀的决定。")
    compound.add_argument("run_id", nargs="?")
    compound.add_argument("--decision", required=True, choices=["captured", "skipped"])
    compound.add_argument("--category", default="general")
    compound.add_argument("--title")
    compound.add_argument("--summary")
    compound.add_argument("--reason")
    compound.add_argument("--learning")
    compound.add_argument("--prevention")
    compound.add_argument("--risk-tags", nargs="*", default=None)
    compound.add_argument("--applies-to", action="append", default=[])
    compound.add_argument("--write-project-doc", action="store_true")
    compound.add_argument("--project", default=".")
    compound.set_defaults(func=cmd_compound)

    learning = subparsers.add_parser("validate-learning", help="检查 LoopX 复用经验 Markdown 文件。")
    learning.add_argument("path")
    learning.add_argument("--project", default=".")
    learning.set_defaults(func=cmd_validate_learning)
    return parser


def main(argv=None, stdout=None):
    stdout = stdout or sys.stdout
    args = build_parser().parse_args(argv)
    if args.command == "validate-learning":
        learning_parts = PurePosixPath(str(args.path).replace("\\", "/")).parts
        if len(learning_parts) < 4 or learning_parts[:3] != ("docs", "loopx", "runs"):
            return args.func(args, stdout)
        args.run_id = learning_parts[3]
    project = Path(args.project).resolve()
    if args.command == "init":
        run_id = args.run_id or default_run_id(args.requirement)
        args.run_id = run_id
    else:
        try:
            run_id = resolve_run_id(project, getattr(args, "run_id", None))
        except ValueError as exc:
            print(str(exc), file=stdout)
            return 1
        args.run_id = run_id
    try:
        project_backend = uses_project_backend(project, run_id)
    except (StoreError, OSError) as exc:
        print(f"状态存储错误：{exc}", file=stdout)
        return 1
    if project_backend:
        try:
            with ProjectRunSession(project, run_id, create=args.command == "init"):
                run_directory = get_run_dir(project, run_id)
                if run_directory.is_dir():
                    recover_atomic_writes(run_directory)
                imported_backups = []
                if args.command == "record-stage" and args.artifact_file:
                    try:
                        args.artifact.extend(import_artifact_files(
                            project, run_id, args.stage, args.artifact_file, imported_backups,
                        ))
                    except (OSError, ValueError) as exc:
                        restore_imported_artifacts(imported_backups)
                        print(f"无法导入结构化产物：{exc}", file=stdout)
                        return 1
                result = args.func(args, stdout)
                if args.command == "record-stage" and result != 0:
                    restore_imported_artifacts(imported_backups)
                return result
        except (StoreError, OSError, RuntimeError) as exc:
            print(f"状态存储错误：{exc}", file=stdout)
            return 1

    buffered = io.StringIO()
    try:
        with ExternalRunSession(project, run_id, create=args.command == "init") as session:
            imported_backups = []
            if args.command == "record-stage" and args.artifact_file:
                try:
                    args.artifact.extend(import_artifact_files(
                        project, run_id, args.stage, args.artifact_file, imported_backups,
                    ))
                except (OSError, ValueError) as exc:
                    raise StoreError(f"无法导入结构化产物：{exc}") from exc
            result = args.func(args, buffered)
            if args.command == "record-stage" and result != 0:
                restore_imported_artifacts(imported_backups)
            if args.command != "import-artifact" or result == 0:
                session.commit()
    except (StoreError, OSError) as exc:
        print(f"状态存储错误：{exc}", file=stdout)
        return 1
    print(buffered.getvalue(), end="", file=stdout)
    return result


if __name__ == "__main__":
    sys.exit(main())
