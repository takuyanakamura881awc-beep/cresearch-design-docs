"""営業日・決算発表日のカレンダー。

決算発表の前後はギャップリスクが予測不能なため、
ユニバースから除外する（docs/03-universe.md §1 フィルタF）。
"""

from __future__ import annotations

from datetime import date


def is_trading_day(d: date) -> bool:
    """東証の営業日か。"""
    raise NotImplementedError("Phase 1 で実装する")


def previous_trading_day(d: date) -> date:
    """直前の営業日を返す。"""
    raise NotImplementedError("Phase 1 で実装する")


def days_to_earnings(symbol: str, as_of: date) -> int | None:
    """次の決算発表日までの営業日数。

    **``as_of`` 時点で公表されている予定日のみを使うこと。**
    後から確定した日付を過去に遡って適用するとルックアヘッドになる。

    Returns:
        営業日数。予定が不明なら ``None``。
    """
    raise NotImplementedError("Phase 1 で実装する")
