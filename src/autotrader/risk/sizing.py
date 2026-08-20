"""ポジションサイジング。

日本株は単元100株。株価 P 円の1単元 = 100P 円。
50万円という資金では、この粒度が銘柄選定とサイジングの両方を強く縛る
（docs/03-universe.md §2）。

端数処理を誤ると、意図した金額と実際の建玉額がずれてレバレッジ判定を狂わせる。
"""

from __future__ import annotations

from decimal import Decimal

from autotrader.types import PriceTier


def calc_quantity(
    target_notional: Decimal,
    price: float,
    lot_size: int = 100,
) -> int:
    """目標建玉額から発注株数を求める。

    単元株の倍数に切り下げる。切り上げると目標額を超え、
    レバレッジ上限に抵触しうるため必ず切り下げる。

    Returns:
        発注株数。1単元も買えない場合は 0。
    """
    raise NotImplementedError("Phase 2 で実装する")


def target_notional_for_tier(
    capital: Decimal,
    tier: PriceTier,
    max_weight_per_symbol: float = 0.25,
) -> Decimal:
    """株価レンジの枠に応じた目標建玉額を返す。

    PREMIUM は1単元が資金の40〜60%を占めるため、
    ``max_weight_per_symbol`` の制約に抵触しやすい。
    抵触する場合は採用しない（docs/03-universe.md §2）。
    """
    raise NotImplementedError("Phase 2 で実装する")
