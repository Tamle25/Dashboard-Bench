"""Chạy bộ bench dạng Harbor / Terminal-Bench.

Mỗi task là một thư mục:
    task.toml        cấu hình (timeout, tài nguyên, metadata.category…)
    instruction.md   đề bài cho agent
    environment/     Dockerfile môi trường agent làm việc
    tests/           verifier, ghi reward vào /logs/verifier/reward.{txt,json}
    solution/        lời giải mẫu (agent "oracle" dùng để kiểm tra task)

Backend gọi `harbor run -p <task> -a <agent> …` cho từng task (một tiến trình / task),
rồi đọc <jobs_dir>/<job>/<trial>/result.json để lấy reward.
"""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import settings
from ..models import ModelEndpoint
from .secrets import secrets

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "jobs"}
# Khi tự nhận diện (repo không có bench.yaml): bỏ các thư mục công cụ / fixture / mẫu,
# vd. scripts/checks/test-tasks, leaderboard/…/ci, vn-bench/layer-b/_template của terminal-bench.
NOISE_DIRS = {"scripts", "tools", "leaderboard", "docs", "examples", "fixtures", "templates", "test-tasks", "build"}
MIN_AUTO_TASKS = 2
INSTRUCTION_MAX = 6000


def _skip_part(name: str) -> bool:
    return name in SKIP_DIRS or name in NOISE_DIRS or name.startswith((".", "_"))


class HarborError(Exception):
    pass


# ---------------------------------------------------------------- nhận diện task

@dataclass
class TaskInfo:
    name: str  # tên thư mục
    path: Path
    category: str
    instruction: str


def list_tasks(task_root: Path, exclude: list[str] | None = None) -> list[TaskInfo]:
    excl = set(exclude or [])
    out: list[TaskInfo] = []
    for d in sorted(p for p in task_root.iterdir() if p.is_dir()):
        toml = d / "task.toml"
        if d.name in excl or d.name.startswith(("_", ".")) or not toml.is_file():
            continue
        category = ""
        try:
            meta = tomllib.loads(toml.read_text(encoding="utf-8")).get("metadata", {})
            category = str(meta.get("category") or "")
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
            pass
        instr = ""
        if (d / "instruction.md").is_file():
            instr = (d / "instruction.md").read_text(encoding="utf-8", errors="replace")[:INSTRUCTION_MAX]
        out.append(TaskInfo(d.name, d, category, instr))
    return out


def detect_harbor_dirs(root: Path, max_depth: int = 4) -> list[tuple[str, int]]:
    """Tìm các thư mục chứa task Harbor (thư mục con có task.toml). Trả về [(đường dẫn tương đối, số task)]."""
    found: list[tuple[str, int]] = []
    root = root.resolve()
    for cur, dirs, files in os.walk(root):
        rel = Path(cur).relative_to(root)
        depth = 0 if str(rel) == "." else len(rel.parts)
        if "task.toml" in files:  # bản thân là một task: không đi sâu hơn
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not _skip_part(d)]
        n = sum(1 for d in dirs if (Path(cur) / d / "task.toml").is_file())
        if n >= MIN_AUTO_TASKS and str(rel) != ".":
            found.append((rel.as_posix(), n))
        if depth >= max_depth:
            dirs[:] = []
    return found


def detect_harbor_dirs_from_paths(paths: set[str], max_depth: int = 4) -> list[tuple[str, int]]:
    """Như detect_harbor_dirs nhưng chỉ dựa trên danh sách đường dẫn file (từ `git ls-tree`)."""
    from collections import Counter

    task_dirs = {p.rsplit("/", 1)[0] for p in paths if p.endswith("/task.toml")}
    parents: Counter[str] = Counter()
    for td in task_dirs:
        if "/" not in td:
            continue
        parent, name = td.rsplit("/", 1)
        parts = parent.split("/")
        if len(parts) > max_depth or parent in task_dirs or _skip_part(name) or any(_skip_part(p) for p in parts):
            continue
        if any("/".join(parts[:i]) in task_dirs for i in range(1, len(parts))):
            continue  # nằm bên trong một task khác
        parents[parent] += 1
    return sorted((d, n) for d, n in parents.items() if n >= MIN_AUTO_TASKS)


def count_tasks_from_paths(paths: set[str], data: str, exclude: list[str] | None = None) -> int:
    prefix = data.strip("/") + "/"
    excl = set(exclude or [])
    names = {p[len(prefix):].split("/", 1)[0] for p in paths if p.startswith(prefix) and p.endswith("/task.toml") and p[len(prefix):].count("/") == 1}
    return len({n for n in names - excl if not n.startswith(("_", "."))})


def synthesize_manifest(root: Path | None = None, dirs: list[tuple[str, int]] | None = None) -> str | None:
    """Sinh manifest YAML tạm cho repo Harbor chưa có bench.yaml."""
    import re

    import yaml

    if dirs is None:
        dirs = detect_harbor_dirs(root)
    if not dirs:
        return None
    benches = []
    for rel, _n in dirs:
        bid = re.sub(r"[^a-z0-9]+", "-", rel.lower()).strip("-")[:64] or "tasks"
        benches.append({"id": bid, "name": rel, "task": f"Task Harbor trong {rel}/", "data": rel, "scoring": {"type": "harbor"}})
    return yaml.safe_dump({"version": 1, "benches": benches}, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------- bước prepare

def run_prepare(root: Path, commands: list[str]) -> None:
    """Chạy lệnh chuẩn bị của manifest trong thư mục repo.

    CẢNH BÁO: đây là thực thi code từ repo bench. Chỉ kết nối repo bạn tin tưởng.
    "python" ở đầu lệnh được thay bằng trình thông dịch của backend.
    """
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    for cmd in commands:
        args = shlex.split(cmd, posix=True)
        if not args:
            continue
        if args[0] in ("python", "python3"):
            args[0] = sys.executable
        try:
            p = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=settings.prepare_timeout_s)
        except FileNotFoundError as e:
            raise HarborError(f"Bước prepare: không tìm thấy lệnh '{args[0]}'") from e
        except subprocess.TimeoutExpired as e:
            raise HarborError(f"Bước prepare quá {settings.prepare_timeout_s}s: {cmd}") from e
        if p.returncode != 0:
            tail = (p.stderr or p.stdout or "").strip().splitlines()[-3:]
            raise HarborError(f"Bước prepare thất bại ({cmd}): {' | '.join(tail)[:400]}")


# ---------------------------------------------------------------- kiểm tra môi trường

def preflight() -> str:
    """Kiểm tra Harbor CLI và môi trường chạy. Trả về phiên bản Harbor."""
    exe = settings.harbor_cmd[0]
    if not (shutil.which(exe) or Path(exe).is_file()):
        raise HarborError(f"Không tìm thấy lệnh '{exe}'. Cài bằng: uv tool install harbor (hoặc đặt BENCH_HARBOR_CMD).")
    try:
        v = subprocess.run([*settings.harbor_cmd, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        version = (v.stdout or v.stderr).strip().splitlines()[-1] if (v.stdout or v.stderr).strip() else "?"
    except (subprocess.TimeoutExpired, OSError) as e:
        raise HarborError(f"Không chạy được Harbor CLI: {e}") from e
    if settings.harbor_env == "docker":
        try:
            d = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            raise HarborError("Không gọi được Docker. Cài Docker Desktop, hoặc đặt BENCH_HARBOR_ENV=modal/daytona.") from e
        if d.returncode != 0:
            raise HarborError("Docker chưa chạy. Mở Docker Desktop rồi chạy lại.")
    return version


# ---------------------------------------------------------------- dựng lệnh

def build_command(ep: ModelEndpoint, task_path: Path, jobs_dir: Path, job_name: str) -> tuple[list[str], dict]:
    cmd = [
        *settings.harbor_cmd, "run",
        "-p", str(task_path), "-o", str(jobs_dir), "--job-name", job_name,
        "-e", settings.harbor_env, "-n", "1", "-y", "-q",
    ]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if ep.kind == "harbor":
        if not ep.agent:
            raise HarborError(f"{ep.id}: chưa đặt agent Harbor")
        cmd += ["-a", ep.agent]
        if ep.model_name:
            cmd += ["-m", ep.model_name]
        if ep.secret_name:
            # Nói rõ cho Harbor biến môi trường nào chứa API key, nếu không nó chỉ tìm theo
            # tên mặc định của agent (codex: OPENAI_API_KEY) và ô "Tên bí mật" sẽ vô nghĩa.
            cmd += ["--api-key-name", ep.secret_name]
    elif ep.kind == "openai":
        cmd += ["-a", ep.agent or "terminus-2", "-m", ep.model_name or ep.id, "--base-url", ep.base_url, "--api-format", "openai_chat_completions"]
        if ep.secret_name:
            cmd += ["--api-key-name", ep.secret_name]
    else:
        raise HarborError(f"{ep.id}: kiểu '{ep.kind}' không chạy được bộ bench Harbor")
    if ep.secret_name:
        value = secrets.get(ep.secret_name)
        if not value:
            raise HarborError(f"{ep.id}: thiếu bí mật {ep.secret_name}")
        env[ep.secret_name] = value
    cmd += settings.harbor_extra_args
    return cmd, env


def start_process(cmd: list[str], env: dict, log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    f = open(log_path, "wb")
    try:
        return subprocess.Popen(cmd, env=env, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)
    finally:
        f.close()  # tiến trình con giữ handle riêng


def stop_process(proc: subprocess.Popen, grace_s: float = 30) -> None:
    """Dừng nhẹ (để Harbor kịp dọn container), quá hạn thì giết cả cây tiến trình."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=grace_s)
        return
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def tail(path: Path, n: int = 1500) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-n:].decode("utf-8", errors="replace").strip()


# ---------------------------------------------------------------- đọc kết quả

@dataclass
class TrialOutcome:
    reward: float | None
    valid: bool
    exception: str | None
    tokens: int
    cost_usd: float | None
    seconds: int
    trial_dir: Path
    verifier_tail: str


def _parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_trial(job_dir: Path) -> TrialOutcome | None:
    """Đọc result.json của trial đầu tiên trong job (mỗi job ở đây chỉ có một task, một lần thử)."""
    for rp in sorted(job_dir.glob("*/result.json")):
        try:
            d = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if "task_name" not in d:
            continue
        rewards = (d.get("verifier_result") or {}).get("rewards") or {}
        reward: float | None = None
        if "reward" in rewards:
            reward = float(rewards["reward"])
        elif rewards:
            reward = sum(float(v) for v in rewards.values()) / len(rewards)
        exc = d.get("exception_info") or None
        exc_text = f"{exc.get('exception_type')}: {str(exc.get('exception_message', ''))[:300]}" if exc else None
        valid = bool(d.get("reward_valid", reward is not None))
        ar = d.get("agent_result") or {}
        t0, t1 = _parse_time(d.get("started_at")), _parse_time(d.get("finished_at"))
        trial_dir = rp.parent
        return TrialOutcome(
            reward=reward, valid=valid and reward is not None, exception=exc_text,
            tokens=int(ar.get("n_output_tokens") or 0), cost_usd=ar.get("cost_usd"),
            seconds=int((t1 - t0).total_seconds()) if t0 and t1 else 0,
            trial_dir=trial_dir, verifier_tail=tail(trial_dir / "verifier" / "test-stdout.txt"),
        )
    return None
