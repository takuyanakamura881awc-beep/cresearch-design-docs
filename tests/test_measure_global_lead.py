"""scripts/measure_global_lead.py のテスト。

**スクリプトファイルなので `pythonpath` には乗らない。** importlib で直接読む
（`tests/test_measure_gap_fade.py` と同じパターン）。

重点は3つ:

1. **織り込み率が正しく出ること。** 合成データで「ギャップに何%入ったか」を
   仕込み、`priced_in` がそれを復元できるか。ここが狂うと
   セクション2の残余を解釈できない
2. **市場平均が銘柄ごとではなく日ごとに1つになること**（意思決定ログ72）
3. **判定基準がコードに埋まっていること**——結果を見てから動かさない（意思決定ログ87）
"""

from __future__ import annotations

import importlib.util
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from autotrader.types import Bar

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "measure_global_lead.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("measure_global_lead_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mgl() -> ModuleType:
    return _load_script()


def _bar(symbol: str, day: date, *, open_: float, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=datetime(day.year, day.month, day.day),
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=100_000,
    )


def _weekdays(n: int, start: date = date(2026, 1, 5)) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


class TestMarketDays:
    """市場平均は**日ごとに1つ**。銘柄ごとに数えると t値が過大に出る。"""

    def _daily(self, symbols: int, days: list[date]) -> dict[str, tuple[Bar, ...]]:
        out: dict[str, tuple[Bar, ...]] = {}
        for i in range(symbols):
            code = f"{2000 + i}"
            bars, close = [], 2_000.0
            for day in days:
                open_ = close * 1.01
                new_close = open_ * 1.02
                bars.append(_bar(code, day, open_=open_, close=new_close))
                close = new_close
            out[code] = tuple(bars)
        return out

    def test_日ごとに1件になる(self, mgl: ModuleType) -> None:
        days = _weekdays(10)
        got = mgl.market_days(self._daily(5, days))
        # 初日はギャップが作れないので9日ぶん
        assert len(got) == 9
        assert [d.day for d in got] == days[1:]

    def test_銘柄数を増やしても日数は変わらない(self, mgl: ModuleType) -> None:
        days = _weekdays(10)
        few = mgl.market_days(self._daily(3, days))
        many = mgl.market_days(self._daily(30, days))
        assert len(few) == len(many)
        assert many[0].symbols == 30

    def test_ギャップと日中リターンを分けて持つ(self, mgl: ModuleType) -> None:
        days = _weekdays(5)
        got = mgl.market_days(self._daily(2, days))
        assert got[0].gap_bps == pytest.approx(100.0)  # +1%
        assert got[0].intraday_bps == pytest.approx(200.0)  # +2%

    def test_価格が0以下の日は除く(self, mgl: ModuleType) -> None:
        """0除算対策。既存の診断と同じ規律。"""
        days = _weekdays(3)
        bars = (
            _bar("A", days[0], open_=100.0, close=0.0),
            _bar("A", days[1], open_=100.0, close=110.0),
        )
        assert mgl.market_days({"A": bars}) == ()


class TestPricedIn:
    """**事実確認であって検定ではない。** 織り込み率を知らないと残余を読めない。"""

    def _fixture(
        self, mgl: ModuleType, absorbed: float
    ) -> tuple[dict[date, float], tuple[Any, ...]]:
        """夜間の変動 x のうち ``absorbed`` の割合がギャップに入る合成データ。"""
        days = _weekdays(60)
        night = {d: 0.01 * math.sin(i) for i, d in enumerate(days)}
        bars, close = [], 2_000.0
        for day in days:
            r = night[day]
            open_ = close * (1 + absorbed * r)
            new_close = open_ * (1 + (1 - absorbed) * r)
            bars.append(_bar("A", day, open_=open_, close=new_close))
            close = new_close
        return night, mgl.market_days({"A": tuple(bars)})

    def test_織り込み率を復元する(self, mgl: ModuleType) -> None:
        night, days = self._fixture(mgl, 0.8)
        fit = mgl.priced_in(night, days)
        assert fit is not None
        # 1% の夜間変動に対し 0.8% = 80bps 動く
        assert fit.slope == pytest.approx(80.0, rel=1e-3)
        assert fit.r_squared == pytest.approx(1.0, abs=1e-6)

    def test_織り込みゼロなら傾きゼロ(self, mgl: ModuleType) -> None:
        night, days = self._fixture(mgl, 0.0)
        fit = mgl.priced_in(night, days)
        assert fit is not None
        assert fit.slope == pytest.approx(0.0, abs=1e-6)

    def test_共通する日が少なければNone(self, mgl: ModuleType) -> None:
        _, days = self._fixture(mgl, 0.5)
        assert mgl.priced_in({}, days) is None


class TestResidualScore:
    """夜間の方向についていく向きにそろえる。**反転版を別の変種として数えない。**"""

    def _day(self, mgl: ModuleType, intraday: float) -> Any:
        return mgl.MarketDay(
            day=date(2026, 6, 1),
            gap_bps=0.0,
            intraday_bps=intraday,
            cost_bps=10.0,
            symbols=10,
        )

    def test_夜間が上げなら日中の上げが正(self, mgl: ModuleType) -> None:
        assert mgl.residual_score(0.01, self._day(mgl, 50.0)) == pytest.approx(50.0)

    def test_夜間が下げなら日中の下げが正(self, mgl: ModuleType) -> None:
        assert mgl.residual_score(-0.01, self._day(mgl, -50.0)) == pytest.approx(50.0)

    def test_夜間が動いていなければゼロ(self, mgl: ModuleType) -> None:
        assert mgl.residual_score(0.0, self._day(mgl, 50.0)) == pytest.approx(0.0)


class TestBucketStats:
    def _fixture(self, mgl: ModuleType, n: int = 200) -> Any:
        days = _weekdays(n)
        night = {d: 0.01 * math.sin(i) for i, d in enumerate(days)}
        ranks = {d: (i % 10) / 10.0 for i, d in enumerate(days)}
        bars, close = [], 2_000.0
        for day in days:
            open_ = close * 1.001
            new_close = open_ * 1.001
            bars.append(_bar("A", day, open_=open_, close=new_close))
            close = new_close
        return night, ranks, mgl.market_days({"A": tuple(bars)})

    def test_件数が足りなければNone(self, mgl: ModuleType) -> None:
        """**無理に判定しない。** 少ない標本でパーセンタイルを出さない。"""
        night, ranks, days = self._fixture(mgl, n=40)
        assert mgl.bucket_stats(night, ranks, days, 0.9) is None

    def test_順位の下限で絞る(self, mgl: ModuleType) -> None:
        night, ranks, days = self._fixture(mgl)
        low = mgl.bucket_stats(night, ranks, days, 0.0)
        high = mgl.bucket_stats(night, ranks, days, 0.5)
        assert low is not None and high is not None
        assert high.n < low.n

    def test_netはgrossからコストを引いたもの(self, mgl: ModuleType) -> None:
        night, ranks, days = self._fixture(mgl)
        stats = mgl.bucket_stats(night, ranks, days, 0.0)
        assert stats is not None
        assert stats.net_bps == pytest.approx(stats.gross_bps - stats.cost_bps)


class TestPreRegistration:
    """**基準はコードに埋める。** 結果を見てから動かさないため（意思決定ログ87）。"""

    def test_バケットは3つに固定(self, mgl: ModuleType) -> None:
        """5系列 × 3バケットで15セル。増やすと多重比較で偶然を拾う。"""
        assert len(mgl.RANK_BUCKETS) == 3

    def test_バケットは昇順(self, mgl: ModuleType) -> None:
        assert list(mgl.RANK_BUCKETS) == sorted(mgl.RANK_BUCKETS)

    def test_目標年利は据え置き(self, mgl: ModuleType) -> None:
        assert mgl.ANNUAL_TARGET == 0.25

    def test_最低日数を持つ(self, mgl: ModuleType) -> None:
        assert mgl.MIN_BUCKET_DAYS >= 30
