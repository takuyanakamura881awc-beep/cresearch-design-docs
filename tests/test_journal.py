"""発注記録（安全装置 #9 と #15）のテスト。

**冪等性と監査ログを1つのテーブルで担う。**
別々に持つと、片方だけ書けた状態が生まれて整合が取れなくなる。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autotrader.execution.journal import OrderJournal, OrderState
from autotrader.types import CashMargin, MarginTradeType, Order, OrderType, Side


def _order(client_order_id: str = "o1", symbol: str = "7203") -> Order:
    return Order(
        client_order_id=client_order_id,
        symbol=symbol,
        side=Side.LONG,
        quantity=100,
        order_type=OrderType.MARKET,
        cash_margin=CashMargin.MARGIN_OPEN,
        margin_trade_type=MarginTradeType.DAYTRADE,
        trigger_price=950.0,
    )


@pytest.fixture
def journal(tmp_path: Path) -> OrderJournal:
    return OrderJournal(tmp_path / "orders.db")


class TestReserve:
    def test_予約して読み戻せる(self, journal: OrderJournal) -> None:
        journal.reserve(_order(), {"reason": "orb"})
        entry = journal.get("o1")

        assert entry is not None
        assert entry.state is OrderState.RESERVED
        assert entry.symbol == "7203"
        assert entry.context["reason"] == "orb"

    def test_同じIDで二度予約できない(self, journal: OrderJournal) -> None:
        """**二重発注を構造的に防ぐ土台。**

        黙って上書きすると最初の記録が消え、追跡できなくなる。
        """
        journal.reserve(_order(), {})
        with pytest.raises(ValueError, match="既に予約済み"):
            journal.reserve(_order(), {})

    def test_注文の内容を残す(self, journal: OrderJournal) -> None:
        """#15。**事後に完全再現できる粒度。**"""
        journal.reserve(_order(), {})
        payload = journal.get("o1").payload  # type: ignore[union-attr]

        assert payload["quantity"] == 100
        assert payload["trigger_price"] == 950.0
        assert payload["margin_trade_type"] == "daytrade"

    def test_知らないIDはNone(self, journal: OrderJournal) -> None:
        assert journal.get("unknown") is None


class TestMark:
    def test_状態を更新する(self, journal: OrderJournal) -> None:
        journal.reserve(_order(), {})
        journal.mark("o1", OrderState.SENT, broker_order_id="B-1")

        entry = journal.get("o1")
        assert entry is not None
        assert entry.state is OrderState.SENT
        assert entry.broker_order_id == "B-1"

    def test_予約なしに状態を書けない(self, journal: OrderJournal) -> None:
        """**書けてしまうと「発注前に予約する」という規律が形骸化する。**"""
        with pytest.raises(ValueError, match="予約されていない"):
            journal.mark("o1", OrderState.SENT)

    def test_理由を残せる(self, journal: OrderJournal) -> None:
        journal.reserve(_order(), {})
        journal.mark("o1", OrderState.REJECTED, note="残高不足")
        assert journal.get("o1").note == "残高不足"  # type: ignore[union-attr]


class TestUnresolved:
    """**RESERVED は「送っていない」ではなく「送ったか不明」。**"""

    def test_送信済みは含まない(self, journal: OrderJournal) -> None:
        journal.reserve(_order("o1"), {})
        journal.reserve(_order("o2"), {})
        journal.mark("o1", OrderState.SENT)

        assert [e.client_order_id for e in journal.unresolved()] == ["o2"]

    def test_拒否済みも含まない(self, journal: OrderJournal) -> None:
        """拒否は確定した失敗。再発注してよい。"""
        journal.reserve(_order("o1"), {})
        journal.mark("o1", OrderState.REJECTED)
        assert journal.unresolved() == ()

    def test_空なら空(self, journal: OrderJournal) -> None:
        assert journal.unresolved() == ()


class TestPersistence:
    def test_プロセスをまたいで残る(self, tmp_path: Path) -> None:
        """**発注前に永続化する意味がここにある。**

        再起動後に「送ったかもしれない注文」を列挙できないと、
        まさに落ちた瞬間の注文が二重発注になる。
        """
        path = tmp_path / "orders.db"
        OrderJournal(path).reserve(_order(), {"reason": "orb"})

        reopened = OrderJournal(path)
        entry = reopened.get("o1")
        assert entry is not None
        assert entry.state is OrderState.RESERVED

    def test_全記録を取り出せる(self, journal: OrderJournal) -> None:
        """日次レポートと事後検証に使う。"""
        journal.reserve(_order("o1"), {})
        journal.reserve(_order("o2"), {})
        assert len(journal.all_entries()) == 2
