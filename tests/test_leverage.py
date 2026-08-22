"""レバレッジ1倍強制（安全装置 #1）のテスト。

**本システムの安全性の土台。** 信用取引を使いながら使用金額を現物と
同水準に抑えることが、リスクを現物並みに保つ唯一の仕組み。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from autotrader.risk.leverage import LeverageViolationError, check, enforce
from autotrader.types import AccountState, MarginTradeType, Position, Side

NOW = datetime(2026, 6, 1, 9, 0)


def _position(price: float, qty: int, side: Side = Side.LONG) -> Position:
    return Position(
        symbol="7203",
        side=side,
        quantity=qty,
        entry_price=price,
        margin_trade_type=MarginTradeType.DAYTRADE,
        opened_at=NOW,
    )


def _account(cash: int, *positions: Position) -> AccountState:
    return AccountState(cash=Decimal(cash), positions=positions, as_of=NOW)


class TestLeverageCheck:
    def test_残高内なら通す(self) -> None:
        result = check(_account(500_000), Decimal(300_000))
        assert result.allowed
        assert result.resulting_ratio == pytest.approx(0.6)

    def test_ちょうど残高なら通す(self) -> None:
        """不変条件は「≤」。等号は違反ではない。"""
        result = check(_account(500_000), Decimal(500_000))
        assert result.allowed
        assert result.resulting_ratio == pytest.approx(1.0)

    def test_1円でも超えたら拒否する(self) -> None:
        result = check(_account(500_000), Decimal(500_001))
        assert not result.allowed
        assert "レバレッジ上限違反" in result.reason

    def test_既存建玉を合算する(self) -> None:
        account = _account(500_000, _position(1000.0, 300))  # 30万円
        assert check(account, Decimal(200_000)).allowed
        assert not check(account, Decimal(200_001)).allowed

    def test_ロングとショートを相殺しない(self) -> None:
        """**両建てでもリスクは合算される。**

        ネットで見ると 0 になるが、それぞれ独立に逆行しうる。
        """
        account = _account(
            500_000,
            _position(1000.0, 250, Side.LONG),
            _position(1000.0, 250, Side.SHORT),
        )
        assert account.gross_notional == Decimal(500_000)
        assert not check(account, Decimal(1)).allowed

    def test_現金がゼロなら必ず拒否する(self) -> None:
        """比率がゼロ除算になる領域を判定の入口で塞ぐ。"""
        result = check(_account(0), Decimal(1))
        assert not result.allowed
        assert "現金残高" in result.reason

    def test_現金が負でも拒否する(self) -> None:
        account = AccountState(cash=Decimal(-1000), positions=(), as_of=NOW)
        assert not check(account, Decimal(1)).allowed

    def test_負の発注額を拒否する(self) -> None:
        """返済は建玉の減少として扱う。ここに負値を渡す経路は誤り。"""
        result = check(_account(500_000), Decimal(-100_000))
        assert not result.allowed

    def test_判定の内訳を残す(self) -> None:
        """監査ログのため、なぜ通ったか/落ちたかが後から読めること。"""
        result = check(_account(500_000, _position(1000.0, 300)), Decimal(100_000))
        assert result.current_notional == Decimal(300_000)
        assert result.additional_notional == Decimal(100_000)
        assert result.cash == Decimal(500_000)
        assert result.reason


class TestEnforce:
    def test_通れば何も起きない(self) -> None:
        enforce(_account(500_000), Decimal(100_000))

    def test_超えたら例外で止める(self) -> None:
        """**呼び出し側が結果を無視できないようにする。**

        戻り値だと `if` を書き忘れた経路がそのまま発注に進む。
        """
        with pytest.raises(LeverageViolationError):
            enforce(_account(500_000), Decimal(600_000))

    def test_例外に内訳が入る(self) -> None:
        with pytest.raises(LeverageViolationError, match="600,000"):
            enforce(_account(500_000), Decimal(600_000))
