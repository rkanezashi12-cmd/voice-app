"""scripts/lib/env.sh：接続先の GCP を deploy/ops.env から読む。既定のプロジェクトは持たない。"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOW = 'source scripts/lib/env.sh && echo "$PROJECT|$REGION|$SERVICE|$RUN_SA|$TASKS_SA"'


def run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    base = {"PATH": "/usr/bin:/bin"}
    return subprocess.run(  # noqa: S603 リポジトリ内のスクリプトを読むだけ
        ["bash", "-c", SHOW],  # noqa: S607
        cwd=ROOT,
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_reads_values_from_ops_env(tmp_path: Path) -> None:
    ops = tmp_path / "ops.env"
    ops.write_text(
        "# コメント\nGCP_PROJECT_ID=member-proj\nREGION=asia-northeast1\nSERVICE=notes\n", encoding="utf-8"
    )
    res = run({"OPS_ENV": str(ops)})
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == (
        "member-proj|asia-northeast1|notes|notes-run@member-proj.iam.gserviceaccount.com|"
        "notes-tasks@member-proj.iam.gserviceaccount.com"
    )


def test_environment_variables_win(tmp_path: Path) -> None:
    ops = tmp_path / "ops.env"
    ops.write_text("GCP_PROJECT_ID=member-proj\n", encoding="utf-8")
    res = run({"OPS_ENV": str(ops), "PROJECT": "other-proj"})
    assert res.stdout.startswith("other-proj|asia-northeast1|meeting-notes|")


def test_stops_without_a_project(tmp_path: Path) -> None:
    """ops.env が無いときに、どこかの既定のプロジェクトに繋がらない。"""
    res = run({"OPS_ENV": str(tmp_path / "missing.env")})
    assert res.returncode != 0
    assert "GCP_PROJECT_ID" in res.stderr and res.stdout == ""


def test_scripts_have_no_hardcoded_project() -> None:
    for script in (ROOT / "scripts").glob("*.sh"):
        text = script.read_text(encoding="utf-8")
        assert "voice-ai-510014" not in text, script.name
