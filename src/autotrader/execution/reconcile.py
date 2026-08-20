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

from dataclasses import dataclass

from autotrader.broker.base import Broker
from autotrader.types import Position


@dataclass(frozen=True)
class ReconcileResult:
    """突合の結果。"""

    matched: tuple[Position, ...]
    only_in_local: tuple[Position, ...]
    """システムは持っていると思っているが、実際にはない建玉"""
    only_in_broker: tuple[Position, ...]
    """実際にはあるが、システムが把握していない建玉。**最も危険**"""

    @property
    def is_consistent(self) -> bool:
        return not self.only_in_local and not self.only_in_broker


def reconcile(broker: Broker, local_positions: tuple[Position, ...]) -> ReconcileResult:
    """想定建玉と実建玉を突合する。

    Returns:
        突合結果。``is_consistent`` が False なら発注を停止すること。
    """
    raise NotImplementedError("Phase 3 で実装する")
