"""ヒストリカル・リプレイ Broker。**Stage A の検証基盤。**

蓄積済みのバーを時系列に再生し、``Broker`` インターフェースを満たす。
**証券口座もAPIも不要**でバックテストが回る。

【約定モデル — 板がない場合の扱い】

板情報（bid/ask/厚み）が取れないため、バーの OHLCV から約定を推定する。

**原則: 検証できないものは必ず保守的な側（成績が悪くなる側）に倒す。**

=================  ==========================================================
注文               約定の仮定
=================  ==========================================================
成行               **そのバーの始値 + スリッページ**。不利な側にずらしたうえで
                   バーのレンジ [安値, 高値] に収める
指値               バーのレンジが指値を通過した場合のみ約定
スリッページ       **厚めに見積もる**（Stage A は片道20bps。Stage B は10bps）
=================  ==========================================================

始値を基準にするのは、**発注した瞬間に実際に出ていた価格がそれだから**。
「バーの高値で約定する」とすると、発注から5分待ってから最悪値で約定する
という現実にない遅延をモデル化することになり、保守的というより不正確になる。
一方、観測された高値を超える価格では約定しえないので、レンジで頭を抑える。

**楽観的な約定モデルを使わない。**
バックテストで勝つ戦略を作るのは簡単だが、それは目的ではない。
Stage B で実測スリッページに置き換えたときに成績が崩れるなら、
それは Stage A のモデルが甘かったということ（docs/09-data-sources.md §4）。

【レバレッジ1倍はここで強制する】

``send_order`` は必ず ``risk.leverage.enforce`` を通る。
バックテストだけがチェックを迂回すると、**実運用では発注できない建玉で
成績を出す**ことになり、結果が再現しない（CLAUDE.md 規約1）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from autotrader.broker.base import Broker, BrokerError, OrderRejectedError
from autotrader.risk.leverage import LeverageViolationError, enforce
from autotrader.risk.sizing import average_turnover_of
from autotrader.types import (
    AccountState,
    Bar,
    CashMargin,
    MarginTradeType,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
    Trade,
)

logger = logging.getLogger(__name__)

STAGE_A_SLIPPAGE_BPS = 20.0
"""Stage A の片道スリッページ（bps）。板がないぶん Stage B（10bps）より厚い。"""

THIN_TURNOVER_YEN = Decimal(500_000_000)
"""これを下回る売買代金の銘柄には追加スリッページを当てる。"""

THIN_SLIPPAGE_PENALTY_BPS = 5.0
"""薄い銘柄への追加スリッページ（bps）。

流動性下限を10億円から下げるぶん、**モデルは逆に厳しくする**。
薄い銘柄で成績が出るなら、それがコストに耐える戦略かをここで先に問う。
"""

SHORTABLE_MIN_TURNOVER_YEN = Decimal(1_000_000_000)
"""Stage A で「売建できる」とみなす売買代金の下限。**これは代理指標。**"""


class ReplayBroker(Broker):
    """蓄積済みバーを再生する検証用 Broker。

    時刻は ``advance()` で1バーずつ進む。``send_order`` は
    「いま開始したバー」の始値を基準に約定する。

    **1銘柄1建玉。** 同一銘柄の追加建ては拒否する。
    50万円という資金では同時5銘柄が上限で、同一銘柄を積み増す余地がない。
    許すと建玉管理が複雑になるだけで、得るものがない。
    """

    def __init__(
        self,
        initial_cash: Decimal,
        bars: Mapping[str, tuple[Bar, ...]],
        slippage_bps: float = STAGE_A_SLIPPAGE_BPS,
    ) -> None:
        """
        Args:
            initial_cash: 架空の初期資金（円）。
            bars: 銘柄コード → バー列（時刻の昇順）。
            slippage_bps: 片道スリッページ（bps）。
                **板がないので Stage B（10bps）より厚く見積もる。**

        Raises:
            ValueError: スリッページが0以下、または資金が0以下の場合。
        """
        if slippage_bps <= 0:
            raise ValueError(
                "スリッページを0以下にしてはならない。"
                "手数料が0でもコストは0ではない（CLAUDE.md 規約5）"
            )
        if initial_cash <= 0:
            raise ValueError(f"初期資金は正の値である必要がある: {initial_cash}")

        self._bars = dict(bars)
        self._slippage_bps = slippage_bps
        self._initial_cash = initial_cash
        self._cash = initial_cash

        # 時刻 → 銘柄 → バー。同じ時刻の複数銘柄をまとめて引けるようにする
        self._by_time: dict[datetime, dict[str, Bar]] = {}
        for code, series in self._bars.items():
            for bar in series:
                self._by_time.setdefault(bar.timestamp, {})[code] = bar
        self._timeline: tuple[datetime, ...] = tuple(sorted(self._by_time))
        self._index = 0

        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._trades: list[Trade] = []
        self._reasons: dict[str, str] = {}
        """注文ID → 手仕舞い理由。

        実際の証券会社は「なぜ」を知らないので ``Order`` には持たせず、
        リプレイ側で記録する。`Trade.exit_reason` は監査とデバッグに要る。
        """
        self._turnover_cache: dict[str, Decimal | None] = {}

    # ------------------------------------------------------------------
    # 時計
    # ------------------------------------------------------------------

    @property
    def now(self) -> datetime:
        """現在時刻（いま開始したバーのタイムスタンプ）。"""
        if not self._timeline:
            raise BrokerError("バーが1本もない。再生できない")
        return self._timeline[min(self._index, len(self._timeline) - 1)]

    @property
    def timeline(self) -> tuple[datetime, ...]:
        """再生する時刻の列。昇順。"""
        return self._timeline

    @property
    def exhausted(self) -> bool:
        return self._index >= len(self._timeline)

    def advance(self) -> bool:
        """時刻を1バー進める。

        Returns:
            まだバーが残っていれば True。
        """
        self._index += 1
        return not self.exhausted

    def current_bar(self, symbol: str) -> Bar | None:
        """いま開始したバー。その銘柄に当該時刻のバーがなければ ``None``。

        **売買停止などでバーが欠ける銘柄がある。** 欠けているのに
        前のバーで代用すると、存在しない価格で約定させることになる。
        """
        if self.exhausted:
            return None
        return self._by_time.get(self.now, {}).get(symbol)

    # ------------------------------------------------------------------
    # 約定価格
    # ------------------------------------------------------------------

    def slippage_bps_for(self, symbol: str) -> float:
        """その銘柄に当てるスリッページ（bps）。

        薄い銘柄には上乗せする。流動性下限を下げたぶん、
        **約定モデルは逆に厳しくする**（CLAUDE.md 規約5）。
        """
        turnover = self._average_turnover(symbol)
        if turnover is not None and turnover < THIN_TURNOVER_YEN:
            return self._slippage_bps + THIN_SLIPPAGE_PENALTY_BPS
        return self._slippage_bps

    def _average_turnover(self, symbol: str) -> Decimal | None:
        if symbol not in self._turnover_cache:
            self._turnover_cache[symbol] = average_turnover_of(
                self._bars.get(symbol, ())
            )
        return self._turnover_cache[symbol]

    def fill_price(self, symbol: str, bar: Bar, side: Side, *, opening: bool) -> float:
        """成行の約定価格。

        始値を基準に**不利な側**へスリッページぶんずらし、
        観測されたレンジ [安値, 高値] で頭を抑える
        （その日そのバーで実際に付いていない価格では約定しえない）。
        """
        rate = self.slippage_bps_for(symbol) / 10_000.0
        buying = (side is Side.LONG) == opening
        if buying:
            return min(bar.open * (1 + rate), bar.high)
        return max(bar.open * (1 - rate), bar.low)

    # ------------------------------------------------------------------
    # Broker インターフェース
    # ------------------------------------------------------------------

    @property
    def cash(self) -> Decimal:
        """現金残高。**実現損益のみを反映する**（含み損益は含めない）。"""
        return self._cash

    @property
    def trades(self) -> tuple[Trade, ...]:
        """手仕舞い済みのトレード。成績集計に使う。"""
        return tuple(self._trades)

    def get_account(self) -> AccountState:
        return AccountState(
            cash=self._cash,
            positions=self.get_positions(),
            as_of=self.now,
        )

    def get_positions(self) -> tuple[Position, ...]:
        return tuple(self._positions[code] for code in sorted(self._positions))

    def equity(self) -> float:
        """現金 + 含み損益。エクイティカーブに使う。

        **含み損益は現在のバーの終値で評価する。** 建玉を持ったまま
        バーをまたぐので、実現損益だけを追うとドローダウンを過小評価する。
        """
        total = float(self._cash)
        for code, position in self._positions.items():
            bar = self.current_bar(code)
            if bar is None:
                continue
            diff = bar.close - position.entry_price
            if position.side is Side.SHORT:
                diff = -diff
            total += diff * position.quantity
        return total

    def get_quote(self, symbol: str) -> Quote:
        """気配を返す。

        **板がないため、bid/ask はバーの終値からスプレッドを推定して合成する。**
        実際の板ではない。厚み（``bid_size`` / ``ask_size``）は取得できないので
        0 を返す。**0 を「板が空」と解釈してはならない**（未知という意味）。
        """
        bar = self.current_bar(symbol)
        if bar is None:
            raise BrokerError(f"{symbol} は {self.now} 時点のバーがない")
        half = bar.close * self.slippage_bps_for(symbol) / 10_000.0
        return Quote(
            symbol=symbol,
            timestamp=bar.timestamp,
            bid=bar.close - half,
            ask=bar.close + half,
            last=bar.close,
            bid_size=0,
            ask_size=0,
        )

    def send_order(self, order: Order) -> Order:
        """注文を送信する。

        **冪等。** 同じ ``client_order_id`` で二度呼ばれても建玉は1つしかできない
        （docs/05-risk-management.md #9）。

        Raises:
            OrderRejectedError: レバレッジ上限違反、バーがない、
                ショートにストップがない、同一銘柄の重複建玉、
                返済対象の建玉がない場合。
        """
        existing = self._orders.get(order.client_order_id)
        if existing is not None:
            logger.debug("既に送信済みの注文: %s", order.client_order_id)
            return existing

        bar = self.current_bar(order.symbol)
        if bar is None:
            raise OrderRejectedError(
                f"{order.symbol} は {self.now} 時点のバーがない。約定価格を決められない"
            )
        if order.quantity <= 0:
            raise OrderRejectedError(f"数量が0以下: {order.quantity}")

        if order.cash_margin is CashMargin.MARGIN_CLOSE:
            filled = self._close_position(order, bar)
        else:
            filled = self._open_position(order, bar)

        self._orders[order.client_order_id] = filled
        return filled

    def _open_position(self, order: Order, bar: Bar) -> Order:
        if order.symbol in self._positions:
            raise OrderRejectedError(
                f"{order.symbol} は既に建玉がある。1銘柄1建玉に限る"
            )

        # #3 空売りの逆指値ストップ必須。
        # 実運用では「新規建て」と「ストップ発注」は別の2回のAPI呼び出しで、
        # ストップ発注に失敗したら建玉を即座に閉じる（risk.yaml の
        # close_on_stop_order_failure）。リプレイには部分失敗のモードがないので、
        # ここでは不可分な操作としてモデル化する。
        if order.side is Side.SHORT and order.trigger_price is None:
            raise OrderRejectedError(
                f"{order.symbol}: ショート建玉はストップなしで作れない"
                "（docs/05-risk-management.md #3）"
            )

        price = self.fill_price(order.symbol, bar, order.side, opening=True)
        notional = Decimal(str(price)) * order.quantity

        # レバレッジ1倍の強制。**ここが必須通過点**（CLAUDE.md 規約1）
        try:
            enforce(self.get_account(), notional)
        except LeverageViolationError as exc:
            raise OrderRejectedError(str(exc)) from exc

        self._positions[order.symbol] = Position(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            entry_price=price,
            margin_trade_type=order.margin_trade_type or MarginTradeType.DAYTRADE,
            opened_at=self.now,
            stop_order_id=order.client_order_id if order.trigger_price else None,
        )
        return replace(
            order, status=OrderStatus.FILLED, broker_order_id=f"replay-{order.client_order_id}"
        )

    def _close_position(self, order: Order, bar: Bar) -> Order:
        position = self._positions.get(order.symbol)
        if position is None:
            raise OrderRejectedError(f"{order.symbol} に返済する建玉がない")
        if order.quantity != position.quantity:
            raise OrderRejectedError(
                f"{order.symbol}: 部分返済は未対応"
                f"（建玉 {position.quantity} / 注文 {order.quantity}）"
            )

        price = self.fill_price(order.symbol, bar, position.side, opening=False)
        trade = Trade(
            symbol=order.symbol,
            side=position.side,
            quantity=position.quantity,
            entry_time=position.opened_at,
            entry_price=position.entry_price,
            exit_time=self.now,
            exit_price=price,
            exit_reason=self._reasons.get(order.client_order_id, order.client_order_id),
        )
        self._cash += Decimal(str(trade.pnl))
        self._trades.append(trade)
        del self._positions[order.symbol]
        return replace(
            order, status=OrderStatus.FILLED, broker_order_id=f"replay-{order.client_order_id}"
        )

    def cancel_order(self, client_order_id: str) -> None:
        """注文を取り消す。

        リプレイでは注文は送信と同時に約定するため、取り消せる注文は存在しない。
        **黙って成功にしない** — 呼び出し側が「取り消せたはず」と誤解する。
        """
        order = self._orders.get(client_order_id)
        if order is None:
            raise BrokerError(f"知らない注文: {client_order_id}")
        raise BrokerError(
            f"{client_order_id} は約定済み。リプレイでは発注と約定が同時に起きる"
        )

    def get_orders(self) -> tuple[Order, ...]:
        return tuple(self._orders[key] for key in sorted(self._orders))

    def is_shortable(self, symbol: str) -> bool:
        """一般信用（デイトレ）で売建可能か。

        **Stage A では一般信用の売建可能銘柄リストが取得できない。**
        流動性上位であることで代理し、Stage B で実データに差し替える
        （docs/09-data-sources.md §3）。

        代理である以上、Stage A のショート成績は
        **実際には売建できない銘柄を含んでいる可能性がある**。
        Stage B での差し替え後に成績が落ちうることを織り込んでおく。
        """
        turnover = self._average_turnover(symbol)
        return turnover is not None and turnover >= SHORTABLE_MIN_TURNOVER_YEN

    # ------------------------------------------------------------------
    # 便宜
    # ------------------------------------------------------------------

    def market_order(
        self,
        client_order_id: str,
        symbol: str,
        side: Side,
        quantity: int,
        *,
        opening: bool,
        stop_price: float | None = None,
        reason: str | None = None,
    ) -> Order:
        """成行注文を組み立てて送る。呼び出し側の定型コードを減らす。

        Args:
            reason: 手仕舞い理由（``"stop"`` / ``"close_all"`` など）。
                返済時に `Trade.exit_reason` に載る。省略時は注文IDを使う。
        """
        if reason is not None:
            self._reasons[client_order_id] = reason
        return self.send_order(
            Order(
                client_order_id=client_order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type=OrderType.MARKET,
                cash_margin=CashMargin.MARGIN_OPEN if opening else CashMargin.MARGIN_CLOSE,
                margin_trade_type=MarginTradeType.DAYTRADE,
                trigger_price=stop_price,
            )
        )
