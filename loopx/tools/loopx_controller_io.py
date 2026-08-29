#!/usr/bin/env python3
"""LoopX 控制器的文件、结构定义和路径辅助函数。"""

import json
import hashlib
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from loopx_controller_store import active_run_dir, resolve_runtime_path
from loopx_controller_yaml import parse_yaml_subset


SUPPORTED_SCHEMA_KEYWORDS = {
    # "$schema" 仅声明标准方言（draft-07），自研校验器不做解释；
    # 兼容它是为了让 schema 文件能被标准 JSON Schema 工具识别。
    "$schema",
    "type",
    "enum",
    "required",
    "properties",
    "items",
    "minItems",
    "minLength",
    "additionalProperties",
    "title",
    "description",
}


def schema_root():
    return Path(__file__).resolve().parents[1] / "schemas"


def loopx_root():
    return Path(__file__).resolve().parents[1]


def source_snapshot_id(project):
    """生成当前 Git 源码快照摘要。

    摘要同时覆盖 HEAD、已跟踪变更和未跟踪文件内容；运行状态目录不属于
    被审核源码，避免 controller 自身记录导致快照漂移。
    """

    commands = (
        ["git", "rev-parse", "--verify", "HEAD"],
        [
            "git", "diff", "--binary", "--no-ext-diff", "HEAD", "--", ".",
            ":(exclude)docs/loopx/runs/**",
        ],
        [
            "git", "ls-files", "--others", "--exclude-standard", "-z", "--", ".",
            ":(exclude)docs/loopx/runs/**",
        ],
    )
    outputs = []
    try:
        for command in commands:
            completed = subprocess.run(
                command,
                cwd=project,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if completed.returncode != 0:
                return "UNAVAILABLE"
            outputs.append(completed.stdout)
        digest = hashlib.sha256()
        digest.update(b"HEAD\0" + outputs[0].strip() + b"\0DIFF\0" + outputs[1] + b"\0UNTRACKED\0")
        for raw_path in sorted(item for item in outputs[2].split(b"\0") if item):
            path = Path(project) / os.fsdecode(raw_path)
            digest.update(raw_path + b"\0")
            if path.is_symlink():
                digest.update(b"SYMLINK\0" + os.fsencode(os.readlink(path)))
                continue
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        return digest.hexdigest()
    except (OSError, subprocess.TimeoutExpired):
        return "UNAVAILABLE"


def load_schema(name):
    path = schema_root() / f"{name}.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def run_root(project):
    return project / "docs" / "loopx" / "runs"


def get_run_dir(project, run_id):
    active = active_run_dir(project, run_id)
    return active if active is not None else run_root(project) / run_id


def path_join(path, key):
    if isinstance(key, int):
        return f"{path}[{key}]" if path else f"[{key}]"
    return f"{path}.{key}" if path else str(key)


def type_matches(value, expected):
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_schema(value, schema, path=""):
    errors = []
    unsupported = sorted(set(schema) - SUPPORTED_SCHEMA_KEYWORDS)
    if unsupported:
        errors.append(f"{path or '$'} 的结构定义包含不支持的关键字：{', '.join(unsupported)}")
        return errors
    expected = schema.get("type")
    if expected and not type_matches(value, expected):
        errors.append(f"{path or '$'} 必须是 {expected}")
        return errors

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(str(item) for item in schema["enum"])
        errors.append(f"{path or '$'} 必须是以下值之一：{allowed}")

    if expected == "string" and "minLength" in schema and len(value) < int(schema["minLength"]):
        errors.append(f"{path or '$'} 长度必须大于或等于 {schema['minLength']}")

    if expected == "object":
        required = schema.get("required", [])
        for key in required:
            if key not in value or value[key] is None:
                errors.append(f"{path_join(path, key)} 为必填项")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path_join(path, key)} 不允许出现")
        for key, child_schema in properties.items():
            if key in value and value[key] is not None:
                errors.extend(validate_schema(value[key], child_schema, path_join(path, key)))

    if expected == "array":
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{path or '$'} 至少需要 {schema['minItems']} 项")
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(validate_schema(item, schema["items"], path_join(path, index)))

    return errors


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"文件不存在：{path}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"文件不是有效 JSON：{path}：{exc}") from exc


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def json_text(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def event_line(event):
    payload = {"time": datetime.now().isoformat(timespec="seconds"), **event}
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _transaction_path(root):
    return root / f".loopx-transaction-{os.getpid()}-{uuid.uuid4().hex}.json"


def _write_transaction(path, payload):
    temporary = path.with_suffix(".tmp")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _transaction_file(root, raw):
    candidate = (root / str(raw)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"事务日志包含越界路径：{raw}") from exc
    return candidate


def _restore_backup(backup, target):
    """使用保留原备份的原子替换，使回滚在中断后仍可重试。"""

    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".restore",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            with backup.open("rb") as source:
                shutil.copyfileobj(source, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, backup.stat().st_mode & 0o777)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def recover_atomic_writes(directory):
    """在持有 run 锁后恢复进程中断留下的多文件提交。"""

    root = Path(directory).resolve()
    for journal in sorted(root.glob(".loopx-transaction-*.json")):
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法恢复多文件事务：{journal}：{exc}") from exc
        if payload.get("state") not in {"PREPARED", "COMMITTED", "ROLLED_BACK"} or not isinstance(
            payload.get("entries"), list,
        ):
            raise RuntimeError(f"多文件事务日志格式无效：{journal}")
        entries = payload["entries"]
        if payload["state"] == "PREPARED":
            for entry in reversed(entries):
                target = _transaction_file(root, entry.get("target"))
                backup_raw = entry.get("backup")
                if backup_raw:
                    backup = _transaction_file(root, backup_raw)
                    if not backup.is_file():
                        raise RuntimeError(f"多文件事务缺少恢复备份：{backup}")
                    _restore_backup(backup, target)
                else:
                    target.unlink(missing_ok=True)
            # 先持久化“已回滚”再删备份；清理中断后只需继续幂等清理，
            # 不再依赖可能已删除的备份重放回滚。
            _write_transaction(journal, {"state": "ROLLED_BACK", "entries": entries})
        for entry in entries:
            for field in ("temporary", "backup"):
                raw = entry.get(field)
                if raw:
                    _transaction_file(root, raw).unlink(missing_ok=True)
        journal.unlink()


def atomic_write_texts(files):
    """以事务日志保护多文件替换；异常或下次启动时恢复旧代际。"""

    temporary = []
    backups = {}
    replaced = []
    preserve_transaction = False
    targets = [Path(raw_path).resolve() for raw_path in files]
    root = Path(os.path.commonpath([str(path.parent) for path in targets])).resolve()
    if root == Path(root.anchor):
        raise ValueError("多文件事务不能使用文件系统根目录作为事务边界")
    journal = _transaction_path(root)
    try:
        for raw_path, content in files.items():
            path = Path(raw_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            backup_path = None
            if path.exists():
                backup = tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".bak",
                    delete=False,
                )
                try:
                    backup.write(path.read_bytes())
                    backup.flush()
                    os.fsync(backup.fileno())
                finally:
                    backup.close()
                backup_path = Path(backup.name)
                os.chmod(backup_path, path.stat().st_mode & 0o777)
            backups[path] = backup_path
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            )
            try:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            temporary.append((Path(handle.name), path))
        entries = [{
            "target": path.resolve().relative_to(root).as_posix(),
            "temporary": source.resolve().relative_to(root).as_posix(),
            "backup": backups[path].resolve().relative_to(root).as_posix() if backups[path] else "",
        } for source, path in temporary]
        _write_transaction(journal, {"state": "PREPARED", "entries": entries})
        try:
            for source, target in temporary:
                source.replace(target)
                replaced.append(target)
            _write_transaction(journal, {"state": "COMMITTED", "entries": entries})
        except BaseException as exc:
            rollback_errors = []
            for target in reversed(replaced):
                backup_path = backups.get(target)
                try:
                    if backup_path is None:
                        target.unlink(missing_ok=True)
                    else:
                        _restore_backup(backup_path, target)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{target}：{rollback_exc}")
            if rollback_errors:
                preserve_transaction = True
                raise RuntimeError("多文件写入失败且无法完整恢复：" + "；".join(rollback_errors)) from exc
            raise
    finally:
        if not preserve_transaction:
            for source, _ in temporary:
                try:
                    source.unlink()
                except FileNotFoundError:
                    pass
            journal.unlink(missing_ok=True)
            for backup_path in backups.values():
                if backup_path is None:
                    continue
                try:
                    backup_path.unlink()
                except FileNotFoundError:
                    pass


def append_event(directory, event):
    event = {"time": datetime.now().isoformat(timespec="seconds"), **event}
    with (directory / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_state(project, run_id):
    return read_json(get_run_dir(project, run_id) / "state.json")


def save_state(project, run_id, state):
    write_json(get_run_dir(project, run_id) / "state.json", state)


def load_worklist(project, state):
    worklist_path = Path(state.get("worklist") or "")
    if not worklist_path.is_absolute():
        worklist_path = project_path(project, worklist_path)
    return worklist_path, parse_yaml_subset(worklist_path.read_text(encoding="utf-8"))


def project_path(project, path):
    return resolve_runtime_path(project, path or "")
