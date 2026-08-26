"""scripts/measure_gap_fade.py のテスト。

**スクリプトファイルなので `pythonpath` には乗らない。** importlib で
直接読み込む（`tests/test_backtest_take_script.py` と同じパターン）。

重点は5つ:

1. `gap_pct` / `intraday_return_pct` が正しい式で計算されること
2. 銘柄の初日（前日終値がない）を除外すること
3. `fade_score` の符号がフェード方向で正、ギャップ&ゴー方向で負になること
4. **往復コストが `autotrader.tick` と同じ値になること**（診断ごとに
   コストモデルを作り直していないことの確認）
5. **`net_bps` が gross からコストを引いた値であること**——ここを
   取り違えると「コスト後に残る」という誤った結論を出しかねない
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path
from types import ModuleType

import pytest

from autotrader.tick import spread_yen
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
        pair = gf.GapFadePair(
            open_price=1000.0, symbol="A", gap_pct=0.02, intraday_return_pct=-0.01
        )
        assert gf.fade_score(pair) == pytest.approx(0.01)

    def test_ギャップアップしてさらに伸びれば負(self, gf: ModuleType) -> None:
        """ギャップ&ゴー（ギャップ方向にさらに伸びた）は負のスコア。"""
        pair = gf.GapFadePair(open_price=1000.0, symbol="A", gap_pct=0.02, intraday_return_pct=0.01)
        assert gf.fade_score(pair) == pytest.approx(-0.01)

    def test_ギャップダウンして戻れば正(self, gf: ModuleType) -> None:
        pair = gf.GapFadePair(
            open_price=1000.0, symbol="A", gap_pct=-0.02, intraday_return_pct=0.01
        )
        assert gf.fade_score(pair) == pytest.approx(0.01)

    def test_ギャップダウンしてさらに下げれば負(self, gf: ModuleType) -> None:
        pair = gf.GapFadePair(
            open_price=1000.0, symbol="A", gap_pct=-0.02, intraday_return_pct=-0.01
        )
        assert gf.fade_score(pair) == pytest.approx(-0.01)

    def test_ギャップがゼロなら符号を持たずゼロ(self, gf: ModuleType) -> None:
        pair = gf.GapFadePair(open_price=1000.0, symbol="A", gap_pct=0.0, intraday_return_pct=0.01)
        assert gf.fade_score(pair) == 0.0


class TestOpenPrice:
    def test_当日始値を保持する(self, gf: ModuleType) -> None:
        """コストは株価で決まるので、始値を持っていないと見積れない。"""
        daily_bars = {
            "A": (
                _daily_bar("A", DAY1, open_=1000.0, close=1000.0),
                _daily_bar("A", DAY2, open_=1050.0, close=990.0),
            ),
        }
        pairs = gf.gap_fade_pairs(daily_bars)
        assert pairs[0].open_price == pytest.approx(1050.0)


class TestRoundTripCostBps:
    def test_tickモジュールと同じ値になる(self, gf: ModuleType) -> None:
        """**コストモデルを診断ごとに作り直していない**ことの確認。"""
        price = 1000.0
        pair = gf.GapFadePair(
            open_price=price, symbol="A", gap_pct=0.02, intraday_return_pct=-0.01
        )
        expected = float(spread_yen(price)) / price * 10_000.0
        assert gf.round_trip_cost_bps(pair) == pytest.approx(expected)

    def test_安い株ほど往復コストが高い(self, gf: ModuleType) -> None:
        """呼値は絶対額なので、株価が低いほど比率としては重くなる。"""
        cheap = gf.GapFadePair(
            open_price=500.0, symbol="A", gap_pct=0.02, intraday_return_pct=-0.01
        )
        pricey = gf.GapFadePair(
            open_price=2500.0, symbol="B", gap_pct=0.02, intraday_return_pct=-0.01
        )
        assert gf.round_trip_cost_bps(cheap) > gf.round_trip_cost_bps(pricey)


class TestBucketStats:
    def _pairs(self, gf: ModuleType) -> tuple[object, ...]:
        # |gap| が 1%/2%/3% の3件。intraday はすべてギャップと逆方向1%（フェード）
        return tuple(
            gf.GapFadePair(
                open_price=1000.0,
                symbol=f"S{i}",
                gap_pct=gap,
                intraday_return_pct=-0.01,
            )
            for i, gap in enumerate((0.01, 0.02, 0.03))
        )

    def test_閾値で絞り込む(self, gf: ModuleType) -> None:
        pairs = self._pairs(gf)
        assert gf.bucket_stats(pairs, 0.0).n == 3
        assert gf.bucket_stats(pairs, 0.015).n == 2

    def test_該当が2件未満ならNone(self, gf: ModuleType) -> None:
        """標準偏差が計算できないので、無理に数字を出さない。"""
        pairs = self._pairs(gf)
        assert gf.bucket_stats(pairs, 0.025) is None
        assert gf.bucket_stats(pairs, 0.99) is None

    def test_gross_bpsはfade_scoreの平均をbpsにしたもの(self, gf: ModuleType) -> None:
        pairs = self._pairs(gf)
        stats = gf.bucket_stats(pairs, 0.0)
        # 全件が「ギャップと逆に1%」＝ fade_score +0.01 = +100bps
        assert stats.gross_bps == pytest.approx(100.0)

    def test_netはgrossからコストを引いた値(self, gf: ModuleType) -> None:
        """**取り違えると「コスト後に残る」という誤った結論になる。**"""
        pairs = self._pairs(gf)
        stats = gf.bucket_stats(pairs, 0.0)
        assert stats.net_bps == pytest.approx(stats.gross_bps - stats.cost_bps)
        assert stats.cost_bps > 0

    def test_ばらつきがなければt値は無限大にせずstderrゼロで0を返す(
        self, gf: ModuleType
    ) -> None:
        """全件が同じ値だと標準誤差が0になる。ゼロ除算を外に漏らさない。"""
        pairs = self._pairs(gf)
        stats = gf.bucket_stats(pairs, 0.0)
        assert stats.stderr_bps == pytest.approx(0.0)
        assert stats.t_stat == 0.0
