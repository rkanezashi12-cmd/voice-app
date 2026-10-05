"""商談日報ウィジェットの ZIP（scripts/build_widget.py）：項目の API 名は field_map から、ZIP の形は Zoho が受け付ける形。"""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

from app.field_map import DEFAULT_FIELD_MAP

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "build_widget.py"
_spec = importlib.util.spec_from_file_location("build_widget", _PATH)
assert _spec and _spec.loader
bw = importlib.util.module_from_spec(_spec)
sys.modules["build_widget"] = bw
_spec.loader.exec_module(bw)

FAKE_SDK = b"/* fake */ var ZOHO = {};" + b" " * 2000


def test_config_uses_the_field_map_names() -> None:
    """標準ライブラリだけで読んだ既定値が、アプリが使う field_map と同じ（ウィジェットに API 名を直書きしない）。"""
    config = bw.widget_config()
    mr = DEFAULT_FIELD_MAP.meeting_record
    assert config["module"] == mr.module
    assert config["accounts_module"] == DEFAULT_FIELD_MAP.standard.accounts_module
    assert config["fields"] == {k: getattr(mr, k) for k in bw.WIDGET_FIELDS}
    assert config["status"] == {k: getattr(DEFAULT_FIELD_MAP.status, k) for k in bw.WIDGET_STATUS}


def test_zip_has_the_shape_zoho_accepts(tmp_path: Path) -> None:
    out = bw.build(tmp_path / "w.zip", FAKE_SDK)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert names[0] == "plugin-manifest.json", "ルート直下に plugin-manifest.json"
        assert "app/" in names and z.getinfo("app/").is_dir(), "ディレクトリの項目を入れる"
        manifest = json.loads(z.read("plugin-manifest.json"))
        url = manifest["modules"]["widgets"][0]["url"]
        assert url.lstrip("/") in names, "manifest の画面が ZIP にある"
        html = z.read("app/index.html").decode()
        for script in ["ZohoEmbededAppSDK.min.js", "field-map.js", "report.js", "main.js"]:
            assert f'src="{script}"' in html
            assert f"app/{script}" in names
        assert z.read("app/ZohoEmbededAppSDK.min.js") == FAKE_SDK
        js = z.read("app/field-map.js").decode()
        config = json.loads(js.split("window.MEETING_REPORT_CONFIG = ", 1)[1].rstrip().rstrip(";"))
        assert config == bw.widget_config()
        assert "live.zwidgets.com" not in html, "SDK は CDN から読まずに同梱する"


def test_sdk_that_is_not_the_zoho_sdk_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(bw.BuildError):
        bw.check_sdk(b"<html>blocked</html>", "x")
    bad = tmp_path / "sdk.js"
    bad.write_text("not a sdk")
    assert bw.main(["--sdk", str(bad), "--output", str(tmp_path / "w.zip")]) == 1
    assert not (tmp_path / "w.zip").exists()


def test_main_builds_with_a_local_sdk(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sdk = tmp_path / "sdk.js"
    sdk.write_bytes(FAKE_SDK)
    assert bw.main(["--sdk", str(sdk), "--output", str(tmp_path / "w.zip")]) == 0
    assert (tmp_path / "w.zip").exists()
    out = capsys.readouterr().out
    assert "docs/widget.md" in out
    assert "/index.html" in out, "インデックスページ（app/ からの相対）を案内する"
