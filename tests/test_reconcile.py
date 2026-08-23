"""建玉の突合（安全装置 #10）のテスト。

**状態のズレを抱えたまま自動発注を続けるのが最も危険。**
不一致なら発注を停止し、人が原因を確認するまで再開しない。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from autotrader.broker.base import Broker, BrokerError
from autotrader.execution.reconcile import reconcile
from autotrader.types import (
    AccountState,
    MarginTradeType,
    Order,
    Position,
    Quote,
    Side,
)

NOW = datetime(2026, 6, 1, 8, 55)


def _position(
    symbol: str, side: Side = Side.LONG, quantity: int = 100
) -> Position:
    return Position(symbol, side, quantity, 1000.0, MarginTradeType.DAYTRADE, NOW)


class _FakeBroker(Broker):
    def __init__(self, positions: tuple[Position, ...], *, error: bool = False) -> None:
        self._positions = positions
        self._error = error

    def get_account(self) -> AccountState:
        return AccountState(Decimal(500_000), self._positions, NOW)

    def get_positions(self) -> tuple[Position, ...]:
        if self._error:
            raise BrokerError("建玉照会に失敗")
        return self._positions

    def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError

    def send_order(self, order: Order) -> Order:
        raise NotImplementedError

    def cancel_order(self, client_order_id: str) -> None:
        raise NotImplementedError

    def get_orders(self) -> tuple[Order, ...]:
        return ()

    def is_shortable(self, symbol: str) -> bool:
        return True


class TestConsistent:
    def test_一致すれば通す(self) -> None:
        positions = (_position("7203"), _position("6758"))
        result = reconcile(_FakeBroker(positions), positions)

        assert result.is_consistent
        assert len(result.matched) == 2
        assert "突合OK" in result.summary()

    def test_どちらも空なら一致(self) -> None:
        assert reconcile(_FakeBroker(()), ()).is_consistent

    def test_順序が違っても一致(self) -> None:
        a = (_position("7203"), _position("6758"))
        b = (_position("6758"), _position("7203"))
        assert reconcile(_FakeBroker(a), b).is_consistent


class TestMismatch:
    def test_実際にあるがシステムが知らない建玉(self) -> None:
        """**最も危険。** 誰も管理していない建玉が翌日に持ち越される。"""
        result = reconcile(_FakeBroker((_position("7203"),)), ())

        assert not result.is_consistent
        assert [p.symbol for p in result.only_in_broker] == ["7203"]
        assert "最も危険" in result.summary()

    def test_システムは持っているつもりだが実際にない(self) -> None:
        result = reconcile(_FakeBroker(()), (_position("7203"),))

        assert not result.is_consistent
        assert [p.symbol for p in result.only_in_local] == ["7203"]

    def test_数量が食い違う(self) -> None:
        """**片方にしかない建玉と同じくらい危険。**

        「返済したつもりで残る」「二重に返済しようとする」が起きる。
        """
        result = reconcile(
            _FakeBroker((_position("7203", quantity=200),)),
            (_position("7203", quantity=100),),
        )
        assert not result.is_consistent
        assert len(result.mismatched) == 1
        assert "食い違う" in result.summary()

    def test_方向が食い違う(self) -> None:
        result = reconcile(
            _FakeBroker((_position("7203", Side.SHORT),)),
            (_position("7203", Side.LONG),),
        )
        assert not result.is_consistent
        assert len(result.mismatched) == 1

    def test_一致と不一致が混在する(self) -> None:
        actual = (_position("7203"), _position("9984"))
        local = (_position("7203"), _position("6758"))
        result = reconcile(_FakeBroker(actual), local)

        assert [p.symbol for p in result.matched] == ["7203"]
        assert [p.symbol for p in result.only_in_broker] == ["9984"]
        assert [p.symbol for p in result.only_in_local] == ["6758"]


class TestBrokerFailure:
    def test_照会に失敗したら例外を通す(self) -> None:
        """**「取れなかったからローカルを正とする」は、この装置が
        防ごうとしているものそのもの。**
        """
        with pytest.raises(BrokerError):
            reconcile(_FakeBroker((), error=True), (_position("7203"),))
