"""Bộ bench dạng Harbor / Terminal-Bench, chạy với Harbor CLI giả (tests/fake_harbor.py)."""

import json
import textwrap
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import ModelEndpoint
from app.services import harbor


def make_task(root: Path, rel: str, category: str = "Software") -> None:
    d = root / rel
    (d / "tests").mkdir(parents=True)
    (d / "task.toml").write_text(f'[task]\nname = "test/{d.name}"\n\n[metadata]\ncategory = "{category}"\n', encoding="utf-8")
    (d / "instruction.md").write_text(f"Đề bài của {d.name}", encoding="utf-8")


@pytest.fixture(scope="module")
def harbor_repo(tmp_path_factory) -> Path:
    """Repo giống terminal-bench-lct: tasks/, vn-bench/layer-b/tasks/, layer-a cần bước build."""
    root = tmp_path_factory.mktemp("tb-repo")
    for t in ("t-alpha", "t-beta", "infra-broken", "atrx-vep-crispr"):
        make_task(root, f"tasks/{t}", "Science" if t == "t-beta" else "Software")
    make_task(root, "vn-bench/layer-b/tasks/vn-slugify")
    make_task(root, "vn-bench/layer-b/tasks/vn-date-normalize")
    # Nhiễu giống repo thật: mẫu task và fixture của công cụ kiểm tra
    make_task(root, "vn-bench/layer-b/_template")
    make_task(root, "scripts/checks/test-tasks/fixture-a")
    make_task(root, "scripts/checks/test-tasks/fixture-b")
    (root / "vn-bench/layer-a/translations/t-alpha").mkdir(parents=True)  # có meta.toml, không phải task
    (root / "vn-bench/layer-a/translations/t-alpha/meta.toml").write_text("status = 'reviewed'", encoding="utf-8")
    (root / "build_variants.py").write_text(textwrap.dedent("""
        import shutil, sys
        variant = sys.argv[sys.argv.index("--variants") + 1]
        shutil.copytree("tasks/t-alpha", f"vn-bench/layer-a/build/{variant}/t-alpha", dirs_exist_ok=True)
    """), encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def wait_run(client, rid, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/runs/{rid}").json()
        if r["status"] not in ("queued", "running"):
            return r
        time.sleep(0.2)
    raise AssertionError("lượt chạy không kết thúc")


def test_detect_harbor_dirs(harbor_repo):
    found = dict(harbor.detect_harbor_dirs(harbor_repo))
    assert found == {"tasks": 4, "vn-bench/layer-b/tasks": 2}


def test_detect_from_paths_matches_filesystem(harbor_repo):
    paths = {p.relative_to(harbor_repo).as_posix() for p in harbor_repo.rglob("*") if p.is_file()}
    assert harbor.detect_harbor_dirs_from_paths(paths) == sorted(harbor.detect_harbor_dirs(harbor_repo))
    assert harbor.count_tasks_from_paths(paths, "tasks", ["atrx-vep-crispr"]) == 3


def test_partial_clone_probe(tmp_path, harbor_repo):
    """fetch_tree_only + list_paths + show_file trên repo git thật (qua file://)."""
    import shutil
    import subprocess

    from app.services import git_ops

    repo = tmp_path / "remote"
    shutil.copytree(harbor_repo, repo)
    (repo / "bench.yaml").write_text("version: 1\nbenches: []\n", encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "uploadpack.allowFilter", "true"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    url = "file:///" + repo.as_posix().lstrip("/")
    work = tmp_path / "probe"
    sha = git_ops.fetch_tree_only(work, url, "main", "public", None)
    paths = git_ops.list_paths(work)
    assert len(sha) == 40 and "tasks/t-alpha/task.toml" in paths and "bench.yaml" in paths
    assert git_ops.show_file(work, url, "bench.yaml", "public", None).startswith("version: 1")
    assert not (work / "tasks").exists()  # không checkout nội dung

    # Lệnh git bị ngắt giữa chừng để lại file .lock: lần sau phải tự dọn, không kẹt mãi
    (work / ".git" / "shallow.lock").write_text("", encoding="utf-8")
    (work / ".git" / "index.lock").write_text("", encoding="utf-8")
    assert git_ops.fetch_tree_only(work, url, "main", "public", None) == sha
    assert not (work / ".git" / "shallow.lock").exists()


def test_read_trial_parses_reward_and_exception(tmp_path):
    trial = tmp_path / "job" / "t__1"
    (trial / "verifier").mkdir(parents=True)
    (trial / "verifier" / "test-stdout.txt").write_text("ok", encoding="utf-8")
    (trial / "result.json").write_text(json.dumps({
        "task_name": "x", "verifier_result": {"rewards": {"reward": 1}}, "reward_valid": True,
        "exception_info": {"exception_type": "AgentTimeoutError", "exception_message": "8h"},
        "agent_result": {"n_output_tokens": 50}, "started_at": "2026-11-17T01:00:00Z", "finished_at": "2026-11-17T01:02:05Z",
    }), encoding="utf-8")
    (tmp_path / "job" / "result.json").write_text("{}", encoding="utf-8")  # result của job: bỏ qua
    out = harbor.read_trial(tmp_path / "job")
    assert out.reward == 1.0 and out.valid and out.seconds == 125 and out.tokens == 50
    assert out.exception.startswith("AgentTimeoutError") and out.verifier_tail == "ok"


def test_build_command_for_openai_endpoint(monkeypatch):
    monkeypatch.setenv("MY_KEY", "sk-test")
    ep = ModelEndpoint(id="vina-chat-v3", kind="openai", base_url="http://gpu:8000/v1", secret_name="MY_KEY", agent=None)
    cmd, env = harbor.build_command(ep, Path("/t/task"), Path("/jobs"), "job1")
    s = " ".join(cmd)
    assert "-a terminus-2" in s and "-m vina-chat-v3" in s and "--base-url http://gpu:8000/v1" in s
    assert "--api-format openai_chat_completions" in s and "--api-key-name MY_KEY" in s
    assert env["MY_KEY"] == "sk-test"
    with pytest.raises(harbor.HarborError):
        harbor.build_command(ModelEndpoint(id="m", kind="mock", base_url="mock://1"), Path("/t"), Path("/j"), "j")


def test_auto_detect_source_without_manifest(client, harbor_repo):
    test = client.post("/api/sources/test", json={"url": str(harbor_repo)}).json()
    assert test["ok"], test
    assert test["checks"][1]["label"] == "Không có bench.yaml" and test["checks"][1]["ok"]
    assert {b["key"]: b["size"] for b in test["benches"]} == {"tasks": 4, "vn-bench-layer-b-tasks": 2}


def test_manifest_with_prepare_and_exclude(client, harbor_repo):
    (harbor_repo / "bench.yaml").write_text(textwrap.dedent("""
        version: 1
        benches:
          - id: en-baseline
            name: Terminal-Bench gốc
            data: tasks
            exclude: [atrx-vep-crispr]
            scoring: {type: harbor}
          - id: vn-layer-a-vi
            name: VN Lớp A · vi
            data: vn-bench/layer-a/build/vi
            prepare: python build_variants.py --variants vi
            scoring: {type: harbor}
          - id: vn-layer-a-broken
            name: Prepare lỗi
            data: vn-bench/layer-a/build/zz
            prepare: python -c "import sys; sys.exit('build hỏng')"
            scoring: {type: harbor}
    """), encoding="utf-8")
    try:
        test = client.post("/api/sources/test", json={"url": str(harbor_repo)}).json()
        assert {b["key"]: b["size"] for b in test["benches"]} == {"en-baseline": 3, "vn-layer-a-vi": 1}
        assert any("vn-layer-a-broken" in w and "prepare" in w for w in test["warnings"])
    finally:
        (harbor_repo / "bench.yaml").unlink()
        import shutil
        shutil.rmtree(harbor_repo / "vn-bench/layer-a/build", ignore_errors=True)


@pytest.fixture(scope="module")
def seeded(client, harbor_repo):
    r = client.post("/api/sources", json={"url": str(harbor_repo), "sync_mode": "manual"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        s = next(x for x in client.get("/api/sources").json() if x["id"] == sid)
        if s["status"] in ("ok", "warn", "error"):
            break
        time.sleep(0.1)
    assert s["status"] == "ok", s
    for body in (
        {"id": "h-oracle", "kind": "harbor", "agent": "oracle", "role": "contestant"},
        {"id": "h-nop", "kind": "harbor", "agent": "nop", "role": "contestant"},
        {"id": "h-slow", "kind": "harbor", "agent": "slow", "role": "contestant"},
    ):
        m = client.post("/api/models", json=body)
        assert m.status_code == 201 and m.json()["status"] == "ok", m.text
    return {b["key"]: b for b in client.get("/api/benches").json() if b["source"].startswith(harbor_repo.name)}


def test_harbor_run_scores_from_verifier(client, seeded):
    bench = seeded["tasks"]
    assert bench["scoring_type"] == "harbor" and not bench["judge_required"]
    r = client.post("/api/runs", json={"bench_id": bench["id"], "models": ["h-oracle", "h-nop"], "samples": "all", "concurrency": 2})
    assert r.status_code == 201, r.text
    run = wait_run(client, r.json()["id"])
    assert run["status"] == "done", run
    e = {x["model"]: x for x in run["entries"]}
    # infra-broken không có reward → "cần xem lại", không tính vào điểm
    assert e["h-oracle"]["score"] == 100.0 and e["h-oracle"]["review"] == 1 and e["h-oracle"]["passed"] == 3
    assert e["h-nop"]["score"] == 0.0
    msgs = [l["msg"] for l in run["logs"]]
    assert any("Harbor 0.0.0-fake" in m for m in msgs)
    assert any("task t-alpha · reward 1" in m for m in msgs)
    assert any("infra-broken" in m and "EnvironmentBuildError" in m for m in msgs)

    detail = client.get(f"/api/results/{e['h-oracle']['id']}").json()
    assert {c["name"] for c in detail["categories"]} == {"Software", "Science"}
    samples = client.get(f"/api/results/{e['h-oracle']['id']}/samples").json()["items"]
    alpha = next(s for s in samples if s["sample_id"] == "t-alpha")
    assert alpha["prompt"] == "Đề bài của t-alpha" and alpha["note"] == "1 passed" and alpha["tokens"] == 321


def test_mock_model_rejected_for_harbor_bench(client, seeded):
    client.post("/api/models", json={"id": "m-mock", "kind": "mock", "base_url": "mock://1"})
    r = client.post("/api/runs", json={"bench_id": seeded["tasks"]["id"], "models": ["m-mock"]})
    assert r.status_code == 422 and "Harbor" in r.json()["detail"]


def test_cancel_stops_harbor_process(client, seeded):
    r = client.post("/api/runs", json={"bench_id": seeded["vn-bench-layer-b-tasks"]["id"], "models": ["h-slow"], "samples": "all"})
    rid = r.json()["id"]
    time.sleep(1.5)
    t0 = time.time()
    assert client.post(f"/api/runs/{rid}/cancel").status_code == 200
    run = wait_run(client, rid, timeout=60)
    assert run["status"] == "stopped"
    assert time.time() - t0 < 45  # tiến trình con bị dừng, không chờ hết 120s
