"""ReplayBroker（Stage A の約定モデル）のテスト。

重点は3つ。

1. **レバレッジ1倍が必ず強制されること** — バイパス経路がないこと
2. **ショートがストップなしで作れないこと**（安全装置 #3）
3. **約定が常に不利な側にずれること** — 楽観的な約定モデルにしない
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.broker.base import BrokerError, OrderRejectedError
from autotrader.broker.replay import (
    THIN_SLIPPAGE_PENALTY_BPS,
    ReplayBroker,
)
from autotrader.types import Bar, Side

T0 = datetime(2026, 6, 1, 9, 0)


def _bar(
    code: str,
    minute: int,
    open_: float = 1000.0,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    turnover: float = 2_000_000_000.0,
) -> Bar:
    return Bar(
        symbol=code,
        timestamp=T0 + timedelta(minutes=minute),
        open=open_,
        high=high if high is not None else open_ * 1.02,
        low=low if low is not None else open_ * 0.98,
        close=close if close is not None else open_,
        volume=10_000,
        turnover=turnover,
    )


def _broker(
    cash: int = 500_000,
    turnover: float = 2_000_000_000.0,
    prices: tuple[float, ...] = (1000.0, 1000.0, 1000.0),
) -> ReplayBroker:
    bars = {
        "7203": tuple(
            _bar("7203", i * 5, open_=p, turnover=turnover) for i, p in enumerate(prices)
        )
    }
    return ReplayBroker(Decimal(cash), bars)


class TestConstruction:
    def test_スリッページゼロを拒否する(self) -> None:
        """**手数料が0でもコストは0ではない**（CLAUDE.md 規約5）。"""
        with pytest.raises(ValueError, match="0以下"):
            ReplayBroker(Decimal(500_000), {}, slippage_bps=0.0)

    def test_資金ゼロを拒否する(self) -> None:
        with pytest.raises(ValueError):
            ReplayBroker(Decimal(0), {})

    def test_時刻の列を昇順で持つ(self) -> None:
        broker = _broker()
        assert broker.timeline == (T0, T0 + timedelta(minutes=5), T0 + timedelta(minutes=10))
        assert broker.now == T0


class TestClock:
    def test_1バーずつ進む(self) -> None:
        broker = _broker()
        assert broker.advance()
        assert broker.now == T0 + timedelta(minutes=5)
        assert broker.advance()
        assert not broker.advance()
        assert broker.exhausted

    def test_バーのない銘柄はNoneを返す(self) -> None:
        """**前のバーで代用しない。** 存在しない価格で約定させることになる。"""
        assert _broker().current_bar("9999") is None


class TestFillPrice:
    def test_買いは高く売りは安く約定する(self) -> None:
        broker = _broker()
        bar = _bar("7203", 0, open_=1000.0)
        # 20bps = 2円
        assert broker.fill_price("7203", bar, Side.LONG, opening=True) == pytest.approx(1002.0)
        assert broker.fill_price("7203", bar, Side.LONG, opening=False) == pytest.approx(998.0)
        assert broker.fill_price("7203", bar, Side.SHORT, opening=True) == pytest.approx(998.0)
        assert broker.fill_price("7203", bar, Side.SHORT, opening=False) == pytest.approx(1002.0)

    def test_バーのレンジを超えては約定しない(self) -> None:
        """観測された高値を超える価格では実際には約定しえない。"""
        broker = _broker()
        narrow = _bar("7203", 0, open_=1000.0, high=1000.5, low=999.5)
        assert broker.fill_price("7203", narrow, Side.LONG, opening=True) == 1000.5
        assert broker.fill_price("7203", narrow, Side.SHORT, opening=True) == 999.5

    def test_薄い銘柄には厚いスリッページを当てる(self) -> None:
        """流動性下限を下げるぶん、約定モデルは逆に厳しくする。"""
        thick = _broker(turnover=2_000_000_000.0)
        thin = _broker(turnover=300_000_000.0)
        assert thin.slippage_bps_for("7203") == thick.slippage_bps_for("7203") + (
            THIN_SLIPPAGE_PENALTY_BPS
        )


class TestLeverage:
    """**バイパス経路がないことの確認。** ここが本システムの安全性の土台。"""

    def test_残高内なら約定する(self) -> None:
        broker = _broker(cash=500_000)
        broker.market_order("o1", "7203", Side.LONG, 400, opening=True)  # 約40万円
        assert len(broker.get_positions()) == 1

    def test_残高を超えたら拒否する(self) -> None:
        broker = _broker(cash=500_000)
        with pytest.raises(OrderRejectedError, match="レバレッジ"):
            broker.market_order("o1", "7203", Side.LONG, 600, opening=True)  # 約60万円
        assert broker.get_positions() == ()

    def test_既存建玉を合算して判定する(self) -> None:
        bars = {
            code: (_bar(code, 0, open_=1000.0),) for code in ("7203", "6758")
        }
        broker = ReplayBroker(Decimal(500_000), bars)
        broker.market_order("o1", "7203", Side.LONG, 300, opening=True)  # 約30万円
        with pytest.raises(OrderRejectedError, match="レバレッジ"):
            broker.market_order("o2", "6758", Side.LONG, 300, opening=True)  # 合計60万円

    def test_ショートも建玉総額に含める(self) -> None:
        """両建てでもリスクは合算される。相殺してはならない。"""
        bars = {code: (_bar(code, 0, open_=1000.0),) for code in ("7203", "6758")}
        broker = ReplayBroker(Decimal(500_000), bars)
        broker.market_order("o1", "7203", Side.LONG, 300, opening=True)
        with pytest.raises(OrderRejectedError, match="レバレッジ"):
            broker.market_order("o2", "6758", Side.SHORT, 300, opening=True, stop_price=1050.0)


class TestShortSafety:
    def test_ストップなしのショートを拒否する(self) -> None:
        """**空売りは理論上損失無限大**（docs/05-risk-management.md #3）。"""
        broker = _broker()
        with pytest.raises(OrderRejectedError, match="ストップ"):
            broker.market_order("o1", "7203", Side.SHORT, 100, opening=True)
        assert broker.get_positions() == ()

    def test_ストップつきなら建てられる(self) -> None:
        broker = _broker()
        broker.market_order("o1", "7203", Side.SHORT, 100, opening=True, stop_price=1050.0)
        position = broker.get_positions()[0]
        assert position.side is Side.SHORT
        assert position.stop_order_id is not None

    def test_ロングにはストップを要求しない(self) -> None:
        """損失は投下資金までで、無限大にはならない。"""
        broker = _broker()
        broker.market_order("o1", "7203", Side.LONG, 100, opening=True)
        assert broker.get_positions()[0].stop_order_id is None


class TestOrders:
    def test_冪等である(self) -> None:
        """**同じIDで二度呼ばれても建玉は1つ**（docs/05-risk-management.md #9）。"""
        broker = _broker()
        first = broker.market_order("o1", "7203", Side.LONG, 100, opening=True)
        second = broker.market_order("o1", "7203", Side.LONG, 100, opening=True)
        assert first == second
        assert len(broker.get_positions()) == 1

    def test_同一銘柄の重複建玉を拒否する(self) -> None:
        broker = _broker()
        broker.market_order("o1", "7203", Side.LONG, 100, opening=True)
        with pytest.raises(OrderRejectedError, match="既に建玉"):
            broker.market_order("o2", "7203", Side.LONG, 100, opening=True)

    def test_建玉のない返済を拒否する(self) -> None:
        broker = _broker()
        with pytest.raises(OrderRejectedError, match="返済する建玉がない"):
            broker.market_order("o1", "7203", Side.LONG, 100, opening=False)

    def test_部分返済を拒否する(self) -> None:
        """黙って全部返済すると、建玉数の想定がずれたまま進む。"""
        broker = _broker()
        broker.market_order("o1", "7203", Side.LONG, 200, opening=True)
        with pytest.raises(OrderRejectedError, match="部分返済"):
            broker.market_order("o2", "7203", Side.LONG, 100, opening=False)

    def test_バーのない銘柄への発注を拒否する(self) -> None:
        broker = _broker()
        with pytest.raises(OrderRejectedError, match="バーがない"):
            broker.market_order("o1", "9999", Side.LONG, 100, opening=True)

    def test_数量ゼロを拒否する(self) -> None:
        broker = _broker()
        with pytest.raises(OrderRejectedError):
            broker.market_order("o1", "7203", Side.LONG, 0, opening=True)

    def test_約定済みの取消は黙って成功しない(self) -> None:
        """「取り消せたはず」と誤解させない。"""
        broker = _broker()
        broker.market_order("o1", "7203", Side.LONG, 100, opening=True)
        with pytest.raises(BrokerError, match="約定済み"):
            broker.cancel_order("o1")


class TestRoundTrip:
    def test_ロングの往復で損益が確定する(self) -> None:
        broker = _broker(prices=(1000.0, 1100.0))
        broker.market_order("open", "7203", Side.LONG, 100, opening=True)
        broker.advance()
        broker.market_order("close", "7203", Side.LONG, 100, opening=False)

        trade = broker.trades[0]
        # 建値 1000*1.002 = 1002.0 / 返済 1100*0.998 = 1097.8
        assert trade.entry_price == pytest.approx(1002.0)
        assert trade.exit_price == pytest.approx(1097.8)
        assert trade.pnl == pytest.approx((1097.8 - 1002.0) * 100)
        assert broker.get_positions() == ()

    def test_往復コストが必ず引かれる(self) -> None:
        """値動きゼロなら**必ず負け**になる。ここが正なら約定モデルが甘い。"""
        broker = _broker(prices=(1000.0, 1000.0))
        broker.market_order("open", "7203", Side.LONG, 100, opening=True)
        broker.advance()
        broker.market_order("close", "7203", Side.LONG, 100, opening=False)
        assert broker.trades[0].pnl < 0

    def test_ショートは値下がりで勝つ(self) -> None:
        broker = _broker(prices=(1000.0, 900.0))
        broker.market_order("open", "7203", Side.SHORT, 100, opening=True, stop_price=1050.0)
        broker.advance()
        broker.market_order("close", "7203", Side.SHORT, 100, opening=False)
        assert broker.trades[0].pnl > 0

    def test_実現損益が現金に反映される(self) -> None:
        broker = _broker(prices=(1000.0, 1100.0))
        broker.market_order("open", "7203", Side.LONG, 100, opening=True)
        broker.advance()
        broker.market_order("close", "7203", Side.LONG, 100, opening=False)
        assert broker.cash == Decimal(500_000) + Decimal(str(broker.trades[0].pnl))


class TestEquity:
    def test_含み損益を評価に含める(self) -> None:
        """実現損益だけを追うとドローダウンを過小評価する。"""
        broker = _broker(prices=(1000.0, 900.0))
        broker.market_order("open", "7203", Side.LONG, 100, opening=True)
        broker.advance()
        assert broker.equity() < 500_000

    def test_建玉がなければ現金と一致する(self) -> None:
        assert _broker().equity() == pytest.approx(500_000.0)


class TestQuote:
    def test_板がないので合成する(self) -> None:
        broker = _broker()
        quote = broker.get_quote("7203")
        assert quote.bid < quote.last < quote.ask

    def test_厚みは未知として0を返す(self) -> None:
        """**0 を「板が空」と解釈してはならない。** 取得できないという意味。"""
        quote = _broker().get_quote("7203")
        assert quote.bid_size == 0 and quote.ask_size == 0

    def test_バーがなければ例外(self) -> None:
        with pytest.raises(BrokerError):
            _broker().get_quote("9999")


class TestShortable:
    """**売建可否は約定バーから導出しない。**

    5分足で「直近20本の平均売買代金」を取ると100分ぶんの値になり、
    日次の閾値（10億円）と単位が食い違う。実際にこれで
    **ショートが1件も出ないのにエラーも出ない**という失敗をした。
    判定は日次データを持つ呼び出し側の責務にしてある。
    """

    def _with(self, shortable: frozenset[str] | None) -> ReplayBroker:
        bars = {"7203": (_bar("7203", 0, open_=1000.0),)}
        return ReplayBroker(Decimal(500_000), bars, shortable=shortable)

    def test_渡された集合で判定する(self) -> None:
        assert self._with(frozenset({"7203"})).is_shortable("7203")
        assert not self._with(frozenset({"6758"})).is_shortable("7203")

    def test_省略すると1銘柄も売建できない(self) -> None:
        """**保守的な側に倒す**（CLAUDE.md 規約5）。

        「全部売建できる」を既定にすると、渡し忘れたときに
        実際には建てられない銘柄で成績が出てしまう。
        """
        assert not self._with(None).is_shortable("7203")

    def test_知らない銘柄は売建不可(self) -> None:
        assert not self._with(frozenset({"7203"})).is_shortable("9999")

    def test_ストップつきでも売建不可なら建てられない(self) -> None:
        """売建可否のチェックは engine 側。broker は判定を提供するだけ。"""
        broker = self._with(frozenset())
        assert not broker.is_shortable("7203")
