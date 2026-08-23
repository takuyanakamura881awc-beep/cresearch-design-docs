"""建玉の突合（安全装置 #10）。

システムが想定している建玉と、``GET /positions`` の実建玉を突合する。

**不一致なら発注を停止して通知する。**
状態のズレを抱えたまま自動発注を続けるのが最も危険。
推測で続行せず、人が原因を確認するまで再開しない。

起動時（毎営業日 08:55）に必ず実行する。
不一致が出たらその日は発注しない。ズレを抱えて自動売買を始めるより、
1日休む方が安い（docs/06-operations.md §1）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from autotrader.broker.base import Broker
from autotrader.types import Position

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileResult:
    """突合の結果。"""

    matched: tuple[Position, ...]
    only_in_local: tuple[Position, ...]
    """システムは持っていると思っているが、実際にはない建玉"""
    only_in_broker: tuple[Position, ...]
    """実際にはあるが、システムが把握していない建玉。**最も危険**"""

    mismatched: tuple[tuple[Position, Position], ...] = ()
    """同じ銘柄だが数量や方向が食い違う建玉（ローカル, 実際）。

    **片方にしかない建玉と同じくらい危険。** 数量がずれていると
    「返済したつもりで残る」「二重に返済しようとする」が起きる。
    """

    @property
    def is_consistent(self) -> bool:
        return not self.only_in_local and not self.only_in_broker and not self.mismatched

    def summary(self) -> str:
        """人が読む形。不一致のときは通知に載せる。"""
        if self.is_consistent:
            return f"突合OK: {len(self.matched)}件が一致"
        lines = ["**突合に失敗した。発注を停止する**"]
        for position in self.only_in_broker:
            lines.append(
                f"  実際にあるがシステムが知らない: {position.symbol} "
                f"{position.side.value} {position.quantity}株  ← 最も危険"
            )
        for position in self.only_in_local:
            lines.append(
                f"  システムは持っているつもりだが実際にない: {position.symbol} "
                f"{position.side.value} {position.quantity}株"
            )
        for local, actual in self.mismatched:
            lines.append(
                f"  内容が食い違う: {local.symbol} "
                f"ローカル {local.side.value} {local.quantity}株 / "
                f"実際 {actual.side.value} {actual.quantity}株"
            )
        return "\n".join(lines)


def reconcile(broker: Broker, local_positions: tuple[Position, ...]) -> ReconcileResult:
    """想定建玉と実建玉を突合する。

    Returns:
        突合結果。``is_consistent`` が False なら発注を停止すること。

    Note:
        **照会に失敗したら例外をそのまま通す。** 「実建玉が取れなかったから
        ローカルを正とする」は、状態のズレを抱えたまま自動発注を続ける
        ことになり、この装置が防ごうとしているものそのもの。
    """
    actual = broker.get_positions()
    by_symbol_actual = {p.symbol: p for p in actual}
    by_symbol_local = {p.symbol: p for p in local_positions}

    matched: list[Position] = []
    mismatched: list[tuple[Position, Position]] = []
    for symbol, local in by_symbol_local.items():
        counterpart = by_symbol_actual.get(symbol)
        if counterpart is None:
            continue
        if (
            local.side is counterpart.side
            and local.quantity == counterpart.quantity
        ):
            matched.append(counterpart)
        else:
            mismatched.append((local, counterpart))

    result = ReconcileResult(
        matched=tuple(matched),
        only_in_local=tuple(
            p for s, p in by_symbol_local.items() if s not in by_symbol_actual
        ),
        only_in_broker=tuple(
            p for s, p in by_symbol_actual.items() if s not in by_symbol_local
        ),
        mismatched=tuple(mismatched),
    )

    if result.is_consistent:
        logger.info("%s", result.summary())
    else:
        # **推測で続行しない。** 人が原因を確認するまで再開しない
        logger.critical("%s", result.summary())
    return result
