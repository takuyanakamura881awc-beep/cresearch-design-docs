"""ヒストリカル・リプレイ Broker。**Stage A の検証基盤。**

蓄積済みのバーを時系列に再生し、``Broker`` インターフェースを満たす。
**証券口座もAPIも不要**でバックテストが回る。

【約定モデル — 板がない場合の扱い】

板情報（bid/ask/厚み）が取れないため、バーの OHLCV から約定を推定する。

**原則: 検証できないものは必ず保守的な側（成績が悪くなる側）に倒す。**

=================  ==========================================================
注文               約定の仮定
=================  ==========================================================
成行               そのバーの**不利な側**（買いは高値寄り、売りは安値寄り）
指値               バーのレンジが価格を通過 **かつ** 出来高が十分な場合のみ
スリッページ       **厚めに見積もる**（片道 15〜20bps）
=================  ==========================================================

**楽観的な約定モデルを使わない。**
バックテストで勝つ戦略を作るのは簡単だが、それは目的ではない。
Stage B で実測スリッページに置き換えたときに成績が崩れるなら、
それは Stage A のモデルが甘かったということ（docs/09-data-sources.md §4）。
"""

from __future__ import annotations

from decimal import Decimal

from autotrader.broker.base import Broker
from autotrader.types import AccountState, Bar, Order, Position, Quote


class ReplayBroker(Broker):
    """蓄積済みバーを再生する検証用 Broker。

    Stage A（Phase 2〜3）で使う。Phase 2 で実装する。
    """

    def __init__(
        self,
        initial_cash: Decimal,
        bars: dict[str, tuple[Bar, ...]],
        slippage_bps: float = 20.0,
    ) -> None:
        """
        Args:
            initial_cash: 架空の初期資金（円）。
            bars: 銘柄コード → バー列。
            slippage_bps: 片道スリッページ（bps）。
                **板がないので Stage B（10bps）より厚く見積もる。**
        """
        raise NotImplementedError("Phase 2 で実装する")

    def advance(self) -> bool:
        """時刻を1バー進める。

        Returns:
            まだバーが残っていれば True。
        """
        raise NotImplementedError("Phase 2 で実装する")

    def get_account(self) -> AccountState:
        raise NotImplementedError("Phase 2 で実装する")

    def get_positions(self) -> tuple[Position, ...]:
        raise NotImplementedError("Phase 2 で実装する")

    def get_quote(self, symbol: str) -> Quote:
        """気配を返す。

        **板がないため、bid/ask はバーの終値からスプレッドを推定して合成する。**
        実際の板ではないことを呼び出し側が誤解しないよう、
        推定であることをドキュメントとログに明示する。
        """
        raise NotImplementedError("Phase 2 で実装する")

    def send_order(self, order: Order) -> Order:
        raise NotImplementedError("Phase 2 で実装する")

    def cancel_order(self, client_order_id: str) -> None:
        raise NotImplementedError("Phase 2 で実装する")

    def get_orders(self) -> tuple[Order, ...]:
        raise NotImplementedError("Phase 2 で実装する")

    def is_shortable(self, symbol: str) -> bool:
        """売建可能か。

        **Stage A では一般信用の売建可能銘柄リストが取得できない。**
        流動性上位であることで代理し、Stage B で実データに差し替える
        （docs/09-data-sources.md §3）。

        代理である以上、Stage A のショート成績は
        **実際には売建できない銘柄を含んでいる可能性がある**。
        Stage B での差し替え後に成績が落ちうることを織り込んでおく。
        """
        raise NotImplementedError("Phase 2 で実装する")
