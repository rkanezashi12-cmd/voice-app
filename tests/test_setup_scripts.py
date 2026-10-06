"""Cloud Shell 用スクリプトの中の Python（Cloud Run のクライアント設定を組み立てる部分）が、アプリの読める設定を作ること。"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.clients import ClientRegistry
from tests.conftest import WEBHOOK_SECRET

ROOT = Path(__file__).resolve().parent.parent


def _embedded_python(script: str, start: str) -> str:
    """スクリプトの start の行から始まるヒアドキュメント（<<'PY' … PY）の中身を取り出す。"""
    text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    block = text.split(start, 1)[1]
    return block.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


def _service(clients: dict) -> str:
    env = [{"name": "CLIENTS_CONFIG_JSON", "value": json.dumps(clients)}]
    return json.dumps({"spec": {"template": {"spec": {"containers": [{"env": env}]}}}})


def test_setup_recall_adds_recall_config_that_the_app_accepts() -> None:
    current = json.loads((ROOT / "config" / "clients.example.json").read_text(encoding="utf-8"))
    del current["clients"]["default"]["recall"]
    code = _embedded_python("setup_recall.sh", 'CONFIG_OUT="$(')
    result = subprocess.run(  # noqa: S603 リポジトリ内のスクリプトの一部を実行する
        [sys.executable, "-c", code, "default", "projects/p/secrets", "https://ap-northeast-1.recall.ai"],
        env={**os.environ, "SVC_JSON": _service(current), "BOT_NAME": "議事録 ボット（録音中）"},
        capture_output=True,
        text=True,
        check=True,
    )
    env_value, shown = result.stdout.splitlines()
    assert env_value.isascii(), "環境変数には ASCII だけで渡す"
    assert json.loads(shown) == json.loads(env_value)

    client = ClientRegistry.load(json_text=env_value).get("default")
    recall = client.need_recall()
    assert recall.base_url == "https://ap-northeast-1.recall.ai"
    assert recall.api_key == "sm:projects/p/secrets/recall-api-key/versions/latest"
    assert recall.webhook_secret == "sm:projects/p/secrets/recall-webhook-secret/versions/latest"
    assert recall.bot_name == "議事録 ボット（録音中）"
    assert recall.transcription_mode == "async"
    assert recall.transcript_request == {"provider": {"recallai_async": {"language_code": "ja"}}}
    assert client.zoho is not None, "既存の zoho の設定は残す"


# ---- setup_recall.sh を偽の gcloud で通しで動かす（2回目の実行で保存済みの値を使い回せること） ----

FAKE_GCLOUD = r"""#!/usr/bin/env bash
# 偽の gcloud：呼び出しを記録し、保存済みシークレットは $SIM/store/<名前>、
# 地図の API キーは $SIM/store/apikey-<ID>（中身は作った日時）で表す
printf '%q ' "$@" >> "$SIM/gcloud.log"; echo >> "$SIM/gcloud.log"
fake_key() { echo "AIzaSyFake_${1//-/_}"; }
case "$1 $2" in
  "config set" | "secrets add-iam-policy-binding" | "logging read") exit 0 ;;
  "secrets describe")
    [ "$3" = recording-token-secret ] || [ -f "$SIM/store/$3" ] ;;
  "secrets versions")
    if [ "$3" = access ]; then
      for a in "$@"; do case "$a" in --secret=*) cat "$SIM/store/${a#--secret=}" ;; esac; done
    else
      cat > "$SIM/store/$4"
    fi ;;
  "secrets create") cat > "$SIM/store/$3" ;;
  "run services") [ "$3" != describe ] || cat "$SIM/service.json" ;;
  "artifacts repositories")
    case "$3" in
      # リポジトリがあるときは $SIM/repo に大きさ（バイト）を置く
      describe) [ -f "$SIM/repo" ] && cat "$SIM/repo" ;;
      set-cleanup-policies)
        for a in "$@"; do case "$a" in --policy=*) cp "${a#--policy=}" "$SIM/policy.json" ;; esac; done ;;
      list-cleanup-policies) cat "$SIM/policy.json" ;;
      *) echo "想定外の呼び出し: $*" >&2; exit 2 ;;
    esac ;;
  "services enable") exit 0 ;;
  "services api-keys")
    case "$3" in
      list)
        for f in "$SIM"/store/apikey-*; do
          [ -e "$f" ] || continue
          printf '%s\tprojects/123/locations/global/keys/%s\n' "$(cat "$f")" "${f##*/apikey-}"
        done ;;
      create)
        for a in "$@"; do case "$a" in --key-id=*) id="${a#--key-id=}" ;; esac; done
        date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$SIM/store/apikey-$id"
        # 本物の gcloud と同じく、作った結果（キーの値を含む）を標準エラーに出す
        printf 'Operation [operations/akmf.p7-1] complete. Result: {\n    "keyString":"%s",\n}\n' "$(fake_key "$id")" >&2 ;;
      get-key-string) fake_key "$4" ;;
      delete) rm "$SIM/store/apikey-$4" ;;
      *) echo "想定外の呼び出し: $*" >&2; exit 2 ;;
    esac ;;
  *) echo "想定外の呼び出し: $*" >&2; exit 2 ;;
esac
"""

# recall_check.py check-key だけ偽物にする（キーが good で始まれば使える）。ほかの python3 は本物を使う
FAKE_PYTHON = """#!/usr/bin/env bash
if [ "${1:-}" = scripts/recall_check.py ] && [ "${2:-}" = check-key ]; then
  [[ "$RECALL_API_KEY" == good* ]]
  exit
fi
exec "%s" "$@"
"""


@pytest.fixture
def recall_sim(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "store").mkdir()
    for name, body in {
        "gcloud": FAKE_GCLOUD,
        "python3": FAKE_PYTHON % sys.executable,
        "curl": '#!/usr/bin/env bash\necho \'{"status":"ok"}\'\n',
        "sleep": "#!/usr/bin/env bash\nexit 0\n",
    }.items():
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return tmp_path


def _run_setup_recall(
    sim: Path, answers: list[str], *, bot_name: str | None = None
) -> subprocess.CompletedProcess:
    clients: dict = {"clients": {"default": {"zoho": {"dc": "us"}}}}
    if bot_name:
        clients["clients"]["default"]["recall"] = {"bot_name": bot_name}
    return _run_script(sim, "setup_recall.sh", answers, clients)


def _prepare(sim: Path, clients: dict, extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """偽の Cloud Run のサービスを置き、偽の gcloud を先に見つける環境変数を返す。"""
    env = [
        {"name": "CLIENTS_CONFIG_JSON", "value": json.dumps(clients)},
        {"name": "SERVICE_URL", "value": "https://svc.example.run.app"},
        *({"name": k, "value": v} for k, v in (extra_env or {}).items()),
    ]
    service = {"spec": {"template": {"spec": {"containers": [{"env": env}]}}}, "status": {"url": "https://x"}}
    (sim / "service.json").write_text(json.dumps(service), encoding="utf-8")
    (sim / "gcloud.log").write_text("", encoding="utf-8")
    return {**os.environ, "PATH": f"{sim / 'bin'}:{os.environ['PATH']}", "SIM": str(sim)}


def _run_script(
    sim: Path,
    script: str,
    answers: list[str],
    clients: dict,
    *,
    extra_env: dict[str, str] | None = None,
    args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 リポジトリ内のスクリプトを偽の gcloud で実行する
        ["bash", str(ROOT / "scripts" / script), *args],  # noqa: S607
        input="".join(f"{a}\n" for a in answers),
        env=_prepare(sim, clients, extra_env),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _start_script(sim: Path, script: str, clients: dict) -> subprocess.Popen[str]:
    """入力を渡さずに動かし始める（途中で Ctrl+C を送るテスト用）。"""
    return subprocess.Popen(  # noqa: S603 リポジトリ内のスクリプトを偽の gcloud で実行する
        ["bash", str(ROOT / "scripts" / script)],  # noqa: S607
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_prepare(sim, clients),
    )


def _gcloud_calls(sim: Path) -> list[list[str]]:
    lines = (sim / "gcloud.log").read_text(encoding="utf-8").splitlines()
    return [shlex.split(line) for line in lines if line.strip()]


def _saved(sim: Path) -> list[str]:
    """値を書き込んだシークレット（versions add / create）の名前。"""
    calls = _gcloud_calls(sim)
    added = [c[3] for c in calls if c[:3] == ["secrets", "versions", "add"]]
    return added + [c[2] for c in calls if c[:2] == ["secrets", "create"]]


def _deployed_config(sim: Path) -> dict:
    update = next(c for c in _gcloud_calls(sim) if c[:3] == ["run", "services", "update"])
    arg = next(a for a in update if a.startswith("--update-env-vars="))
    return json.loads(arg.split("CLIENTS_CONFIG_JSON=", 1)[1].split("@@", 1)[0])


def _deployed_bot_name(sim: Path) -> str:
    return _deployed_config(sim)["clients"]["default"]["recall"]["bot_name"]


def test_setup_recall_first_run_saves_both_secrets(recall_sim: Path) -> None:
    res = _run_setup_recall(
        recall_sim, ["goodkey", WEBHOOK_SECRET, "マルサン木型 議事録（録音中）", "yes", ""]
    )
    assert res.returncode == 0, res.stderr
    assert sorted(_saved(recall_sim)) == ["recall-api-key", "recall-webhook-secret"]
    assert (recall_sim / "store" / "recall-api-key").read_text() == "goodkey"
    assert _deployed_bot_name(recall_sim) == "マルサン木型 議事録（録音中）"
    assert "手順2" in res.stdout, "次は CRM の関数とワークフロー（docs/recall-bot.md の手順2）"


def test_setup_recall_rerun_reuses_stored_values_with_enter(recall_sim: Path) -> None:
    """2回目は Enter だけで保存済みの API キー・シークレット・表示名を使い、シークレットを保存し直さない。"""
    (recall_sim / "store" / "recall-api-key").write_text("goodstored")
    (recall_sim / "store" / "recall-webhook-secret").write_text(WEBHOOK_SECRET)
    res = _run_setup_recall(recall_sim, ["", "", "", "yes", ""], bot_name="マルサン木型 議事録（録音中）")
    assert res.returncode == 0, res.stderr
    assert _saved(recall_sim) == []
    assert _deployed_bot_name(recall_sim) == "マルサン木型 議事録（録音中）"


def test_setup_recall_saves_new_key_after_stored_key_fails(recall_sim: Path) -> None:
    (recall_sim / "store" / "recall-api-key").write_text("revoked")
    (recall_sim / "store" / "recall-webhook-secret").write_text(WEBHOOK_SECRET)
    res = _run_setup_recall(recall_sim, ["", "goodnew", "", "", "yes", ""], bot_name="議事録ボット（録音中）")
    assert res.returncode == 0, res.stderr
    assert _saved(recall_sim) == ["recall-api-key"]
    assert (recall_sim / "store" / "recall-api-key").read_text() == "goodnew"


def test_setup_recall_saves_nothing_when_cancelled(recall_sim: Path) -> None:
    res = _run_setup_recall(recall_sim, ["goodkey", WEBHOOK_SECRET, "", "no"])
    assert res.returncode == 1
    assert _saved(recall_sim) == []
    assert not any(c[:3] == ["run", "services", "update"] for c in _gcloud_calls(recall_sim))


def test_setup_recall_bot_name_prompt_allows_line_editing() -> None:
    """表示名の入力で矢印キーが使えること（read -e）と、Ctrl+Z でスクリプトが止まらないこと（trap '' TSTP）。"""
    text = (ROOT / "scripts" / "setup_recall.sh").read_text(encoding="utf-8")
    assert "read -erp" in text
    assert "trap '' TSTP" in text


# ---- setup_app_login.sh（録音アプリのログインと GPS の候補） ----

OLD_KEY_CREATED = "2026-01-01T00:00:00.000000Z"


def fake_maps_key(key_id: str) -> str:
    """偽の gcloud が返す地図の API キーの値（本物と同じく AIza で始まる）。"""
    return "AIzaSyFake_" + key_id.replace("-", "_")


def _maps_keys(sim: Path) -> list[str]:
    """残っている（削除していない）地図の API キーの ID。"""
    return sorted(p.name.removeprefix("apikey-") for p in (sim / "store").glob("apikey-*"))


def _stored_app_login(sim: Path, *, maps_key: str | None) -> dict:
    """2回目の実行の前提：クライアント・署名鍵（と地図のキー）は保存済み。"""
    values = {
        "app-login-client-id": "1000.LOGINID",
        "app-login-client-secret": "login-secret",
        "app-session-secret": "s" * 64,
    }
    if maps_key is not None:
        values["app-maps-api-key"] = maps_key
    for name, value in values.items():
        (sim / "store" / name).write_text(value)
    return {"clients": {"default": {"zoho": {"dc": "us"}, "app": {}}}}


def test_setup_app_login_adds_app_config_that_the_app_accepts() -> None:
    current = json.loads((ROOT / "config" / "clients.example.json").read_text(encoding="utf-8"))
    code = _embedded_python("setup_app_login.sh", 'CONFIG_OUT="$(')
    result = subprocess.run(  # noqa: S603 リポジトリ内のスクリプトの一部を実行する
        [sys.executable, "-c", code, "default", "projects/p/secrets"],
        env={**os.environ, "SVC_JSON": _service(current), "USE_MAPS": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    env_value, shown = result.stdout.splitlines()
    assert env_value.isascii()
    assert json.loads(shown) == json.loads(env_value)
    client = ClientRegistry.load(json_text=env_value).get("default")
    app = client.need_app()
    assert app.login_client_id == "sm:projects/p/secrets/app-login-client-id/versions/latest"
    assert app.login_client_secret == "sm:projects/p/secrets/app-login-client-secret/versions/latest"
    assert app.session_secret == "sm:projects/p/secrets/app-session-secret/versions/latest"
    assert app.maps_api_key == "sm:projects/p/secrets/app-maps-api-key/versions/latest"
    assert client.recall is not None and client.zoho is not None, "ほかの設定は残す"


def test_setup_app_login_first_run_with_gps(recall_sim: Path) -> None:
    clients = {"clients": {"default": {"zoho": {"dc": "us"}}}}
    res = _run_script(
        recall_sim, "setup_app_login.sh", ["1000.LOGINID", "login-secret", "yes", "yes"], clients
    )
    assert res.returncode == 0, res.stderr
    assert sorted(_saved(recall_sim)) == [
        "app-login-client-id",
        "app-login-client-secret",
        "app-maps-api-key",
        "app-session-secret",
    ]
    store = recall_sim / "store"
    (key_id,) = _maps_keys(recall_sim)
    assert re.fullmatch(r"app-geocoding-\d{14}", key_id), "キーの ID には作った日時を付ける"
    assert (store / "app-maps-api-key").read_text() == fake_maps_key(key_id)
    assert len((store / "app-session-secret").read_text()) >= 40
    assert "https://svc.example.run.app/auth/callback" in res.stdout, "API コンソールに登録する URL を出す"
    assert "AIza" not in res.stdout + res.stderr, "API キーは画面に出さない（gcloud が出す作成結果も捨てる）"
    app = _deployed_config(recall_sim)["clients"]["default"]["app"]
    assert app["maps_api_key"].endswith("/app-maps-api-key/versions/latest")
    assert "https://svc.example.run.app/app/" in res.stdout


def test_setup_app_login_rerun_keeps_everything(recall_sim: Path) -> None:
    store = recall_sim / "store"
    for name, value in {
        "app-login-client-id": "1000.LOGINID",
        "app-login-client-secret": "login-secret",
        "app-session-secret": "s" * 64,
        "app-maps-api-key": "AIzaOld",
    }.items():
        (store / name).write_text(value)
    clients = {"clients": {"default": {"zoho": {"dc": "us"}, "app": {"session_hours": 8}}}}
    res = _run_script(recall_sim, "setup_app_login.sh", ["", "", "no", "yes"], clients)
    assert res.returncode == 0, res.stderr
    assert _saved(recall_sim) == [], "保存済みの値は保存し直さない（署名鍵も作り直さない）"
    app = _deployed_config(recall_sim)["clients"]["default"]["app"]
    assert app["session_hours"] == 8, "ほかの設定は残す"
    assert "maps_api_key" in app, "保存済みの地図のキーは使い続ける"


def test_setup_app_login_needs_zoho_connection(recall_sim: Path) -> None:
    res = _run_script(recall_sim, "setup_app_login.sh", [], {"clients": {"default": {}}})
    assert res.returncode == 1
    assert "zoho がありません" in res.stderr


def test_setup_app_login_rotates_maps_key_after_switching(recall_sim: Path) -> None:
    """画面に出てしまったキーは作り直せる。新しいキーを保存して Cloud Run を切り替えてから、古いキーを削除する。"""
    clients = _stored_app_login(recall_sim, maps_key=fake_maps_key("app-geocoding"))
    (recall_sim / "store" / "apikey-app-geocoding").write_text(OLD_KEY_CREATED)
    res = _run_script(recall_sim, "setup_app_login.sh", ["", "", "yes", "yes", "yes"], clients)
    assert res.returncode == 0, res.stderr
    (new_id,) = _maps_keys(recall_sim)
    assert new_id.startswith("app-geocoding-"), "古いキーは削除し、新しいキーだけが残る"
    assert _saved(recall_sim) == ["app-maps-api-key"], "ログインの設定は保存し直さない"
    assert (recall_sim / "store" / "app-maps-api-key").read_text() == fake_maps_key(new_id)
    calls = _gcloud_calls(recall_sim)
    update = next(i for i, c in enumerate(calls) if c[:3] == ["run", "services", "update"])
    delete = next(i for i, c in enumerate(calls) if c[:3] == ["services", "api-keys", "delete"])
    assert calls[delete][3] == "app-geocoding"
    assert update < delete, "Cloud Run が新しいキーに切り替わってから、古いキーを削除する"
    assert "古い API キー app-geocoding を削除しました" in res.stdout
    assert "AIza" not in res.stdout + res.stderr, "新しいキーも古いキーも画面に出さない"


def test_setup_app_login_keeps_maps_key_without_saving_again(recall_sim: Path) -> None:
    clients = _stored_app_login(recall_sim, maps_key=fake_maps_key("app-geocoding"))
    (recall_sim / "store" / "apikey-app-geocoding").write_text(OLD_KEY_CREATED)
    res = _run_script(recall_sim, "setup_app_login.sh", ["", "", "yes", "no", "yes"], clients)
    assert res.returncode == 0, res.stderr
    assert _maps_keys(recall_sim) == ["app-geocoding"]
    assert _saved(recall_sim) == [], "保存済みと同じキーは保存し直さない"
    calls = _gcloud_calls(recall_sim)
    assert not any(
        c[:3] in (["services", "api-keys", "create"], ["services", "api-keys", "delete"]) for c in calls
    )
    assert _deployed_config(recall_sim)["clients"]["default"]["app"]["maps_api_key"].endswith(
        "/app-maps-api-key/versions/latest"
    )


def test_setup_app_login_saves_existing_key_that_was_not_saved(recall_sim: Path) -> None:
    """キーはあるのに保存されていない場合（前の版のスクリプトで手順4をやめたなど）：作り直さずにそのキーを保存する。"""
    clients = _stored_app_login(recall_sim, maps_key=None)
    (recall_sim / "store" / "apikey-app-geocoding").write_text(OLD_KEY_CREATED)
    res = _run_script(recall_sim, "setup_app_login.sh", ["", "", "yes", "no", "yes"], clients)
    assert res.returncode == 0, res.stderr
    assert _saved(recall_sim) == ["app-maps-api-key"]
    assert (recall_sim / "store" / "app-maps-api-key").read_text() == fake_maps_key("app-geocoding")


def test_setup_app_login_cancel_creates_no_key(recall_sim: Path) -> None:
    """手順4で no と答えたら、API キーも作らない（キーを作るのは yes のあと）。"""
    answers = ["1000.LOGINID", "login-secret", "yes", "no"]
    res = _run_script(
        recall_sim, "setup_app_login.sh", answers, {"clients": {"default": {"zoho": {"dc": "us"}}}}
    )
    assert res.returncode == 1
    assert "API キーも作っていません" in res.stderr
    assert _maps_keys(recall_sim) == []
    assert _saved(recall_sim) == []
    assert not any(c[:3] == ["run", "services", "update"] for c in _gcloud_calls(recall_sim))


def test_setup_app_login_explains_wrong_pastes_without_showing_them(recall_sim: Path) -> None:
    """コピーし直さずに貼った（コマンドが入っていた）、Client Secret と Client ID を取り違えた、を知らせる。値は出さない。"""
    secret = "0123456789abcdef" * 2 + "0123456789"
    answers = [
        "cd ~/voice-app && bash scripts/setup_app_login.sh",
        secret,
        "1000.LOGINID",
        "1000.LOGINID",
        secret,
        "no",
        "yes",
    ]
    res = _run_script(
        recall_sim, "setup_app_login.sh", answers, {"clients": {"default": {"zoho": {"dc": "us"}}}}
    )
    assert res.returncode == 0, res.stderr
    assert "コマンドなど" in res.stdout
    assert "Client Secret が貼られたようです" in res.stdout
    assert "Client ID がもう一度貼られたようです" in res.stdout
    assert secret not in res.stdout + res.stderr
    store = recall_sim / "store"
    assert (store / "app-login-client-id").read_text() == "1000.LOGINID"
    assert (store / "app-login-client-secret").read_text() == secret


def test_setup_app_login_strips_bracketed_paste_markers(recall_sim: Path) -> None:
    answers = ["\x1b[200~1000.LOGINID\x1b[201~", "\x1b[200~login-secret\x1b[201~", "no", "yes"]
    res = _run_script(
        recall_sim, "setup_app_login.sh", answers, {"clients": {"default": {"zoho": {"dc": "us"}}}}
    )
    assert res.returncode == 0, res.stderr
    store = recall_sim / "store"
    assert (store / "app-login-client-id").read_text() == "1000.LOGINID"
    assert (store / "app-login-client-secret").read_text() == "login-secret"


def test_setup_app_login_ctrl_c_says_nothing_was_saved(recall_sim: Path) -> None:
    """Ctrl+C（コピーのつもりで押しがち）で止まったら、何も保存していないことと、続けて貼らないことを伝える。"""
    proc = _start_script(recall_sim, "setup_app_login.sh", {"clients": {"default": {"zoho": {"dc": "us"}}}})
    log = recall_sim / "gcloud.log"
    deadline = time.monotonic() + 30
    while "app-login-client-secret" not in log.read_text(encoding="utf-8"):
        assert time.monotonic() < deadline, "Client ID を聞くところまで進まない"
        time.sleep(0.05)
    time.sleep(0.3)  # Client ID の入力を待つところまで進める
    proc.send_signal(signal.SIGINT)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 130
    assert "何も保存していません" in err
    assert "Client ID や Client Secret を貼らないでください" in err
    assert _saved(recall_sim) == []


# ---- go_live.sh（本番運用への切り替え：商談記録の名前に【TEST】を付けない） ----

LIVE_CLIENTS = {"clients": {"default": {"zoho": {"dc": "us"}}}}


def _env_updates(sim: Path) -> list[str]:
    return [
        a
        for c in _gcloud_calls(sim)
        if c[:3] == ["run", "services", "update"]
        for a in c
        if a.startswith("--update-env-vars=")
    ]


def test_go_live_turns_off_test_names_only(recall_sim: Path) -> None:
    env = {"DRY_RUN": "false", "CRM_TEST_RECORDS": "true"}
    res = _run_script(recall_sim, "go_live.sh", ["yes"], LIVE_CLIENTS, extra_env=env)
    assert res.returncode == 0, res.stderr
    assert _env_updates(recall_sim) == ["--update-env-vars=CRM_TEST_RECORDS=false"], (
        "変えるのは CRM_TEST_RECORDS だけ"
    )
    assert "テストの記録" in res.stdout, "先に CRM の画面でテストの記録を消すよう案内する"
    assert "本番運用（商談記録の名前に【TEST】を付けない）に切り替えました" in res.stdout


def test_go_live_cancel_changes_nothing(recall_sim: Path) -> None:
    env = {"DRY_RUN": "false", "CRM_TEST_RECORDS": "true"}
    res = _run_script(recall_sim, "go_live.sh", ["no"], LIVE_CLIENTS, extra_env=env)
    assert res.returncode == 1
    assert "何も変えていません" in res.stderr
    assert _env_updates(recall_sim) == []


def test_go_live_when_already_live_changes_nothing(recall_sim: Path) -> None:
    env = {"DRY_RUN": "false", "CRM_TEST_RECORDS": "false"}
    res = _run_script(recall_sim, "go_live.sh", [], LIVE_CLIENTS, extra_env=env)
    assert res.returncode == 0, res.stderr
    assert "すでに本番運用" in res.stdout
    assert _env_updates(recall_sim) == []


def test_go_live_can_go_back_to_test_names(recall_sim: Path) -> None:
    env = {"DRY_RUN": "false", "CRM_TEST_RECORDS": "false"}
    res = _run_script(recall_sim, "go_live.sh", ["yes"], LIVE_CLIENTS, extra_env=env, args=("--test",))
    assert res.returncode == 0, res.stderr
    assert _env_updates(recall_sim) == ["--update-env-vars=CRM_TEST_RECORDS=true"]


def test_go_live_warns_when_dry_run_is_still_on(recall_sim: Path) -> None:
    """DRY_RUN が無い（アプリの既定は true）ときは、CRM に書き込まないままだと知らせる。DRY_RUN は変えない。"""
    res = _run_script(recall_sim, "go_live.sh", ["yes"], LIVE_CLIENTS)
    assert res.returncode == 0, res.stderr
    assert "DRY_RUN=true のままです" in res.stdout
    assert _env_updates(recall_sim) == ["--update-env-vars=CRM_TEST_RECORDS=false"]


def test_go_live_rejects_unknown_arguments(recall_sim: Path) -> None:
    res = _run_script(recall_sim, "go_live.sh", [], LIVE_CLIENTS, args=("--prod",))
    assert res.returncode == 1
    assert "使い方" in res.stderr
    assert _env_updates(recall_sim) == []


# ---- setup_artifact_cleanup.sh：プログラムの古い版を自動で消す設定 ----


def _cleanup_calls(sim: Path) -> list[list[str]]:
    return [c for c in _gcloud_calls(sim) if c[:3] == ["artifacts", "repositories", "set-cleanup-policies"]]


def test_artifact_cleanup_keeps_recent_versions_and_deletes_old_ones(recall_sim: Path) -> None:
    (recall_sim / "repo").write_text("367001600", encoding="utf-8")
    res = _run_script(recall_sim, "setup_artifact_cleanup.sh", ["yes"], LIVE_CLIENTS)
    assert res.returncode == 0, res.stderr
    assert "約 350 MB" in res.stdout, "今の大きさを出す"
    policies = json.loads((recall_sim / "policy.json").read_text(encoding="utf-8"))
    assert policies == [
        {"name": "keep-recent", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 5}},
        {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"olderThan": "604800s"}},
    ], "新しい5版は残し（Keep が優先）、それより古く7日を過ぎた版を消す"
    (call,) = _cleanup_calls(recall_sim)
    assert call[3] == "cloud-run-source-deploy", "deploy.sh（--source）が作るリポジトリ"
    assert "--location=asia-northeast1" in call
    assert "--no-dry-run" in call, "試しではなく本当に消す設定にする"


def test_artifact_cleanup_cancel_changes_nothing(recall_sim: Path) -> None:
    (recall_sim / "repo").write_text("367001600", encoding="utf-8")
    res = _run_script(recall_sim, "setup_artifact_cleanup.sh", ["no"], LIVE_CLIENTS)
    assert res.returncode == 1
    assert "何も変えていません" in res.stderr
    assert _cleanup_calls(recall_sim) == []


def test_artifact_cleanup_needs_the_repository(recall_sim: Path) -> None:
    res = _run_script(recall_sim, "setup_artifact_cleanup.sh", ["yes"], LIVE_CLIENTS)
    assert res.returncode == 1
    assert "見つかりません" in res.stderr
    assert _cleanup_calls(recall_sim) == []


def test_artifact_cleanup_keep_count_can_be_changed(
    recall_sim: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (recall_sim / "repo").write_text("1", encoding="utf-8")
    monkeypatch.setenv("KEEP_VERSIONS", "3")
    res = _run_script(recall_sim, "setup_artifact_cleanup.sh", ["yes"], LIVE_CLIENTS)
    assert res.returncode == 0, res.stderr
    assert json.loads((recall_sim / "policy.json").read_text(encoding="utf-8"))[0]["mostRecentVersions"] == {
        "keepCount": 3
    }
    monkeypatch.setenv("KEEP_VERSIONS", "0")
    res = _run_script(recall_sim, "setup_artifact_cleanup.sh", ["yes"], LIVE_CLIENTS)
    assert res.returncode == 1
    assert "KEEP_VERSIONS" in res.stderr
