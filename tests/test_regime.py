"""日次の値動きの荒さ分類（事後診断専用）のテスト。

重点は3つ。

1. **`daily_range_pct` が既存の `prev_range_pct` と同じ式であること**
   （新しい指標を発明していないことの確認）
2. **境界を固定値で持たず、その日の中央値で決まること**
3. **他の日のバーが混ざらないこと**（`classify_days` が日付でちゃんと絞ること）
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from autotrader.regime import classify_days, daily_range_pct
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


class TestClassifyDays:
    def test_中央値で二分する(self) -> None:
        """固定閾値を持たず、その日の中央値が境界になる。"""
        bars_by_symbol: dict[str, tuple[Bar, ...]] = {
            # 値幅の狭い順に A(2%) < B(4%) < C(6%)
            "A": (_bar("A", DAY, 0, high=1010.0, low=990.0, close=1000.0),),  # 2.0%
            "B": (_bar("B", DAY, 0, high=1020.0, low=980.0, close=1000.0),),  # 4.0%
            "C": (_bar("C", DAY, 0, high=1030.0, low=970.0, close=1000.0),),  # 6.0%
        }
        result = classify_days(bars_by_symbol, DAY)
        # 中央値は B(4%)。中央値以上を wild とするので B は wild
        assert result["A"] == "calm"
        assert result["B"] == "wild"
        assert result["C"] == "wild"

    def test_日付できちんと絞る(self) -> None:
        """他の日のバーが混ざって中央値がずれてはいけない。"""
        bars_by_symbol = {
            "A": (
                _bar("A", DAY, 0, high=1010.0, low=990.0, close=1000.0),  # 2.0%（対象日）
                _bar("A", OTHER_DAY, 0, high=1500.0, low=500.0, close=1000.0),  # 別日の暴れ足
            ),
            "B": (_bar("B", DAY, 0, high=1020.0, low=980.0, close=1000.0),),  # 4.0%
        }
        result = classify_days(bars_by_symbol, DAY)
        # 別日の暴れ足が混ざっていれば A が wild 側に寄るはずだが、
        # 対象日だけで見れば A(2%) < B(4%) なので A は calm
        assert result["A"] == "calm"
        assert result["B"] == "wild"

    def test_対象日にバーがない銘柄は含まれない(self) -> None:
        bars_by_symbol: dict[str, tuple[Bar, ...]] = {
            "A": (_bar("A", DAY, 0, high=1010.0, low=990.0, close=1000.0),),
            "B": (_bar("B", OTHER_DAY, 0, high=1020.0, low=980.0, close=1000.0),),
        }
        result = classify_days(bars_by_symbol, DAY)
        assert set(result) == {"A"}

    def test_対象日のバーが1つもなければ空(self) -> None:
        bars_by_symbol: dict[str, tuple[Bar, ...]] = {
            "A": (_bar("A", OTHER_DAY, 0, high=10.0, low=5.0, close=8.0),)
        }
        assert classify_days(bars_by_symbol, DAY) == {}

    def test_1銘柄しかなければ自分自身が中央値でwildになる(self) -> None:
        """中央値以上を wild とするので、1件だけなら必ず wild。境界値の仕様として明示する。"""
        bars_by_symbol: dict[str, tuple[Bar, ...]] = {
            "A": (_bar("A", DAY, 0, high=1010.0, low=990.0, close=1000.0),)
        }
        assert classify_days(bars_by_symbol, DAY) == {"A": "wild"}
