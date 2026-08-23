"""全建玉クローズ（安全装置 #2）のテスト。**最重要コードパス。**

デイトレ信用の建玉を当日中に返済し損ねると、翌営業日に強制決済され
**1注文2,200円**（月利目標の約2日分）。

重点は**障害注入**。部分約定・API障害・タイムアウトでも
最終的に建玉が残らないことを確認する。
「発注が通った」ではなく「建玉が消えた」で判定していること。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from autotrader.broker.base import Broker, BrokerError, OrderRejectedError
from autotrader.execution.close_all import close_all
from autotrader.types import (
    AccountState,
    MarginTradeType,
    Order,
    Position,
    Quote,
    Side,
)

NOW = datetime(2026, 6, 1, 14, 50)


def _position(symbol: str, side: Side = Side.LONG) -> Position:
    return Position(
        symbol=symbol,
        side=side,
        quantity=100,
        entry_price=1000.0,
        margin_trade_type=MarginTradeType.DAYTRADE,
        opened_at=NOW,
    )


class _FakeBroker(Broker):
    """建玉のクローズ挙動を注入できるブローカー。

    - ``fail_symbols``: 発注が拒否される銘柄
    - ``survive_rounds``: 発注は通るが N 回目まで建玉が消えない銘柄
      （**部分約定・約定遅延の再現**。「発注成功=クローズ完了」ではない）
    - ``positions_error_rounds``: 建玉照会が失敗するラウンド数
    """

    def __init__(
        self,
        positions: list[Position],
        *,
        fail_symbols: set[str] | None = None,
        survive_rounds: dict[str, int] | None = None,
        positions_error_rounds: int = 0,
    ) -> None:
        self._positions = list(positions)
        self._fail = fail_symbols or set()
        self._survive = dict(survive_rounds or {})
        self._positions_error_rounds = positions_error_rounds
        self.sent: list[Order] = []
        self.get_positions_calls = 0

    def get_account(self) -> AccountState:
        return AccountState(Decimal(500_000), tuple(self._positions), NOW)

    def get_positions(self) -> tuple[Position, ...]:
        self.get_positions_calls += 1
        if self._positions_error_rounds > 0:
            self._positions_error_rounds -= 1
            raise BrokerError("建玉照会に失敗")
        return tuple(self._positions)

    def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError

    def send_order(self, order: Order) -> Order:
        self.sent.append(order)
        if order.symbol in self._fail:
            raise OrderRejectedError(f"{order.symbol} は拒否された")
        remaining = self._survive.get(order.symbol, 0)
        if remaining > 0:
            self._survive[order.symbol] = remaining - 1
            return order  # 発注は通るが建玉は残る
        self._positions = [p for p in self._positions if p.symbol != order.symbol]
        return order

    def cancel_order(self, client_order_id: str) -> None:
        raise NotImplementedError

    def get_orders(self) -> tuple[Order, ...]:
        return tuple(self.sent)

    def is_shortable(self, symbol: str) -> bool:
        return True


class TestNormal:
    def test_全建玉をクローズする(self) -> None:
        broker = _FakeBroker([_position("7203"), _position("6758")])
        result = close_all(broker, "scheduled_close")

        assert result.success
        assert len(result.closed) == 2
        assert result.residual == ()
        assert result.retries == 0

    def test_建玉がなければ何もしない(self) -> None:
        broker = _FakeBroker([])
        result = close_all(broker, "scheduled_close")

        assert result.success
        assert broker.sent == []

    def test_返済注文として送る(self) -> None:
        """新規建てと取り違えると建玉が倍になる。"""
        broker = _FakeBroker([_position("7203", Side.SHORT)])
        close_all(broker, "killswitch")

        order = broker.sent[0]
        assert order.cash_margin.name == "MARGIN_CLOSE"
        assert order.side is Side.SHORT  # 建玉と同じ側を渡す
        assert order.quantity == 100

    def test_理由が注文IDに残る(self) -> None:
        """監査ログで「なぜ閉じたか」が追えること。"""
        broker = _FakeBroker([_position("7203")])
        close_all(broker, "daily_loss")
        assert "daily_loss" in broker.sent[0].client_order_id

    def test_注文IDは銘柄ごとに異なる(self) -> None:
        broker = _FakeBroker([_position("7203"), _position("6758")])
        close_all(broker, "scheduled_close")
        ids = {o.client_order_id for o in broker.sent}
        assert len(ids) == 2


class TestVerification:
    """**「成功したはず」と仮定しない**（docs/05 #2）。"""

    def test_発注が通っても建玉が残れば再試行する(self) -> None:
        """**部分約定・約定遅延の再現。**

        発注は成功を返すが建玉は消えない。「発注が通った」で
        判定していると、ここで残存を見逃す。
        """
        broker = _FakeBroker([_position("7203")], survive_rounds={"7203": 2})
        result = close_all(broker, "scheduled_close", retry_interval_seconds=0)

        assert result.success
        assert result.retries == 2
        assert len(broker.sent) == 3

    def test_毎回建玉を実測する(self) -> None:
        broker = _FakeBroker([_position("7203")], survive_rounds={"7203": 1})
        close_all(broker, "scheduled_close", retry_interval_seconds=0)
        # 初回の対象取得 + 各ラウンドの残存確認
        assert broker.get_positions_calls >= 3

    def test_初回の照会が失敗しても再試行する(self) -> None:
        """**最重要コードパスで「照会できないから何もしない」は許されない。**

        一時的なAPIエラーで全建玉が翌日に持ち越される。
        """
        broker = _FakeBroker([_position("7203")], positions_error_rounds=1)
        result = close_all(
            broker, "scheduled_close", max_retries=2, retry_interval_seconds=0
        )
        assert result.success
        assert not result.positions_unknown

    def test_最後まで照会できなければ不明として失敗させる(self) -> None:
        """**「確認できない」と「確認して0件」は別物。**

        照会できない状態を成功として扱うと、残っているのに気づけない。
        運用側の対応も変わる（前者はAPI疎通の確認、後者は手動クローズ）。
        """
        broker = _FakeBroker([_position("7203")], positions_error_rounds=99)
        result = close_all(
            broker, "scheduled_close", max_retries=2, retry_interval_seconds=0
        )
        assert not result.success
        assert result.positions_unknown
        assert broker.sent == []  # 建玉が分からないので発注もしていない

    def test_返済後の確認が失敗したら成功にしない(self) -> None:
        """発注は通ったが残存を確認できない場合。"""

        class _CheckFails(_FakeBroker):
            def get_positions(self) -> tuple[Position, ...]:
                self.get_positions_calls += 1
                if self.get_positions_calls == 1:
                    return tuple(self._positions)
                raise BrokerError("照会に失敗")

        broker = _CheckFails([_position("7203")])
        result = close_all(
            broker, "scheduled_close", max_retries=1, retry_interval_seconds=0
        )
        assert not result.success
        assert result.positions_unknown


class TestFailure:
    def test_閉じられなければ失敗として返す(self) -> None:
        """**黙って成功にしない。** 翌営業日に1注文2,200円が発生する。"""
        broker = _FakeBroker([_position("7203")], fail_symbols={"7203"})
        result = close_all(
            broker, "scheduled_close", max_retries=2, retry_interval_seconds=0
        )

        assert not result.success
        assert [p.symbol for p in result.residual] == ["7203"]
        assert result.retries == 2

    def test_1銘柄の失敗で他を巻き込まない(self) -> None:
        """**例外を伝播させると残りが手つかずで翌日に持ち越される。**"""
        broker = _FakeBroker(
            [_position("7203"), _position("6758"), _position("9984")],
            fail_symbols={"6758"},
        )
        result = close_all(
            broker, "scheduled_close", max_retries=1, retry_interval_seconds=0
        )

        assert {p.symbol for p in result.closed} == {"7203", "9984"}
        assert [p.symbol for p in result.residual] == ["6758"]
        assert not result.success

    def test_再試行の上限で打ち切る(self) -> None:
        """無限に粘らない。人が対応する必要がある。"""
        broker = _FakeBroker([_position("7203")], fail_symbols={"7203"})
        result = close_all(
            broker, "scheduled_close", max_retries=3, retry_interval_seconds=0
        )
        assert result.retries == 3
        assert len(broker.sent) == 4  # 初回 + 3回


class TestSharedPath:
    """**大引けと緊急停止で同じコードを通す**（docs/05 原則1）。

    滅多に走らないコードは、いざという時に動かない。
    定時クローズで毎営業日約60回実行されることで信頼性を稼ぐ。
    """

    @pytest.mark.parametrize(
        "reason", ["scheduled_close", "killswitch", "daily_loss", "rolling_loss"]
    )
    def test_どの理由でも同じ経路を通る(self, reason: str) -> None:
        broker = _FakeBroker([_position("7203")])
        result = close_all(broker, reason)
        assert result.success
        assert reason in broker.sent[0].client_order_id
