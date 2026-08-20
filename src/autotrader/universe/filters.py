"""ユニバース構築のハードフィルタ（Layer 1）。

該当したら無条件で除外する。docs/03-universe.md §1 の A〜I に対応。
"""

from __future__ import annotations

from decimal import Decimal

from autotrader.types import PriceTier


def classify_price_tier(
    price: float,
    hard_min: int = 300,
    normal_max: int = 2000,
    premium_max: int = 3000,
) -> PriceTier | None:
    """株価から枠を判定する（docs/03-universe.md §2）。

    日本株は単元100株なので 1単元 = 株価 × 100円。
    50万円という資金では、これが銘柄選定の最も厳しい制約になる。

    ==================  ==============  ==================================
    枠                  1単元           扱い
    ==================  ==============  ==================================
    NORMAL 300〜2,000    3万〜20万円     スコア上位から通常採用
    PREMIUM 2,000〜3,000 20万〜30万円    明確な優位がある場合のみ採用
    ==================  ==============  ==================================

    Returns:
        枠。範囲外（300円未満 / 3,000円超）は ``None`` で除外を意味する。

        - 300円未満: 1ティック（1円）が0.33%以上でコスト過大
        - 3,000円超: 1単元が資金の60%超で分散が成立しない
    """
    raise NotImplementedError("Phase 2 で実装する")


def passes_liquidity(
    avg_turnover_yen: Decimal, min_turnover_yen: Decimal = Decimal(1_000_000_000)
) -> bool:
    """流動性フィルタ（B）。

    10〜15万円の注文が板を動かさない水準を確保する。
    """
    raise NotImplementedError("Phase 2 で実装する")


def is_excluded(
    *,
    days_to_earnings: int | None,
    days_since_listing: int,
    limit_hit: bool,
    halted: bool,
    supervised: bool,
    days_to_corporate_action: int | None,
) -> tuple[bool, str]:
    """除外条件（F〜I）を判定する。

    Returns:
        (除外するか, 理由)。理由は監査ログとデバッグのため必ず返す。
    """
    raise NotImplementedError("Phase 2 で実装する")
