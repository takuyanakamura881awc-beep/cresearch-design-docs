"""営業日カレンダーのテスト。

**祝日テーブルを持たず、観測した日足から営業日を定義する。**
最大の関心は「観測範囲外を『休みだった』と誤答しないこと」。
誤答すると存在するはずの取引日を静かに飛ばす。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from autotrader.data.calendar import (
    TradingCalendar,
    UnknownTradingDayError,
    days_to_earnings,
)
from autotrader.types import Bar

# 2026-06-01(月) 〜 06-05(金) と 06-08(月)。06-06/07 は土日
WEEK = [date(2026, 6, d) for d in (1, 2, 3, 4, 5, 8)]
CAL = TradingCalendar.from_dates(WEEK)


def _bar(code: str, d: date) -> Bar:
    ts = datetime(d.year, d.month, d.day)
    return Bar(symbol=code, timestamp=ts, open=1.0, high=1.0, low=1.0, close=1.0, volume=1)


class TestConstruction:
    def test_日足から営業日を復元する(self) -> None:
        bars = {"7203": (_bar("7203", WEEK[0]), _bar("7203", WEEK[1]))}
        assert TradingCalendar.from_bars(bars).days == (WEEK[0], WEEK[1])

    def test_銘柄をまたいで和を取る(self) -> None:
        """個別銘柄は売買停止でバーが欠けうる。1銘柄でもあれば営業日。"""
        bars = {"7203": (_bar("7203", WEEK[0]),), "6758": (_bar("6758", WEEK[1]),)}
        assert TradingCalendar.from_bars(bars).days == (WEEK[0], WEEK[1])

    def test_重複と順序を正す(self) -> None:
        cal = TradingCalendar.from_dates([WEEK[1], WEEK[0], WEEK[1]])
        assert cal.days == (WEEK[0], WEEK[1])

    def test_未整列のまま直接構築するのは拒否する(self) -> None:
        with pytest.raises(ValueError):
            TradingCalendar(days=(WEEK[1], WEEK[0]))

    def test_観測範囲を返す(self) -> None:
        assert CAL.covered == (WEEK[0], WEEK[-1])
        assert TradingCalendar.from_dates([]).covered is None


class TestIsTradingDay:
    def test_営業日を判定する(self) -> None:
        assert CAL.is_trading_day(date(2026, 6, 3))

    def test_土日は営業日でない(self) -> None:
        assert not CAL.is_trading_day(date(2026, 6, 6))

    def test_観測範囲外は例外にする(self) -> None:
        """**「知らない」を「休みだった」と混同しない。**

        黙って False を返すと、存在するはずの取引日が静かに飛ぶ。
        """
        with pytest.raises(UnknownTradingDayError, match="観測範囲"):
            CAL.is_trading_day(date(2026, 6, 9))
        with pytest.raises(UnknownTradingDayError):
            CAL.is_trading_day(date(2026, 5, 29))

    def test_空のカレンダーは例外にする(self) -> None:
        with pytest.raises(UnknownTradingDayError):
            TradingCalendar.from_dates([]).is_trading_day(date(2026, 6, 1))


class TestNeighbours:
    def test_直前の営業日は週末を飛ばす(self) -> None:
        assert CAL.previous_trading_day(date(2026, 6, 8)) == date(2026, 6, 5)

    def test_非営業日からでも遡れる(self) -> None:
        assert CAL.previous_trading_day(date(2026, 6, 7)) == date(2026, 6, 5)

    def test_自分自身は含まない(self) -> None:
        assert CAL.previous_trading_day(date(2026, 6, 3)) == date(2026, 6, 2)
        assert CAL.next_trading_day(date(2026, 6, 3)) == date(2026, 6, 4)

    def test_次の営業日は週末を飛ばす(self) -> None:
        assert CAL.next_trading_day(date(2026, 6, 5)) == date(2026, 6, 8)

    def test_端では例外にする(self) -> None:
        with pytest.raises(UnknownTradingDayError):
            CAL.previous_trading_day(WEEK[0])
        with pytest.raises(UnknownTradingDayError):
            CAL.next_trading_day(WEEK[-1])


class TestSessions:
    def test_期間内の営業日を返す(self) -> None:
        got = CAL.sessions(date(2026, 6, 3), date(2026, 6, 8))
        assert got == (date(2026, 6, 3), date(2026, 6, 4), date(2026, 6, 5), date(2026, 6, 8))

    def test_両端を含む(self) -> None:
        assert CAL.sessions(WEEK[0], WEEK[0]) == (WEEK[0],)

    def test_範囲外を含むなら例外にする(self) -> None:
        """観測している部分だけ黙って返すと、期間が欠けたバックテストに気づけない。"""
        with pytest.raises(UnknownTradingDayError):
            CAL.sessions(date(2026, 5, 25), date(2026, 6, 3))

    def test_逆順は拒否する(self) -> None:
        with pytest.raises(ValueError):
            CAL.sessions(date(2026, 6, 4), date(2026, 6, 2))


class TestBusinessDaysBetween:
    def test_営業日数を数える(self) -> None:
        assert CAL.business_days_between(date(2026, 6, 1), date(2026, 6, 4)) == 3

    def test_週末をまたいでも暦日ではなく営業日で数える(self) -> None:
        # 6/5(金) → 6/8(月) は暦日3日だが営業日は1日
        assert CAL.business_days_between(date(2026, 6, 5), date(2026, 6, 8)) == 1

    def test_同日はゼロ(self) -> None:
        assert CAL.business_days_between(WEEK[0], WEEK[0]) == 0

    def test_過去向きは負値(self) -> None:
        """決算「後」も除外対象なので、符号で前後を区別できる必要がある。"""
        assert CAL.business_days_between(date(2026, 6, 4), date(2026, 6, 1)) == -3


class TestDaysToEarnings:
    def test_次の決算までの営業日数(self) -> None:
        got = days_to_earnings("7203", date(2026, 6, 1), {"7203": (date(2026, 6, 4),)}, CAL)
        assert got == 3

    def test_発表直後は負値になる(self) -> None:
        """ギャップリスクは発表の前後どちらにもある。"""
        got = days_to_earnings("7203", date(2026, 6, 4), {"7203": (date(2026, 6, 1),)}, CAL)
        assert got == -3

    def test_予定が不明ならNone(self) -> None:
        assert days_to_earnings("7203", date(2026, 6, 1), {}, CAL) is None
        assert days_to_earnings("7203", date(2026, 6, 1), {"7203": ()}, CAL) is None

    def test_観測範囲外はNoneにする(self) -> None:
        """「決算が近くない」と断定できない。不明を不明として返す。"""
        got = days_to_earnings("7203", date(2026, 6, 1), {"7203": (date(2026, 9, 1),)}, CAL)
        assert got is None
