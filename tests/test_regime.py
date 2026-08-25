"""日次の値動きの荒さ分類（事後診断専用）のテスト。

重点は4つ。

1. **`daily_range_pct` が既存の `prev_range_pct` と同じ式であること**
   （新しい指標を発明していないことの確認）
2. **`classify_days` は日ごとに1つのラベルを付けること**
   （銘柄ごとに分類する旧実装は、エントリー条件とほぼ同義になり
   トレードのほぼ全部が同じラベルに集まった。意思決定ログ48）
3. **各日の値は、その日の全銘柄の `daily_range_pct` の中央値であること**
4. **境界を固定値で持たず、日をまたいだ中央値で決まること**
5. **他の日のバーが混ざらないこと**（対象日以外のバーが集計にも
   閾値にも影響しないこと）
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from autotrader.regime import classify_days, daily_range_pct, market_range_by_day
from autotrader.types import Bar

DAY = date(2026, 6, 1)
OTHER_DAY = date(2026, 6, 2)


def _bar(symbol: str, day: date, minute: int, *, high: float, low: float, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=datetime(day.year, day.month, day.day, 9, 0) + timedelta(minutes=minute),
        open=close,
        high=high,
        low=low,
        close=close,
        volume=10_000,
        turnover=2_000_000_000.0,
    )


class TestDailyRangePct:
    def test_既存のprev_range_pctと同じ式(self) -> None:
        """(高値 - 安値) ÷ 終値。universe/selector.py と同じ式であること。"""
        bars = (
            _bar("A", DAY, 0, high=1010.0, low=995.0, close=1000.0),
            _bar("A", DAY, 5, high=1005.0, low=990.0, close=998.0),
        )
        # 高値 max(1010,1005)=1010 / 安値 min(995,990)=990 / 終値 998（最後のバー）
        expected = (1010.0 - 990.0) / 998.0
        assert daily_range_pct(bars) == pytest.approx(expected)

    def test_バーが空ならNone(self) -> None:
        assert daily_range_pct(()) is None

    def test_終値がゼロ以下ならNone(self) -> None:
        bars = (_bar("A", DAY, 0, high=10.0, low=0.0, close=0.0),)
        assert daily_range_pct(bars) is None


THIRD_DAY = date(2026, 6, 3)


class TestMarketRangeByDay:
    def test_日ごとに全銘柄のdaily_range_pctの中央値になる(self) -> None:
        bars_by_symbol: dict[str, tuple[Bar, ...]] = {
            "A": (_bar("A", DAY, 0, high=1010.0, low=990.0, close=1000.0),),  # 2.0%
            "B": (_bar("B", DAY, 0, high=1020.0, low=980.0, close=1000.0),),  # 4.0%
            "C": (_bar("C", DAY, 0, high=1030.0, low=970.0, close=1000.0),),  # 6.0%
        }
        result = market_range_by_day(bars_by_symbol, (DAY,))
        assert result[DAY] == pytest.approx(0.04)

    def test_対象日リスト外の日は含まれない(self) -> None:
        bars_by_symbol: dict[str, tuple[Bar, ...]] = {
            "A": (_bar("A", DAY, 0, high=1010.0, low=990.0, close=1000.0),),
            "B": (_bar("B", OTHER_DAY, 0, high=1020.0, low=980.0, close=1000.0),),
        }
        result = market_range_by_day(bars_by_symbol, (DAY,))
        assert set(result) == {DAY}

    def test_値動きを観測できる銘柄が1つもない日は含まれない(self) -> None:
        bars_by_symbol: dict[str, tuple[Bar, ...]] = {
            "A": (_bar("A", OTHER_DAY, 0, high=10.0, low=5.0, close=8.0),)
        }
        assert market_range_by_day(bars_by_symbol, (DAY,)) == {}


class TestClassifyDays:
    def test_日ごとに全銘柄の中央値を市場全体の荒さとし日をまたいだ中央値で二分する(
        self,
    ) -> None:
        """銘柄ごとではなく日ごとに1つのラベルが付き、境界は日をまたいだ中央値になる。"""
        bars_by_symbol: dict[str, tuple[Bar, ...]] = {
            "A": (
                _bar("A", DAY, 0, high=1010.0, low=990.0, close=1000.0),  # DAY: 2.0%
                _bar("A", OTHER_DAY, 0, high=1050.0, low=950.0, close=1000.0),  # 10.0%
                _bar("A", THIRD_DAY, 0, high=1100.0, low=900.0, close=1000.0),  # 20.0%
            ),
            "B": (
                _bar("B", DAY, 0, high=1020.0, low=980.0, close=1000.0),  # DAY: 4.0%
                _bar("B", OTHER_DAY, 0, high=1060.0, low=940.0, close=1000.0),  # 12.0%
                _bar("B", THIRD_DAY, 0, high=1110.0, low=890.0, close=1000.0),  # 22.0%
            ),
            "C": (
                _bar("C", DAY, 0, high=1030.0, low=970.0, close=1000.0),  # DAY: 6.0%
                _bar("C", OTHER_DAY, 0, high=1070.0, low=930.0, close=1000.0),  # 14.0%
                _bar("C", THIRD_DAY, 0, high=1120.0, low=880.0, close=1000.0),  # 24.0%
            ),
        }
        # 日ごとの市場全体の荒さ（3銘柄の中央値）: DAY=4.0% / OTHER_DAY=12.0% / THIRD_DAY=22.0%
        # 日をまたいだ中央値は OTHER_DAY(12.0%)
        result = classify_days(bars_by_symbol, (DAY, OTHER_DAY, THIRD_DAY))
        assert result[DAY] == "calm"
        assert result[OTHER_DAY] == "wild"
        assert result[THIRD_DAY] == "wild"

    def test_対象日リスト外の日は結果にも閾値にも混ざらない(self) -> None:
        """`days` に含めない日のバーは、他の日の集計にも境界にも影響しない。"""
        bars_by_symbol: dict[str, tuple[Bar, ...]] = {
            "A": (
                _bar("A", DAY, 0, high=1010.0, low=990.0, close=1000.0),  # DAY: 2.0%
                # OTHER_DAY は days に含めないので、暴れていても結果に出ない
                _bar("A", OTHER_DAY, 0, high=1500.0, low=500.0, close=1000.0),  # 100.0%
            ),
        }
        result = classify_days(bars_by_symbol, (DAY,))
        assert set(result) == {DAY}

    def test_対象日に値動きを観測できる銘柄が1つもなければその日は除かれる(self) -> None:
        bars_by_symbol: dict[str, tuple[Bar, ...]] = {
            "A": (_bar("A", DAY, 0, high=1010.0, low=990.0, close=1000.0),),
            "B": (_bar("B", OTHER_DAY, 0, high=1020.0, low=980.0, close=1000.0),),
        }
        # THIRD_DAY にはどの銘柄のバーもない
        result = classify_days(bars_by_symbol, (DAY, OTHER_DAY, THIRD_DAY))
        assert set(result) == {DAY, OTHER_DAY}

    def test_対象日が1つもなければ空(self) -> None:
        bars_by_symbol: dict[str, tuple[Bar, ...]] = {
            "A": (_bar("A", DAY, 0, high=10.0, low=5.0, close=8.0),)
        }
        assert classify_days(bars_by_symbol, ()) == {}

    def test_1日しか渡さなければその日は必ずwildになる(self) -> None:
        """中央値以上を wild とするので、1日だけなら必ず wild。境界値の仕様として明示する。"""
        bars_by_symbol: dict[str, tuple[Bar, ...]] = {
            "A": (_bar("A", DAY, 0, high=1010.0, low=990.0, close=1000.0),)
        }
        assert classify_days(bars_by_symbol, (DAY,)) == {DAY: "wild"}
