"""ポジションサイジング。

日本株は単元100株。株価 P 円の1単元 = 100P 円。
50万円という資金では、この粒度が銘柄選定とサイジングの両方を強く縛る
（docs/03-universe.md §2）。

端数処理を誤ると、意図した金額と実際の建玉額がずれてレバレッジ判定を狂わせる。
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from autotrader.types import Bar


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
    if price <= 0:
        raise ValueError(f"株価は正の値である必要がある: {price}")
    if lot_size < 1:
        raise ValueError(f"単元株数は1以上である必要がある: {lot_size}")
    if target_notional <= 0:
        return 0

    lot_cost = Decimal(str(price)) * lot_size
    lots = int(target_notional // lot_cost)
    return lots * lot_size


def max_affordable_price(
    capital: Decimal,
    max_weight_per_symbol: float = 0.25,
    lot_size: int = 100,
) -> Decimal:
    """1単元が上限比率に収まる最大株価。

    **50万円 × 25% ÷ 100株 = 1,250円。**
    つまり1銘柄あたり総資産25%（docs/05-risk-management.md #7）を守る限り、
    株価1,250円を超える銘柄は1単元すら建てられない。

**ユニバースの株価上限（`DEFAULT_PRICE_PREMIUM_MAX`）はこの値と一致させてある。**
    かつては上限3,000円と食い違っており、「選定は通るがサイジングで0株になる」
    銘柄が1,483中502も生まれていた。静かに機会を失うだけでログにも異常として
    出ないため、実測で発見するまで気づけなかった（2026-08-23）。

    突き合わせを続けられるよう、値をここで計算できる形にしてある。
    """
    return capital * Decimal(str(max_weight_per_symbol)) / lot_size


def target_notional(
    capital: Decimal,
    max_weight_per_symbol: float = 0.25,
) -> Decimal:
    """1銘柄あたりの目標建玉額。

    **枠（PriceTier）で額を変えない。** 当初の設計ではプレミアム枠に
    別扱いを与える想定だったが、上限25%のもとで買える最大株価が1,250円と
    分かり、**どの銘柄も同じ上限の内側に収まる**ようになった
    （docs/03-universe.md §2）。枠の役割はスコアのハードル
    （`universe.selector`）に一本化してある。

    Args:
        capital: 判定の基準額。**現金残高を渡すこと。**
            `risk.leverage.check` も現金を基準にしているので、
            base を揃えないとサイジングとレバレッジ判定が食い違う。
        max_weight_per_symbol: 1銘柄あたりの上限比率（安全装置 #7）。

    Returns:
        目標建玉額（円）。実際の株数は `calc_quantity` が単元に丸める。
    """
    if max_weight_per_symbol <= 0:
        raise ValueError(f"上限比率は正の値である必要がある: {max_weight_per_symbol}")
    if capital <= 0:
        return Decimal(0)
    return capital * Decimal(str(max_weight_per_symbol))


def average_turnover_of(bars: Sequence[Bar], lookback_days: int = 20) -> Decimal | None:
    """直近 N 本の平均売買代金。約定モデルの厳しさを決めるのに使う。

    `universe.filters.average_turnover` と同じ計算だが、あちらは
    ユニバース判定用に「本数が足りなければ ``None``」という厳しい規約を持つ。
    こちらは**あるだけのバーで概算する** — 約定モデルは判定ではなく
    見積もりで、本数不足を理由に見積もりを放棄すると
    薄い銘柄が逆に安いコストで約定してしまう。

    Returns:
        平均売買代金。バーが1本もなければ ``None``。
    """
    if not bars:
        return None
    recent = bars[-lookback_days:]
    total = sum((Decimal(str(b.effective_turnover)) for b in recent), Decimal(0))
    return total / len(recent)
