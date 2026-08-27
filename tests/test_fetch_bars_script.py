"""scripts/fetch_bars.py のテスト。

**スクリプトファイルなので `pythonpath` には乗らない。** importlib で
直接読み込む（`tests/test_measure_gap_fade.py` と同じパターン）。

重点は2つ:

1. **`scale_category` が保存と読み込みで往復すること。**
   `save_universe` は書いていたのに `load_universe` が読み落としており、
   キャッシュを経由すると TOPIX100 判定が黙って落ちていた
2. **`load_topix100` が5分足の収集対象に TOPIX100 を足せること。**
   Layer 1 の銘柄群では3手法とも棄却されており、唯一 net 正が出た
   TOPIX100 の5分足が貯まっていなかった（意思決定ログ76）。
   **yfinance は58日しか遡れないので、取り逃した週は永久に失われる**
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

from autotrader.types import Symbol

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fetch_bars.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fetch_bars_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fb() -> ModuleType:
    return _load_script()


@pytest.fixture
def data_root(fb: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`DATA_ROOT` を一時ディレクトリに差し替える。

    `UNIVERSE_PATH` はモジュール読み込み時に確定しているので、両方を差し替える。
    """
    monkeypatch.setattr(fb, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(fb, "UNIVERSE_PATH", tmp_path / "universe.json")
    return tmp_path


def _write_master(path: Path, rows: list[dict[str, str | None]]) -> None:
    path.write_text(
        json.dumps({"as_of": "2024-08-26", "symbols": rows}, ensure_ascii=False),
        encoding="utf-8",
    )


class TestUniverseRoundTrip:
    def test_scale_categoryが往復する(self, fb: ModuleType, data_root: Path) -> None:
        """**書いているのに読んでいなかった。** 呼値の判定が黙って落ちる。"""
        symbols = (
            Symbol(
                code="7203",
                name="トヨタ",
                market="プライム",
                margin_type="貸借",
                sector="輸送用機器",
                scale_category="TOPIX Core30",
            ),
        )
        fb.save_universe(date(2026, 5, 29), symbols)
        restored = fb.load_universe()
        assert restored is not None
        assert restored[0].scale_category == "TOPIX Core30"
        assert restored[0].is_topix100

    def test_scale_categoryが無い古いキャッシュでも読める(
        self, fb: ModuleType, data_root: Path
    ) -> None:
        """**後方互換。** 既存の universe.json には無い。"""
        (data_root / "universe.json").write_text(
            json.dumps(
                {"as_of": "2026-05-29", "symbols": [{"code": "1234", "name": "テスト"}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        restored = fb.load_universe()
        assert restored is not None
        assert restored[0].scale_category is None
        assert not restored[0].is_topix100

    def test_ファイルが無ければNone(self, fb: ModuleType, data_root: Path) -> None:
        assert fb.load_universe() is None


class TestLoadTopix100:
    def test_TOPIX100だけを返す(self, fb: ModuleType, data_root: Path) -> None:
        _write_master(
            data_root / "master_scale_historical.json",
            [
                {"code": "7203", "name": "トヨタ", "scale_category": "TOPIX Core30"},
                {"code": "6501", "name": "日立", "scale_category": "TOPIX Large70"},
                {"code": "1234", "name": "中型", "scale_category": "TOPIX Mid400"},
                {"code": "5678", "name": "区分なし", "scale_category": None},
            ],
        )
        codes = [s.code for s in fb.load_topix100()]
        assert codes == ["7203", "6501"]

    def test_検証期間の開始時点の一覧を優先する(
        self, fb: ModuleType, data_root: Path
    ) -> None:
        """**現在の一覧を過去に適用しない**（docs/03 §4.2・意思決定ログ66）。"""
        _write_master(
            data_root / "master_scale_historical.json",
            [{"code": "7203", "name": "当時", "scale_category": "TOPIX Core30"}],
        )
        _write_master(
            data_root / "master_scale.json",
            [{"code": "9999", "name": "現在", "scale_category": "TOPIX Core30"}],
        )
        assert [s.code for s in fb.load_topix100()] == ["7203"]

    def test_過去の一覧が無ければ現在の一覧を使う(
        self, fb: ModuleType, data_root: Path
    ) -> None:
        """**収集対象を決めるだけなので、ここでは現在の一覧でも害がない。**

        サバイバーシップが問題になるのは過去の成績を測るときであって、
        これから5分足を貯める銘柄を選ぶときではない。
        """
        _write_master(
            data_root / "master_scale.json",
            [{"code": "9999", "name": "現在", "scale_category": "TOPIX Large70"}],
        )
        assert [s.code for s in fb.load_topix100()] == ["9999"]

    def test_一覧が無ければ空(self, fb: ModuleType, data_root: Path) -> None:
        """**空でも落ちない。** 収集自体は Layer 1 だけで続行できる。"""
        assert fb.load_topix100() == ()
