#!/usr/bin/env python3
"""把 LoopX 逻辑运行目录还原并原子保存为用户级单文件。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath


STORAGE_VERSION = "1"
STATE_DIR_ENV, BACKEND_ENV, PROJECT_BACKEND = "LOOPX_STATE_DIR", "LOOPX_STATE_BACKEND", "project"


class StoreError(ValueError):
    """用户可理解的存储错误。"""

_ACTIVE_RUN_DIRS: dict[tuple[str, str], Path] = {}

def _project_key(project: Path, run_id: str) -> tuple[str, str]:
    return str(Path(project).resolve()), run_id


def active_run_dir(project: Path, run_id: str) -> Path | None:
    return _ACTIVE_RUN_DIRS.get(_project_key(project, run_id))


def state_root() -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LoopX" / "state"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "LoopX" / "state"
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".local" / "state") / "loopx"


def project_id(project: Path) -> str:
    raw = str(Path(project).resolve()).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def bundle_path(project: Path, run_id: str) -> Path:
    _validate_run_id(run_id)
    return state_root() / project_id(project) / run_id / "run.json"


def legacy_run_dir(project: Path, run_id: str) -> Path:
    _validate_run_id(run_id)
    return Path(project).resolve() / "docs" / "loopx" / "runs" / run_id


def uses_project_backend(project: Path, run_id: str) -> bool:
    if legacy_run_dir(project, run_id).is_dir():
        return True
    if bundle_path(project, run_id).is_file():
        return False
    return os.environ.get(BACKEND_ENV, "").strip().lower() == PROJECT_BACKEND


def external_runs(project: Path) -> list[tuple[str, float]]:
    root = state_root() / project_id(project)
    if not root.is_dir():
        return []
    result = []
    for directory in root.iterdir():
        path = directory / "run.json"
        if directory.is_dir() and path.is_file():
            result.append((directory.name, path.stat().st_mtime))
    return result


def resolve_runtime_path(project: Path, raw_path: str | Path) -> Path:
    """把状态中的逻辑运行路径解析到当前命令的临时视图。"""
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    parts = PurePosixPath(str(raw_path).replace(os.sep, "/")).parts
    prefix = ("docs", "loopx", "runs")
    if len(parts) >= 4 and parts[:3] == prefix:
        active = active_run_dir(project, parts[3])
        if active is not None:
            return active.joinpath(*parts[4:])
    return Path(project) / candidate


def runtime_relative_path(project: Path, resolved: Path) -> str | None:
    """把临时视图中的文件转换回稳定的项目逻辑相对路径。"""
    target = Path(resolved).resolve()
    for (project_key, run_id), directory in _ACTIVE_RUN_DIRS.items():
        if project_key != str(Path(project).resolve()):
            continue
        try:
            suffix = target.relative_to(directory.resolve())
        except ValueError:
            continue
        return (PurePosixPath("docs/loopx/runs") / run_id / PurePosixPath(suffix.as_posix())).as_posix()
    return None


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not run_id or "\\" in run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise StoreError("运行 ID 不能包含路径片段")


def _safe_relative(raw: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise StoreError(f"运行容器包含非法路径：{raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise StoreError(f"运行容器包含越界路径：{raw}")
    return path


def _canonical_digest(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _encode_file(path: Path) -> dict:
    data = path.read_bytes()
    try:
        return {"encoding": "utf-8", "content": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"encoding": "base64", "content": base64.b64encode(data).decode("ascii")}


def _decode_file(value: dict, label: str) -> bytes:
    if not isinstance(value, dict) or set(value) != {"encoding", "content"}:
        raise StoreError(f"运行容器文件条目无效：{label}")
    encoding = value.get("encoding")
    content = value.get("content")
    if not isinstance(content, str):
        raise StoreError(f"运行容器文件内容无效：{label}")
    if encoding == "utf-8":
        return content.encode("utf-8")
    if encoding == "base64":
        try:
            return base64.b64decode(content, validate=True)
        except ValueError as exc:
            raise StoreError(f"运行容器 base64 内容无效：{label}") from exc
    raise StoreError(f"运行容器文件编码不受支持：{label}")


def build_container(project: Path, run_id: str, directory: Path) -> dict:
    files = {}
    directories = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise StoreError(f"运行逻辑目录不允许符号链接：{path}")
        relative = path.relative_to(directory).as_posix()
        _safe_relative(relative)
        if path.is_dir():
            directories.append(relative)
        elif path.is_file():
            files[relative] = _encode_file(path)
        else:
            raise StoreError(f"运行逻辑目录包含不支持的文件类型：{path}")
    payload = {
        "storage_version": STORAGE_VERSION,
        "project": str(Path(project).resolve()),
        "run_id": run_id,
        "directories": directories,
        "files": files,
    }
    return {**payload, "digest": _canonical_digest(payload)}


def load_container(path: Path, project: Path, run_id: str) -> dict:
    try:
        container = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StoreError(f"运行不存在：{run_id}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise StoreError(f"无法读取运行容器：{path}：{exc}") from exc
    if not isinstance(container, dict):
        raise StoreError("运行容器根节点必须是对象")
    payload = {key: value for key, value in container.items() if key != "digest"}
    if container.get("digest") != _canonical_digest(payload):
        raise StoreError("运行容器摘要校验失败，文件可能已被修改")
    if container.get("storage_version") != STORAGE_VERSION:
        raise StoreError(f"不支持的运行容器版本：{container.get('storage_version')}")
    if container.get("run_id") != run_id:
        raise StoreError("运行容器的 run_id 与目录不一致")
    if container.get("project") != str(Path(project).resolve()):
        raise StoreError("运行容器记录的项目路径与当前项目不一致")
    if not isinstance(container.get("directories"), list) or not isinstance(container.get("files"), dict):
        raise StoreError("运行容器缺少有效的目录或文件映射")
    return container


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class ExternalRunSession:
    """一次外部运行命令的锁定、还原和提交会话。"""

    def __init__(self, project: Path, run_id: str, *, create: bool = False):
        _validate_run_id(run_id)
        self.project = Path(project).resolve()
        self.run_id = run_id
        self.create = create
        self.bundle = bundle_path(self.project, run_id)
        self.lock = self.bundle.with_name("run.lock")
        self.token = uuid.uuid4().hex
        self.owner = self.bundle.with_name(f".run.{self.token}.owner")
        self.temp: tempfile.TemporaryDirectory | None = None
        self.directory: Path | None = None
        self.original_digest = ""

    def __enter__(self):
        self.bundle.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.bundle.parent, 0o700)
        except OSError:
            pass
        try:
            self._acquire_lock()
            self.temp = tempfile.TemporaryDirectory(prefix="loopx-run-")
            self.directory = Path(self.temp.name) / self.run_id
            if self.bundle.is_file():
                container = load_container(self.bundle, self.project, self.run_id)
                self.original_digest = container["digest"]
                self._materialize(container)
            elif not self.create:
                raise StoreError(f"运行不存在：{self.run_id}")
            _ACTIVE_RUN_DIRS[_project_key(self.project, self.run_id)] = self.directory
            return self
        except Exception:
            self._cleanup()
            raise

    def commit(self) -> None:
        if self.directory is None or not self.directory.is_dir():
            return
        container = build_container(self.project, self.run_id, self.directory)
        if self.bundle.is_file() and container["digest"] == self.original_digest:
            return
        self._atomic_write(container)
        self.original_digest = container["digest"]

    def __exit__(self, exc_type, exc, traceback):
        self._cleanup()
        return False

    def _materialize(self, container: dict) -> None:
        try:
            self.directory.mkdir(parents=True, mode=0o700)
            for raw in container["directories"]:
                relative = _safe_relative(raw)
                self.directory.joinpath(*relative.parts).mkdir(parents=True, exist_ok=True, mode=0o700)
            for raw, value in container["files"].items():
                relative = _safe_relative(raw)
                target = self.directory.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_bytes(_decode_file(value, raw))
        except OSError as exc:
            raise StoreError(f"无法还原运行容器：{self.bundle}：{exc}") from exc

    def _atomic_write(self, container: dict) -> None:
        encoded = json.dumps(container, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        temporary = self.bundle.with_name(f".run.{self.token}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.bundle)
            try:
                os.chmod(self.bundle, 0o600)
                directory_fd = os.open(self.bundle.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        except OSError as exc:
            raise StoreError(f"运行容器提交失败：{self.bundle}：{exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _acquire_lock(self) -> None:
        payload = json.dumps({"pid": os.getpid(), "token": self.token, "created_at": time.time()})
        try:
            descriptor = os.open(self.owner, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise StoreError(f"无法创建运行锁所有权文件：{self.owner}：{exc}") from exc
        for _ in range(3):
            try:
                os.link(self.owner, self.lock)
                return
            except FileExistsError:
                try:
                    current = json.loads(self.lock.read_text(encoding="utf-8"))
                    pid = int(current.get("pid") or 0)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    current = {}
                    pid = 0
                if _pid_alive(pid):
                    raise StoreError(f"运行正在被进程 {pid} 修改，请稍后重试：{self.lock}")
                self._reclaim_stale_lock(current)
            except OSError as exc:
                raise StoreError(f"无法创建运行锁：{self.lock}：{exc}") from exc
        raise StoreError(f"无法获取运行锁：{self.lock}")

    def _reclaim_stale_lock(self, current) -> None:
        """先原子取得旧 owner 所有权，再移除对应锁，避免多个回收者互删。"""
        stale_token = str(current.get("token") or "") if isinstance(current, dict) else ""
        if not stale_token:
            raise StoreError(f"无法安全回收缺少 token 的运行锁：{self.lock}")
        if len(stale_token) != 32 or any(char not in "0123456789abcdef" for char in stale_token):
            raise StoreError(f"无法安全回收 token 非法的运行锁：{self.lock}")
        stale_owner = self.lock.with_name(f".run.{stale_token}.owner")
        claim = self.lock.with_name(f".run.{stale_token}.{self.token}.reclaim")
        try:
            source = stale_owner
            if not source.exists():
                candidates = []
                for candidate in self.lock.parent.glob(f".run.{stale_token}.*.reclaim"):
                    try:
                        if os.path.samestat(self.lock.stat(), candidate.stat()):
                            candidates.append(candidate)
                    except FileNotFoundError:
                        continue
                if len(candidates) != 1:
                    return
                source = candidates[0]
            if not os.path.samestat(self.lock.stat(), source.stat()):
                raise StoreError(f"运行锁所有权文件与锁不一致：{self.lock}")
            source.rename(claim)
            try:
                if not os.path.samestat(self.lock.stat(), claim.stat()):
                    return
                try:
                    value = json.loads(claim.read_text(encoding="utf-8"))
                    pid = int(value.get("pid") or 0)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pid = 0
                if not _pid_alive(pid):
                    self.lock.unlink(missing_ok=True)
                else:
                    claim.rename(stale_owner)
            except FileNotFoundError:
                return
        except FileNotFoundError:
            # 其他回收者已取得 stale_owner 时，本进程不得再删除 run.lock。
            return
        except OSError as exc:
            raise StoreError(f"无法安全回收失效运行锁：{self.lock}：{exc}") from exc
        finally:
            claim.unlink(missing_ok=True)

    def _release_lock(self) -> None:
        try:
            if os.path.samestat(self.lock.stat(), self.owner.stat()):
                self.lock.unlink(missing_ok=True)
        except OSError:
            pass
        self.owner.unlink(missing_ok=True)

    def _cleanup(self) -> None:
        _ACTIVE_RUN_DIRS.pop(_project_key(self.project, self.run_id), None)
        try:
            if self.temp is not None:
                self.temp.cleanup()
        finally:
            self.temp = None
            self._release_lock()
        if not self.bundle.exists():
            try:
                self.bundle.parent.rmdir()
                self.bundle.parent.parent.rmdir()
            except OSError:
                pass


class ProjectRunSession(ExternalRunSession):
    """为项目目录后端复用同一把每运行锁，不改变其文件布局。"""

    def __enter__(self):
        self.bundle.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.bundle.parent, 0o700)
        except OSError:
            pass
        try:
            self._acquire_lock()
            return self
        except Exception:
            self._release_lock()
            raise

    def __exit__(self, exc_type, exc, traceback):
        self._release_lock()
        if not self.bundle.exists():
            try:
                self.bundle.parent.rmdir()
                self.bundle.parent.parent.rmdir()
            except OSError:
                pass
        return False
