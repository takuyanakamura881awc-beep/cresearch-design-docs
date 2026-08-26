"""scripts/measure_gap_fade.py のテスト。

**スクリプトファイルなので `pythonpath` には乗らない。** importlib で
直接読み込む（`tests/test_backtest_take_script.py` と同じパターン）。

重点は3つ:

1. `gap_pct` / `intraday_return_pct` が正しい式で計算されること
2. 銘柄の初日（前日終値がない）を除外すること
3. `fade_score` の符号がフェード方向で正、ギャップ&ゴー方向で負になること
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path
from types import ModuleType

import pytest

from autotrader.types import Bar

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "measure_gap_fade.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("measure_gap_fade_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gf() -> ModuleType:
    return _load_script()


def _daily_bar(symbol: str, day: date, *, open_: float, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=datetime(day.year, day.month, day.day, 0, 0),
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=10_000,
    )


DAY1 = date(2026, 6, 1)
DAY2 = date(2026, 6, 2)
DAY3 = date(2026, 6, 3)


class TestGapFadePairs:
    def test_gap_pctとintraday_return_pctを正しく計算する(self, gf: ModuleType) -> None:
        daily_bars = {
            "A": (
                _daily_bar("A", DAY1, open_=1000.0, close=1000.0),
                # 前日終値1000から1050で寄り付き（gap +5%）、990で引け
                _daily_bar("A", DAY2, open_=1050.0, close=990.0),
            ),
        }
        pairs = gf.gap_fade_pairs(daily_bars)
        assert len(pairs) == 1
        pair = pairs[0]
        assert pair.symbol == "A"
        assert pair.gap_pct == pytest.approx((1050.0 - 1000.0) / 1000.0)
        assert pair.intraday_return_pct == pytest.approx((990.0 - 1050.0) / 1050.0)

    def test_銘柄の初日は除外する(self, gf: ModuleType) -> None:
        """前日終値がない最初の日はギャップを定義できない。"""
        daily_bars = {"A": (_daily_bar("A", DAY1, open_=1000.0, close=1010.0),)}
        assert gf.gap_fade_pairs(daily_bars) == ()

    def test_前日終値が0以下の日は除外する(self, gf: ModuleType) -> None:
        daily_bars = {
            "A": (
                _daily_bar("A", DAY1, open_=1000.0, close=0.0),
                _daily_bar("A", DAY2, open_=1000.0, close=1000.0),
            ),
        }
        assert gf.gap_fade_pairs(daily_bars) == ()

    def test_当日始値が0以下の日は除外する(self, gf: ModuleType) -> None:
        daily_bars = {
            "A": (
                _daily_bar("A", DAY1, open_=1000.0, close=1000.0),
                _daily_bar("A", DAY2, open_=0.0, close=1000.0),
            ),
        }
        assert gf.gap_fade_pairs(daily_bars) == ()

    def test_複数銘柄複数日で銘柄ごとに独立して計算する(self, gf: ModuleType) -> None:
        daily_bars = {
            "A": (
                _daily_bar("A", DAY1, open_=1000.0, close=1000.0),
                _daily_bar("A", DAY2, open_=1010.0, close=1005.0),
                _daily_bar("A", DAY3, open_=1020.0, close=1015.0),
            ),
            "B": (
                _daily_bar("B", DAY1, open_=500.0, close=500.0),
                _daily_bar("B", DAY2, open_=490.0, close=495.0),
            ),
        }
        pairs = gf.gap_fade_pairs(daily_bars)
        # Aは2日ぶん（DAY2, DAY3）、Bは1日ぶん（DAY2）
        assert sum(1 for p in pairs if p.symbol == "A") == 2
        assert sum(1 for p in pairs if p.symbol == "B") == 1

    def test_バーの並び順に依存しない(self, gf: ModuleType) -> None:
        """`BarStore.read` の返す順が保証されなくても、時刻で並べ替えて計算する。"""
        daily_bars = {
            "A": (
                _daily_bar("A", DAY2, open_=1050.0, close=990.0),
                _daily_bar("A", DAY1, open_=1000.0, close=1000.0),
            ),
        }
        pairs = gf.gap_fade_pairs(daily_bars)
        assert len(pairs) == 1
        assert pairs[0].gap_pct == pytest.approx(0.05)


class TestFadeScore:
    def test_ギャップアップして戻れば正(self, gf: ModuleType) -> None:
        """フェード（ギャップ方向と逆に動いた）は正のスコア。"""
        pair = gf.GapFadePair(symbol="A", gap_pct=0.02, intraday_return_pct=-0.01)
        assert gf.fade_score(pair) == pytest.approx(0.01)

    def test_ギャップアップしてさらに伸びれば負(self, gf: ModuleType) -> None:
        """ギャップ&ゴー（ギャップ方向にさらに伸びた）は負のスコア。"""
        pair = gf.GapFadePair(symbol="A", gap_pct=0.02, intraday_return_pct=0.01)
        assert gf.fade_score(pair) == pytest.approx(-0.01)

    def test_ギャップダウンして戻れば正(self, gf: ModuleType) -> None:
        pair = gf.GapFadePair(symbol="A", gap_pct=-0.02, intraday_return_pct=0.01)
        assert gf.fade_score(pair) == pytest.approx(0.01)

    def test_ギャップダウンしてさらに下げれば負(self, gf: ModuleType) -> None:
        pair = gf.GapFadePair(symbol="A", gap_pct=-0.02, intraday_return_pct=-0.01)
        assert gf.fade_score(pair) == pytest.approx(-0.01)

    def test_ギャップがゼロなら符号を持たずゼロ(self, gf: ModuleType) -> None:
        pair = gf.GapFadePair(symbol="A", gap_pct=0.0, intraday_return_pct=0.01)
        assert gf.fade_score(pair) == 0.0
