"""全建玉クローズ（安全装置 #2）。**最重要コードパス。**

デイトレ信用の建玉を当日中に返済し損ねると、翌営業日に強制決済され
**1注文につき2,200円**が発生する。月利目標（日次約1,220円）の約2日分。

大引けの定時クローズと、キルスイッチ・損失ブレーカーによる緊急クローズの
**両方からここを呼ぶ**。最も重要なコードを1本に集約し、
ペーパー期間中に毎営業日（約60回）実行することで信頼性を稼ぐ。

滅多に走らないコードは、いざという時に動かない。

【手順】
1. 全建玉を成行でクローズ発注
2. ``broker.get_positions()`` で**残存を実測確認**（成功したはずと仮定しない）
3. 残存あり → 再試行 + アラート
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from autotrader.broker.base import Broker, BrokerError
from autotrader.types import (
    CashMargin,
    MarginTradeType,
    Order,
    OrderType,
    Position,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CloseAllResult:
    """クローズ処理の結果。

    ``residual`` が空でなければ**失敗**。アラートを上げて人が対応する
    （docs/06-operations.md §4）。翌営業日に持ち越してはならない。
    """

    attempted: tuple[Position, ...]
    closed: tuple[Position, ...]
    residual: tuple[Position, ...]
    """クローズできなかった建玉。空でなければ失敗"""
    retries: int
    positions_unknown: bool = False
    """建玉を照会できなかったか。

    **「確認できない」と「確認して0件」は別物。**
    照会できない状態を成功として扱うと、残っているのに気づけない。
    運用側の対応も変わる（前者はAPI疎通の確認、後者は残存建玉の手動クローズ）。
    """

    @property
    def success(self) -> bool:
        return not self.residual and not self.positions_unknown


def close_all(
    broker: Broker,
    reason: str,
    max_retries: int = 5,
    retry_interval_seconds: int = 10,
) -> CloseAllResult:
    """全建玉を成行でクローズし、残存がないことを実測確認する。

    部分約定・API障害・タイムアウトでも最終的に建玉が残らないことを
    保証しなければならない。test-writer は必ず障害注入テストを書くこと
    （.claude/agents/test-writer.md）。

    Args:
        broker: 接続先。
        reason: クローズ理由（"scheduled_close" / "killswitch" / "daily_loss" 等）。
            監査ログに残す。
        max_retries: 残存があった場合の再試行回数。
        retry_interval_seconds: 再試行の間隔。

    Returns:
        結果。``success`` が False ならアラートを上げること。
    """
    attempted = _list_positions(broker, max_retries, retry_interval_seconds)
    if attempted is None:
        # **建玉が分からなければ閉じようがない。** 成功として扱わない。
        logger.critical(
            "建玉を照会できない (%s)。クローズできたか**不明**。API疎通を確認すること",
            reason,
        )
        return CloseAllResult((), (), (), max_retries, positions_unknown=True)

    if not attempted:
        logger.info("クローズ対象の建玉なし (%s)", reason)
        return CloseAllResult((), (), (), 0)

    logger.warning("全建玉クローズを開始する: %d件 (%s)", len(attempted), reason)

    residual = attempted
    retries = 0
    unknown = False
    for attempt in range(max_retries + 1):
        if attempt > 0:
            retries = attempt
            logger.warning(
                "残存 %d件。%d秒待って再試行する (%d/%d)",
                len(residual),
                retry_interval_seconds,
                attempt,
                max_retries,
            )
            time.sleep(retry_interval_seconds)

        _send_close_orders(broker, residual, reason)

        # **成功したはずと仮定しない。** 発注が通ったかではなく、
        # GET /positions で残存を実測して確認する（docs/05 #2）。
        # 部分約定・タイムアウト・取引所側の拒否は「発注は成功」に見える。
        checked = _list_positions(broker, 0, retry_interval_seconds)
        if checked is None:
            # **確認できない = 残っているかもしれない。** 成功にしない
            unknown = True
            continue
        unknown = False
        residual = checked
        if not residual:
            logger.info("全建玉クローズ完了 (%s / 再試行%d回)", reason, retries)
            break

    closed = tuple(p for p in attempted if p.symbol not in {r.symbol for r in residual})
    if residual or unknown:
        # **翌営業日に持ち越すと1注文2,200円。** 人が対応する必要がある
        logger.critical(
            "全建玉クローズに失敗した。残存 %d件: %s (%s)。"
            "翌営業日に強制決済され1注文2,200円が発生する",
            len(residual),
            ", ".join(p.symbol for p in residual) or "不明",
            reason,
        )
    return CloseAllResult(
        attempted=attempted,
        closed=closed,
        residual=residual,
        retries=retries,
        positions_unknown=unknown,
    )


def _list_positions(
    broker: Broker, max_retries: int, retry_interval_seconds: int
) -> tuple[Position, ...] | None:
    """建玉を照会する。失敗したら再試行し、駄目なら ``None``。

    **``None`` は「0件」ではなく「不明」。** 呼び出し側で区別すること。
    照会できない状態を「建玉なし」と扱うのが、この経路で最も危険な誤り。
    """
    for attempt in range(max_retries + 1):
        if attempt > 0:
            time.sleep(retry_interval_seconds)
        try:
            return broker.get_positions()
        except BrokerError as exc:
            logger.error(
                "建玉の照会に失敗した (%d/%d): %s", attempt, max_retries, exc
            )
    return None


def _send_close_orders(
    broker: Broker, positions: tuple[Position, ...], reason: str
) -> None:
    """建玉ごとに返済の成行注文を送る。

    **1銘柄の失敗で全体を止めない。** ここで例外を伝播させると、
    残りの建玉が手つかずのまま翌日に持ち越される。
    失敗はログに残し、残存確認と再試行に任せる。
    """
    for position in positions:
        order = Order(
            client_order_id=f"close_all-{reason}-{position.symbol}-{uuid.uuid4().hex[:8]}",
            symbol=position.symbol,
            side=position.side,
            quantity=position.quantity,
            order_type=OrderType.MARKET,
            cash_margin=CashMargin.MARGIN_CLOSE,
            margin_trade_type=position.margin_trade_type or MarginTradeType.DAYTRADE,
        )
        try:
            broker.send_order(order)
        except BrokerError as exc:
            logger.error("%s の返済発注に失敗した: %s", position.symbol, exc)
