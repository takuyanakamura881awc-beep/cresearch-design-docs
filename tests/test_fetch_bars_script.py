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


class TestAccumulationReport:
    """蓄積状況は**銘柄ごとに数える**。

    和集合で数えると、1銘柄でも80日あれば「80日（227銘柄）」と出てしまい、
    **ほとんど空の銘柄を「揃った」と誤読する**。途中から収集対象に加わった
    銘柄（TOPIX100・意思決定ログ77）があるので、この区別は実際に効く。
    """

    def _store(self, tmp_path: Path, coverage: dict[str, int]) -> object:
        from datetime import datetime

        from autotrader.data.store import BarStore
        from autotrader.types import Bar

        store = BarStore(tmp_path)
        for code, n_days in coverage.items():
            if n_days == 0:
                continue
            bars = tuple(
                Bar(
                    symbol=code,
                    timestamp=datetime(2026, 6, 1 + i, 9, 0),
                    open=1000.0,
                    high=1010.0,
                    low=990.0,
                    close=1005.0,
                    volume=10_000,
                )
                for i in range(n_days)
            )
            store.write(code, "5m", bars)
        return store

    def test_1銘柄だけ揃っていても揃ったと言わない(
        self, fb: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """**これが和集合で数えたときに起きる誤読。**"""
        coverage = {"A": 25, **{f"S{i}": 1 for i in range(60)}}
        store = self._store(tmp_path, coverage)
        symbols = tuple(Symbol(code=c, name=c) for c in coverage)
        monkey = pytest.MonkeyPatch()
        monkey.setattr(fb, "WALKFORWARD_DAYS", 25)
        try:
            fb._print_accumulation(store, symbols)
        finally:
            monkey.undo()
        out = capsys.readouterr().out
        assert "Phase 4 を回せる" not in out
        assert "25日以上ある銘柄: 1/61" in out
        assert "最小: 1日" in out

    def test_全銘柄が揃えば回せると言う(
        self, fb: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        coverage = {f"S{i}": 26 for i in range(55)}
        store = self._store(tmp_path, coverage)
        symbols = tuple(Symbol(code=c, name=c) for c in coverage)
        monkey = pytest.MonkeyPatch()
        monkey.setattr(fb, "WALKFORWARD_DAYS", 25)
        try:
            fb._print_accumulation(store, symbols)
        finally:
            monkey.undo()
        out = capsys.readouterr().out
        assert "Phase 4 を回せる" in out

    def test_日数は足りても銘柄数が監視枠に届かなければ回せない(
        self, fb: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """**日次の監視枠50を埋められないなら、日数だけあっても意味がない。**"""
        coverage = {f"S{i}": 26 for i in range(10)}
        store = self._store(tmp_path, coverage)
        symbols = tuple(Symbol(code=c, name=c) for c in coverage)
        monkey = pytest.MonkeyPatch()
        monkey.setattr(fb, "WALKFORWARD_DAYS", 25)
        try:
            fb._print_accumulation(store, symbols)
        finally:
            monkey.undo()
        out = capsys.readouterr().out
        assert "Phase 4 を回せる" not in out
        assert "監視枠" in out

    def test_5分足が1本も無ければその旨を出す(
        self, fb: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from autotrader.data.store import BarStore

        store = BarStore(tmp_path)
        fb._print_accumulation(store, (Symbol(code="A", name="A"),))
        assert "5分足がまだ無い" in capsys.readouterr().out


class TestLoadCheapUniverse:
    """コストで切り出したユニバース。**中型・小型の高株価帯**（意思決定ログ95）。

    `universe.json`（≤1,250円）でも TOPIX100（大型）でも手法が棄却された。
    地図を作ったところ、**安く取引できる銘柄の大半は真ん中の帯**にあり、
    そこを丸ごと飛ばしていた。
    """

    def test_保存された銘柄を読む(self, fb: ModuleType, data_root: Path) -> None:
        (data_root / "universe_cheap.json").write_text(
            json.dumps(
                {
                    "as_of": "2026-06-12",
                    "symbols": [
                        {"code": "7203", "name": "トヨタ", "scale_category": "TOPIX Core30"},
                        {"code": "1234", "name": "中型", "scale_category": "TOPIX Mid400"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        loaded = fb.load_cheap_universe()
        assert [s.code for s in loaded] == ["7203", "1234"]
        assert loaded[1].scale_category == "TOPIX Mid400"

    def test_一覧が無ければ空(self, fb: ModuleType, data_root: Path) -> None:
        """**空でも落ちない。** 収集自体は既存の銘柄群で続行できる。"""
        assert fb.load_cheap_universe() == ()
