"""ペーパートレードの Broker 実装。

kabuステーションAPI のリアルタイム時価を受け取り、
**実際の値動きに連動して**約定を判定する。架空資金で本番と同じ挙動を再現する。

【約定モデル】
- 成行 … 次ティックの反対サイド気配 + スリッページ想定で約定
- 指値 … 板が価格を通過したら約定（出来高按分も考慮）

【重要】デイトレ信用は手数料0・金利0・貸株料0だが、**コストは0ではない**。
スプレッドとスリッページを必ず引く。ここを甘くすると、
3ヶ月のペーパー成績が本番で再現しない（docs/02-margin-rules.md §5）。
"""

from __future__ import annotations

from decimal import Decimal

from autotrader.broker.base import Broker
from autotrader.types import AccountState, Order, Position, Quote


class PaperBroker(Broker):
    """架空資金での売買シミュレーション。

    Phase 3 で実装する。Phase 5 の3ヶ月ペーパートレードで使う。
    """

    def __init__(
        self,
        initial_cash: Decimal,
        quote_source: Broker,
        slippage_bps: float = 10.0,
    ) -> None:
        """
        Args:
            initial_cash: 架空の初期資金（円）。
            quote_source: 時価の取得元。実運用では ``KabusBroker`` を渡し、
                実際の値動きに連動させる。
            slippage_bps: 片道のスリッページ（bps）。保守的に見積もること。
        """
        raise NotImplementedError("Phase 3 で実装する")

    def get_account(self) -> AccountState:
        raise NotImplementedError("Phase 3 で実装する")

    def get_positions(self) -> tuple[Position, ...]:
        raise NotImplementedError("Phase 3 で実装する")

    def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError("Phase 3 で実装する")

    def send_order(self, order: Order) -> Order:
        raise NotImplementedError("Phase 3 で実装する")

    def cancel_order(self, client_order_id: str) -> None:
        raise NotImplementedError("Phase 3 で実装する")

    def get_orders(self) -> tuple[Order, ...]:
        raise NotImplementedError("Phase 3 で実装する")

    def is_shortable(self, symbol: str) -> bool:
        raise NotImplementedError("Phase 3 で実装する")
