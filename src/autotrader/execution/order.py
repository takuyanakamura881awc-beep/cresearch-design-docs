"""発注（安全装置 #3/#9/#12/#13/#15）。

**全発注はこのモジュールを通る。** ここで以下を必ず実施する:

- 価格サニティチェック（#13）
- 売建可否チェック（#12）
- ショート建玉へのストップ注文の付与（#3）
- レバレッジ1倍のチェック（``risk.leverage.enforce``、#1）
- 冪等性の担保（#9）
- 監査ログの記録（#15）

**バイパス経路を作ってはならない。** 検査の順序は「安い順・不可逆な操作の前」に
並べてある。発注（不可逆）より前にすべての検査を終える。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from autotrader.broker.base import Broker, BrokerError, OrderRejectedError
from autotrader.execution.journal import OrderJournal, OrderState
from autotrader.risk.leverage import LeverageViolationError
from autotrader.risk.leverage import check as leverage_check
from autotrader.types import (
    AccountState,
    CashMargin,
    MarginTradeType,
    Order,
    OrderStatus,
    OrderType,
    Side,
    Signal,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEVIATION_FROM_PREV_CLOSE = 0.15
"""前日終値からの許容乖離（#13）。config/risk.yaml の ``price_sanity``。

値幅制限（ストップ高安）はおおむね前日終値の ±20〜30% なので、
±15% を超える価格は**誤データか誤発注**の可能性が高い。
正当な急騰急落を弾くこともあるが、**誤発注で建てるほうが高くつく**。
"""


class StopOrderRequiredError(Exception):
    """ショート建玉をストップ注文なしで作ろうとした（#3）。

    空売りは理論上損失無限大。レバレッジ1倍でもこの性質は変わらない。
    """


class PriceSanityError(Exception):
    """価格が前日終値から異常に乖離している（#13）。

    誤データまたは誤発注の可能性。発注を拒否する。
    """


class NotShortableError(Exception):
    """売建できない銘柄に売り注文を出そうとした（#12）。

    一般信用（デイトレ）の在庫は日々変わる。前日の情報で発注すると弾かれる。
    """


@dataclass(frozen=True)
class OrderConfig:
    """発注時の検査設定。config/risk.yaml に対応。"""

    max_deviation_from_prev_close: float = DEFAULT_MAX_DEVIATION_FROM_PREV_CLOSE
    max_retries: int = 3
    close_on_stop_order_failure: bool = True
    """ストップ注文の発注に失敗したら建玉を即座に手仕舞うか（#3）。

    **False にしてはならない。** ストップのないショート建玉を
    保持することになる。
    """


def check_price_sanity(
    price: float,
    prev_close: float,
    max_deviation: float = DEFAULT_MAX_DEVIATION_FROM_PREV_CLOSE,
) -> None:
    """価格が前日終値から異常に乖離していないか（#13）。

    **ゼロ・負値も拒否する。** データ欠損が 0 として流れてくると、
    サイジングが無限大の株数を要求する。

    Raises:
        PriceSanityError: 異常な場合。
    """
    if price <= 0:
        raise PriceSanityError(f"価格が0以下: {price}。データ欠損の可能性")
    if prev_close <= 0:
        raise PriceSanityError(
            f"前日終値が0以下: {prev_close}。比較できないので発注しない"
        )
    deviation = abs(price / prev_close - 1.0)
    if deviation > max_deviation:
        raise PriceSanityError(
            f"前日終値 {prev_close:,.1f} からの乖離 {deviation:.1%} が "
            f"上限 {max_deviation:.1%} を超える（現在値 {price:,.1f}）。"
            "誤データまたは誤発注の可能性"
        )


def submit(
    broker: Broker,
    account: AccountState,
    signal: Signal,
    quantity: int,
    *,
    prev_close: float,
    journal: OrderJournal,
    client_order_id: str,
    config: OrderConfig | None = None,
) -> Order:
    """シグナルを注文に変換して発注する。**全発注の唯一の入口。**

    以下を順に実施する。**発注（不可逆）より前にすべての検査を終える。**

    1. 価格サニティチェック（#13）
    2. 売建可否チェック（ショートの場合、#12）
    3. ショートのストップ価格の有無（#3）
    4. **レバレッジ1倍のチェック**（#1）
    5. 注文IDを**永続化**（#9。発注前に行う）
    6. 発注（冪等リトライつき）
    7. **ショートならストップ注文をセットで発注**（#3）
    8. 監査ログに記録（#15）

    Args:
        prev_close: 前日終値。価格サニティの基準。
        client_order_id: **呼び出し側が採番する。** リトライで同じIDを
            渡せば二重発注しない。

    Raises:
        PriceSanityError: 価格が異常な場合（#13）。
        NotShortableError: 売建できない銘柄の場合（#12）。
        StopOrderRequiredError: ショートにストップ価格がない場合（#3）。
        LeverageViolationError: レバレッジ上限を超える場合（#1）。
    """
    cfg = config or OrderConfig()
    quote = broker.get_quote(signal.symbol)

    # 1. 価格サニティ（#13）— 最も安く、誤データを入口で止める
    check_price_sanity(quote.last, prev_close, cfg.max_deviation_from_prev_close)

    # 2/3. ショート固有の検査（#12 / #3）
    if signal.side is Side.SHORT:
        if not broker.is_shortable(signal.symbol):
            raise NotShortableError(
                f"{signal.symbol} は売建できない。一般信用の在庫は日々変わる（#12）"
            )
        if signal.stop_price is None:
            raise StopOrderRequiredError(
                f"{signal.symbol}: ショート建玉はストップなしで作れない。"
                "空売りは理論上損失無限大（#3）"
            )

    # 4. レバレッジ1倍（#1）— **バイパス経路を作らない**
    notional = Decimal(str(quote.last)) * quantity
    verdict = leverage_check(account, notional)
    if not verdict.allowed:
        raise LeverageViolationError(verdict.reason)

    order = Order(
        client_order_id=client_order_id,
        symbol=signal.symbol,
        side=signal.side,
        quantity=quantity,
        order_type=OrderType.MARKET,
        cash_margin=CashMargin.MARGIN_OPEN,
        margin_trade_type=MarginTradeType.DAYTRADE,
        trigger_price=signal.stop_price,
    )

    # 5. **発注前に永続化**（#9）。ここが冪等性の土台
    #    context は #15 の「事後に完全再現できる粒度」の要求を満たすため
    if journal.get(client_order_id) is None:
        journal.reserve(
            order,
            {
                "reason": signal.reason,
                "strength": signal.strength,
                "quote_last": quote.last,
                "prev_close": prev_close,
                "stop_price": signal.stop_price,
                "take_profit_price": signal.take_profit_price,
                "leverage": verdict.reason,
                "cash": str(account.cash),
                "gross_notional": str(account.gross_notional),
            },
        )

    # 6. 発注（#9 の冪等リトライ）
    placed = submit_with_retry(broker, order, journal, cfg.max_retries)

    # 7. ショートのストップをセットで（#3）
    if signal.side is Side.SHORT:
        _attach_stop(broker, journal, placed, signal, cfg)

    return placed


def submit_with_retry(
    broker: Broker,
    order: Order,
    journal: OrderJournal,
    max_retries: int = 3,
) -> Order:
    """冪等性を担保しつつリトライする（#9）。

    ネットワーク断は必ず起きる。リトライ時の二重発注を**構造的に**防ぐ:

    1. クライアント側で採番した注文IDを、発注前に永続化する（呼び出し側の責務）
    2. リトライ時はその注文IDで ``broker.get_orders()`` を照会し、
       既に通っていないか確認する
    3. **確認できるまで再発注しない**

    照会自体が失敗したら**再発注しない**。「送ったか分からない」状態で
    もう一度送るのが二重発注そのもの。記録は ``RESERVED`` のまま残り、
    起動時の `OrderJournal.unresolved` で人の目に触れる。
    """
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            existing = _find_existing(broker, order.client_order_id)
            if existing is None:
                # **照会できない = 送ったか分からない。再発注しない。**
                logger.error(
                    "%s の状態を確認できないため再発注を中止する。"
                    "記録は RESERVED のまま残す（起動時に人が確認する）",
                    order.client_order_id,
                )
                break
            if existing is not _NOT_SENT:
                logger.warning(
                    "%s は既に送信済みだった。再発注しない", order.client_order_id
                )
                journal.mark(
                    order.client_order_id,
                    OrderState.SENT,
                    broker_order_id=existing.broker_order_id,
                    note="リトライ時に送信済みを検出",
                )
                return existing

        try:
            placed = broker.send_order(order)
        except OrderRejectedError as exc:
            # 拒否は確定した失敗。再発注してよいが、同じ理由でまた拒否される
            journal.mark(
                order.client_order_id, OrderState.REJECTED, note=str(exc)
            )
            raise
        except BrokerError as exc:
            last_error = exc
            logger.warning(
                "%s の発注に失敗した (%d/%d): %s",
                order.client_order_id,
                attempt,
                max_retries,
                exc,
            )
            continue

        journal.mark(
            order.client_order_id,
            OrderState.SENT,
            broker_order_id=placed.broker_order_id,
        )
        return placed

    raise BrokerError(
        f"{order.client_order_id} の発注に失敗した。"
        f"**送信されたかは不明**。起動時に確認すること: {last_error}"
    )


_NOT_SENT = Order(
    client_order_id="",
    symbol="",
    side=Side.LONG,
    quantity=0,
    order_type=OrderType.MARKET,
    cash_margin=CashMargin.MARGIN_OPEN,
    status=OrderStatus.PENDING,
)
"""「照会したが、その注文は存在しなかった」を表す番兵。

``None``（照会自体が失敗した）と区別する。**混同すると、
照会に失敗したときに再発注してしまう** — それが二重発注そのもの。
"""


def _find_existing(broker: Broker, client_order_id: str) -> Order | None:
    """証券会社側にその注文があるか照会する。

    Returns:
        - 注文そのもの: 既に送信済み
        - ``_NOT_SENT``: 照会できたが存在しない（再発注してよい）
        - ``None``: **照会自体が失敗した**（再発注してはならない）
    """
    try:
        orders = broker.get_orders()
    except BrokerError as exc:
        logger.error("注文一覧の照会に失敗した: %s", exc)
        return None
    for existing in orders:
        if existing.client_order_id == client_order_id:
            return existing
    return _NOT_SENT


def _attach_stop(
    broker: Broker,
    journal: OrderJournal,
    position_order: Order,
    signal: Signal,
    config: OrderConfig,
) -> None:
    """ショート建玉に逆指値ストップを付ける（#3）。

    **失敗したら建玉を即座に手仕舞う**（config/risk.yaml の
    ``close_on_stop_order_failure``）。ストップのないショートを
    保持するくらいなら、コストを払って閉じるほうが安い。
    """
    assert signal.stop_price is not None  # submit() で検査済み
    stop_id = f"{position_order.client_order_id}-stop"
    stop = Order(
        client_order_id=stop_id,
        symbol=signal.symbol,
        side=signal.side,
        quantity=position_order.quantity,
        order_type=OrderType.STOP,
        cash_margin=CashMargin.MARGIN_CLOSE,
        margin_trade_type=MarginTradeType.DAYTRADE,
        trigger_price=signal.stop_price,
    )
    if journal.get(stop_id) is None:
        journal.reserve(stop, {"parent": position_order.client_order_id})

    try:
        placed = broker.send_order(stop)
    except BrokerError as exc:
        logger.critical(
            "%s のストップ注文に失敗した: %s。ストップなしのショート建玉は保持しない",
            signal.symbol,
            exc,
        )
        journal.mark(stop_id, OrderState.REJECTED, note=str(exc))
        if config.close_on_stop_order_failure:
            _emergency_close(broker, journal, position_order)
        raise StopOrderRequiredError(
            f"{signal.symbol}: ストップ注文を付与できなかった（#3）"
        ) from exc

    journal.mark(stop_id, OrderState.SENT, broker_order_id=placed.broker_order_id)


def _emergency_close(broker: Broker, journal: OrderJournal, opened: Order) -> None:
    """ストップを付けられなかった建玉を即座に閉じる。

    ここでの失敗は握り潰さない。閉じられなかったこと自体が
    ストップなしのショート建玉が残ったことを意味する。
    """
    close_id = f"{opened.client_order_id}-emergency-close"
    close = Order(
        client_order_id=close_id,
        symbol=opened.symbol,
        side=opened.side,
        quantity=opened.quantity,
        order_type=OrderType.MARKET,
        cash_margin=CashMargin.MARGIN_CLOSE,
        margin_trade_type=opened.margin_trade_type,
    )
    if journal.get(close_id) is None:
        journal.reserve(close, {"parent": opened.client_order_id, "reason": "no_stop"})
    try:
        placed = broker.send_order(close)
    except BrokerError as exc:
        logger.critical(
            "%s の緊急クローズにも失敗した: %s。"
            "**ストップなしのショート建玉が残っている。人が対応すること**",
            opened.symbol,
            exc,
        )
        journal.mark(close_id, OrderState.REJECTED, note=str(exc))
        return
    journal.mark(close_id, OrderState.SENT, broker_order_id=placed.broker_order_id)
    logger.warning("%s を緊急クローズした（ストップ付与に失敗）", opened.symbol)
