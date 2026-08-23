"""発注（安全装置 #3/#9/#12/#13/#15）のテスト。

**全発注がこのモジュールを通る。** バイパス経路がないこと、
そして検査が「発注（不可逆）より前」に終わっていることを固定する。

重点は**二重発注の防止**。ネットワーク断は必ず起きるので、
「送ったか分からない」状態でどう振る舞うかがすべて。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from autotrader.broker.base import Broker, BrokerError, OrderRejectedError
from autotrader.execution.journal import OrderJournal, OrderState
from autotrader.execution.order import (
    NotShortableError,
    OrderConfig,
    PriceSanityError,
    StopOrderRequiredError,
    check_price_sanity,
    submit,
)
from autotrader.risk.leverage import LeverageViolationError
from autotrader.types import (
    AccountState,
    MarginTradeType,
    Order,
    OrderStatus,
    Position,
    Quote,
    Side,
    Signal,
)

NOW = datetime(2026, 6, 1, 10, 0)


def _account(cash: int = 500_000, *positions: Position) -> AccountState:
    return AccountState(Decimal(cash), positions, NOW)


def _signal(
    side: Side = Side.LONG, stop: float | None = None, symbol: str = "7203"
) -> Signal:
    return Signal(symbol, side, 1.0, "orb", stop_price=stop, take_profit_price=None)


class _FakeBroker(Broker):
    def __init__(
        self,
        last: float = 1000.0,
        *,
        shortable: bool = True,
        fail_times: int = 0,
        fail_stop: bool = False,
        orders_error: bool = False,
        reject: bool = False,
    ) -> None:
        self.last = last
        self._shortable = shortable
        self._fail_times = fail_times
        self._fail_stop = fail_stop
        self._orders_error = orders_error
        self._reject = reject
        self.sent: list[Order] = []

    def get_account(self) -> AccountState:
        return _account()

    def get_positions(self) -> tuple[Position, ...]:
        return ()

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol, NOW, self.last - 1, self.last + 1, self.last, 100, 100)

    def send_order(self, order: Order) -> Order:
        if self._fail_stop and order.order_type.value == "stop":
            raise BrokerError("ストップ注文が通らない")
        if self._reject:
            raise OrderRejectedError("証券会社に拒否された")
        if self._fail_times > 0:
            self._fail_times -= 1
            raise BrokerError("ネットワーク断")
        self.sent.append(order)
        return Order(
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            order_type=order.order_type,
            cash_margin=order.cash_margin,
            margin_trade_type=order.margin_trade_type,
            trigger_price=order.trigger_price,
            broker_order_id=f"B-{order.client_order_id}",
            status=OrderStatus.FILLED,
        )

    def cancel_order(self, client_order_id: str) -> None:
        raise NotImplementedError

    def get_orders(self) -> tuple[Order, ...]:
        if self._orders_error:
            raise BrokerError("注文一覧を取得できない")
        return tuple(self.sent)

    def is_shortable(self, symbol: str) -> bool:
        return self._shortable


@pytest.fixture
def journal(tmp_path: Path) -> OrderJournal:
    return OrderJournal(tmp_path / "orders.db")


class TestPriceSanity:
    """#13 価格サニティ。誤データ・誤発注を入口で止める。"""

    def test_許容範囲なら通る(self) -> None:
        check_price_sanity(1100.0, 1000.0)

    def test_乖離が大きければ拒否する(self) -> None:
        with pytest.raises(PriceSanityError, match="乖離"):
            check_price_sanity(1160.0, 1000.0)
        with pytest.raises(PriceSanityError):
            check_price_sanity(840.0, 1000.0)

    def test_境界(self) -> None:
        check_price_sanity(1150.0, 1000.0)  # +15.0%
        with pytest.raises(PriceSanityError):
            check_price_sanity(1150.1, 1000.0)

    def test_ゼロや負値を拒否する(self) -> None:
        """**データ欠損が0として流れてくると、サイジングが無限大を要求する。**"""
        with pytest.raises(PriceSanityError, match="0以下"):
            check_price_sanity(0.0, 1000.0)
        with pytest.raises(PriceSanityError):
            check_price_sanity(-100.0, 1000.0)

    def test_前日終値がゼロなら比較しない(self) -> None:
        with pytest.raises(PriceSanityError, match="前日終値"):
            check_price_sanity(1000.0, 0.0)


class TestSubmitChecks:
    """**発注（不可逆）より前にすべての検査を終える。**"""

    def test_正常系(self, journal: OrderJournal) -> None:
        broker = _FakeBroker()
        order = submit(
            broker, _account(), _signal(), 100,
            prev_close=1000.0, journal=journal, client_order_id="o1",
        )
        assert order.broker_order_id == "B-o1"
        assert journal.get("o1").state is OrderState.SENT  # type: ignore[union-attr]

    def test_価格が異常なら発注しない(self, journal: OrderJournal) -> None:
        broker = _FakeBroker(last=2000.0)
        with pytest.raises(PriceSanityError):
            submit(
                broker, _account(), _signal(), 100,
                prev_close=1000.0, journal=journal, client_order_id="o1",
            )
        assert broker.sent == []
        assert journal.get("o1") is None  # 予約もしていない

    def test_売建できない銘柄は拒否する(self, journal: OrderJournal) -> None:
        """#12。一般信用の在庫は日々変わる。"""
        broker = _FakeBroker(shortable=False)
        with pytest.raises(NotShortableError):
            submit(
                broker, _account(), _signal(Side.SHORT, stop=1050.0), 100,
                prev_close=1000.0, journal=journal, client_order_id="o1",
            )
        assert broker.sent == []

    def test_ストップのないショートを拒否する(self, journal: OrderJournal) -> None:
        """#3。空売りは理論上損失無限大。"""
        broker = _FakeBroker()
        with pytest.raises(StopOrderRequiredError):
            submit(
                broker, _account(), _signal(Side.SHORT), 100,
                prev_close=1000.0, journal=journal, client_order_id="o1",
            )
        assert broker.sent == []

    def test_レバレッジ違反を拒否する(self, journal: OrderJournal) -> None:
        """#1。**バイパス経路を作らない。**"""
        broker = _FakeBroker()
        with pytest.raises(LeverageViolationError):
            submit(
                broker, _account(), _signal(), 600,  # 60万円 > 現金50万
                prev_close=1000.0, journal=journal, client_order_id="o1",
            )
        assert broker.sent == []
        assert journal.get("o1") is None


class TestIdempotency:
    """#9 冪等性。**ネットワーク断は必ず起きる。**"""

    def test_発注前に予約する(self, journal: OrderJournal) -> None:
        """**発注後に書く設計だと、落ちた瞬間の注文だけが記録から漏れる** —
        そしてそれが二重発注になる。
        """
        broker = _FakeBroker(fail_times=99)
        with pytest.raises(BrokerError):
            submit(
                broker, _account(), _signal(), 100,
                prev_close=1000.0, journal=journal, client_order_id="o1",
                config=OrderConfig(max_retries=0),
            )
        entry = journal.get("o1")
        assert entry is not None
        assert entry.state is OrderState.RESERVED  # 「送ったか不明」

    def test_一時的な失敗からリトライで回復する(self, journal: OrderJournal) -> None:
        broker = _FakeBroker(fail_times=2)
        order = submit(
            broker, _account(), _signal(), 100,
            prev_close=1000.0, journal=journal, client_order_id="o1",
        )
        assert order.broker_order_id == "B-o1"
        assert len(broker.sent) == 1  # **二重に送っていない**

    def test_既に送信済みなら再発注しない(self, journal: OrderJournal) -> None:
        """リトライ時に証券会社へ照会して確認する。"""
        broker = _FakeBroker()
        submit(
            broker, _account(), _signal(), 100,
            prev_close=1000.0, journal=journal, client_order_id="o1",
        )
        # 同じIDでもう一度（記録も残っている）
        broker._fail_times = 1  # noqa: SLF001
        submit(
            broker, _account(), _signal(), 100,
            prev_close=1000.0, journal=journal, client_order_id="o1",
        )
        assert len(broker.sent) == 1

    def test_照会できないときは再発注しない(self, journal: OrderJournal) -> None:
        """**「送ったか分からない」状態でもう一度送るのが二重発注そのもの。**

        記録は RESERVED のまま残り、起動時に人の目に触れる。
        """
        broker = _FakeBroker(fail_times=99, orders_error=True)
        with pytest.raises(BrokerError):
            submit(
                broker, _account(), _signal(), 100,
                prev_close=1000.0, journal=journal, client_order_id="o1",
            )
        assert len(broker.sent) == 0
        assert journal.get("o1").state is OrderState.RESERVED  # type: ignore[union-attr]
        assert [e.client_order_id for e in journal.unresolved()] == ["o1"]

    def test_拒否は記録して例外にする(self, journal: OrderJournal) -> None:
        broker = _FakeBroker(reject=True)
        with pytest.raises(OrderRejectedError):
            submit(
                broker, _account(), _signal(), 100,
                prev_close=1000.0, journal=journal, client_order_id="o1",
            )
        assert journal.get("o1").state is OrderState.REJECTED  # type: ignore[union-attr]


class TestShortStop:
    """#3 ショートには必ずストップをセットで付ける。"""

    def test_ストップ注文もセットで送る(self, journal: OrderJournal) -> None:
        broker = _FakeBroker()
        submit(
            broker, _account(), _signal(Side.SHORT, stop=1050.0), 100,
            prev_close=1000.0, journal=journal, client_order_id="o1",
        )
        assert [o.order_type.value for o in broker.sent] == ["market", "stop"]
        assert broker.sent[1].trigger_price == 1050.0

    def test_ストップに失敗したら建玉を閉じる(self, journal: OrderJournal) -> None:
        """**ストップのないショート建玉を保持するくらいなら、
        コストを払って閉じるほうが安い。**
        """
        broker = _FakeBroker(fail_stop=True)
        with pytest.raises(StopOrderRequiredError):
            submit(
                broker, _account(), _signal(Side.SHORT, stop=1050.0), 100,
                prev_close=1000.0, journal=journal, client_order_id="o1",
            )
        # 新規建て → 緊急クローズ の2件が送られている
        assert [o.cash_margin.value for o in broker.sent] == [2, 3]
        assert journal.get("o1-emergency-close").state is OrderState.SENT  # type: ignore[union-attr]


class TestAuditLog:
    """#15 監査ログ。**事後に完全再現できる粒度。**"""

    def test_判断材料を残す(self, journal: OrderJournal) -> None:
        broker = _FakeBroker()
        submit(
            broker, _account(), _signal(stop=950.0), 100,
            prev_close=1000.0, journal=journal, client_order_id="o1",
        )
        context = journal.get("o1").context  # type: ignore[union-attr]
        assert context["reason"] == "orb"
        assert context["prev_close"] == 1000.0
        assert context["quote_last"] == 1000.0
        assert "cash" in context and "leverage" in context

    def test_注文の内容を残す(self, journal: OrderJournal) -> None:
        broker = _FakeBroker()
        submit(
            broker, _account(), _signal(), 100,
            prev_close=1000.0, journal=journal, client_order_id="o1",
        )
        payload = journal.get("o1").payload  # type: ignore[union-attr]
        assert payload["symbol"] == "7203"
        assert payload["quantity"] == 100
        assert payload["margin_trade_type"] == MarginTradeType.DAYTRADE.value
