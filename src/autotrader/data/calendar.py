"""営業日・決算発表日のカレンダー。

決算発表の前後はギャップリスクが予測不能なため、
ユニバースから除外する（docs/03-universe.md §1 フィルタF）。

【祝日テーブルを持たない理由】

日本の祝日は自前で持つと必ずずれる。春分・秋分は天文計算で年ごとに変わり、
振替休日と国民の休日の規則もあり、五輪年のような臨時の移動もあった。
さらに**祝日でなくても取引所が閉まる日**（12/31〜1/3、システム障害）があり、
祝日カレンダーと取引所カレンダーは一致しない。

**そこで、実際に日足が存在した日を営業日の定義とする。**
J-Quants の日次データそのものが取引所の営業実績なので、
テーブルの更新漏れという故障モードが原理的に存在しない。

代償として「まだデータを取っていない期間は判定できない」。
これは黙って推測せず `UnknownTradingDayError` で落とす。
**推測して営業日扱いすると、存在しない日のバーを待って処理が止まる。**
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

from autotrader.types import Bar


class UnknownTradingDayError(Exception):
    """カレンダーが観測していない期間について判定を求められた。

    握り潰して「営業日ではない」と答えてはならない。
    **観測範囲外は「休みだった」ではなく「知らない」**であり、
    混同すると存在するはずの取引日を静かに飛ばす。
    """


@dataclass(frozen=True)
class TradingCalendar:
    """実際に取引が行われた日の集合。

    **観測データから作る。** 祝日テーブルは持たない（module docstring 参照）。
    """

    days: tuple[date, ...]
    """営業日。昇順・重複なし。"""

    def __post_init__(self) -> None:
        if list(self.days) != sorted(set(self.days)):
            raise ValueError("days は昇順かつ重複なしである必要がある")

    @classmethod
    def from_dates(cls, days: Iterable[date]) -> TradingCalendar:
        return cls(days=tuple(sorted(set(days))))

    @classmethod
    def from_bars(cls, bars_by_symbol: Mapping[str, tuple[Bar, ...]]) -> TradingCalendar:
        """日足の観測結果から営業日を復元する。

        **1銘柄でもバーがあればその日は営業日**とする。
        個別銘柄は売買停止でバーが欠けうるので、銘柄をまたいで和を取る。
        """
        days = {
            bar.timestamp.date() for bars in bars_by_symbol.values() for bar in bars
        }
        return cls.from_dates(days)

    @property
    def covered(self) -> tuple[date, date] | None:
        """観測できている期間。空なら ``None``。"""
        return (self.days[0], self.days[-1]) if self.days else None

    def _require_covered(self, d: date) -> None:
        if not self.days:
            raise UnknownTradingDayError("カレンダーが空。営業日を判定できない")
        first, last = self.days[0], self.days[-1]
        if not first <= d <= last:
            raise UnknownTradingDayError(
                f"{d} は観測範囲 {first}〜{last} の外。営業日かどうか判定できない"
            )

    def is_trading_day(self, d: date) -> bool:
        """東証の営業日か。

        Raises:
            UnknownTradingDayError: 観測範囲外の日付。
        """
        self._require_covered(d)
        i = bisect_left(self.days, d)
        return i < len(self.days) and self.days[i] == d

    def previous_trading_day(self, d: date) -> date:
        """``d`` より前の直近の営業日。``d`` 自身は含まない。

        Raises:
            UnknownTradingDayError: 観測範囲外、または前の営業日が範囲内にない。
        """
        self._require_covered(d)
        i = bisect_left(self.days, d)
        if i == 0:
            raise UnknownTradingDayError(
                f"{d} より前の営業日が観測範囲 {self.days[0]}〜{self.days[-1]} にない"
            )
        return self.days[i - 1]

    def next_trading_day(self, d: date) -> date:
        """``d`` より後の直近の営業日。``d`` 自身は含まない。

        Raises:
            UnknownTradingDayError: 観測範囲外、または次の営業日が範囲内にない。
        """
        self._require_covered(d)
        i = bisect_right(self.days, d)
        if i >= len(self.days):
            raise UnknownTradingDayError(
                f"{d} より後の営業日が観測範囲 {self.days[0]}〜{self.days[-1]} にない"
            )
        return self.days[i]

    def sessions(self, start: date, end: date) -> tuple[date, ...]:
        """``start`` 以上 ``end`` 以下の営業日。

        **範囲の端が観測範囲外なら例外にする。** 観測している部分だけを
        黙って返すと、期間の一部が欠けたバックテストに気づけない。
        """
        self._require_covered(start)
        self._require_covered(end)
        if start > end:
            raise ValueError(f"start > end: {start} > {end}")
        lo = bisect_left(self.days, start)
        hi = bisect_right(self.days, end)
        return self.days[lo:hi]

    def business_days_between(self, start: date, end: date) -> int:
        """``start`` から ``end`` までの営業日数（``start`` を含み ``end`` を含まない）。

        符号つき。``end`` が ``start`` より前なら負値を返す。
        """
        if start == end:
            return 0
        sign = 1 if end > start else -1
        lo, hi = (start, end) if end > start else (end, start)
        self._require_covered(lo)
        self._require_covered(hi)
        return sign * (bisect_left(self.days, hi) - bisect_left(self.days, lo))


def days_to_earnings(
    symbol: str,
    as_of: date,
    announcements: dict[str, tuple[date, ...]],
    calendar: TradingCalendar,
) -> int | None:
    """次の決算発表日までの営業日数。

    **``as_of`` 時点で公表されている予定日のみを使うこと。**
    後から確定した日付を過去に遡って適用するとルックアヘッドになる
    （呼び出し側が ``announcements`` を作る時点で守る責任がある）。

    Args:
        announcements: 銘柄コード → 決算発表日。**その時点で公表済みのものだけ。**

    Returns:
        営業日数。予定が不明なら ``None``。
        直近の発表が過去なら負値（発表直後も除外対象にするため符号で返す）。
    """
    dates = announcements.get(symbol)
    if not dates:
        return None

    future = [d for d in sorted(dates) if d >= as_of]
    target = future[0] if future else max(d for d in dates)
    try:
        return calendar.business_days_between(as_of, target)
    except UnknownTradingDayError:
        # 観測範囲外は「決算が近くない」と断定できない。
        # 不明を不明として返し、フィルタ側で判定をスキップさせる。
        return None
