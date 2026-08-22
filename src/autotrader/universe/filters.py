"""ユニバース構築のハードフィルタ（Layer 1）。

該当したら無条件で除外する。docs/03-universe.md §1 の A〜I に対応。

【なぜ「除外理由」を記録するのか】

各フィルタで何銘柄が落ちたかを段階ごとに数えられないと、
**想定（最終100〜200銘柄）と食い違ったときに原因を特定できない**。
流動性が厳しすぎるのか、株価レンジが狭すぎるのかで打つ手が変わる。

`RejectReason` を返すのはそのため。監査ログとデバッグにも使う。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from autotrader.types import Bar, PriceTier, Symbol

# --- 既定値。config/universe.yaml と一致させること ---
DEFAULT_MARKETS = ("プライム",)
DEFAULT_MIN_AVG_TURNOVER_YEN = Decimal(1_000_000_000)
DEFAULT_TURNOVER_LOOKBACK_DAYS = 20
DEFAULT_PRICE_HARD_MIN = 300
DEFAULT_PRICE_NORMAL_MAX = 2000
DEFAULT_PRICE_PREMIUM_MAX = 3000
DEFAULT_MIN_DAYS_SINCE_LISTING = 60
DEFAULT_EARNINGS_BUFFER_DAYS = 2

LOANABLE_MARGIN_TYPE = "貸借"
"""制度信用で売建できる銘柄の信用区分名。

J-Quants の ``MrgnNm`` から取れる（実測で確認）。
**流動性による代理は不要になった。** プライム1,565銘柄のうち1,483銘柄（95%）が該当する。
"""


class RejectReason(Enum):
    """除外理由。段階ごとの通過数を数えるために使う。"""

    MARKET = "market"
    """A: 対象市場でない（ETF は「その他」になるのでここで落ちる）"""
    NOT_LOANABLE = "not_loanable"
    """D: 貸借銘柄でない＝制度信用で売建できない"""
    PRICE_TOO_LOW = "price_too_low"
    """C: 300円未満。1ティック（1円）が0.33%以上でコスト過大"""
    PRICE_TOO_HIGH = "price_too_high"
    """C: 3,000円超。1単元が資金の60%超で分散が成立しない"""
    ILLIQUID = "illiquid"
    """B: 売買代金が薄い。10〜15万円の注文が板を動かしてしまう"""
    NO_DATA = "no_data"
    """判定に必要な日足がない"""
    TOO_NEW = "too_new"
    """G: 上場後3ヶ月未満。値動きの統計が取れない"""
    EARNINGS = "earnings"
    """F: 決算発表の前後。ギャップリスクが予測不能"""
    LIMIT_HIT = "limit_hit"
    """H: ストップ高/安。約定できない"""
    HALTED = "halted"
    """H: 売買停止・監理/整理銘柄"""
    SHORT_RESTRICTED = "short_restricted"
    """E: 空売り価格規制。発注が弾かれる"""
    CORPORATE_ACTION = "corporate_action"
    """I: 分割・併合の前後。価格の連続性が壊れる"""


@dataclass(frozen=True)
class FilterConfig:
    """Layer 1 のフィルタ設定。

    **株価レンジは資金量の関数。** 資金が増えれば上限を緩められる
    （docs/03-universe.md §2）。
    """

    markets: tuple[str, ...] = DEFAULT_MARKETS
    min_avg_turnover_yen: Decimal = DEFAULT_MIN_AVG_TURNOVER_YEN
    turnover_lookback_days: int = DEFAULT_TURNOVER_LOOKBACK_DAYS
    price_hard_min: int = DEFAULT_PRICE_HARD_MIN
    price_normal_max: int = DEFAULT_PRICE_NORMAL_MAX
    price_premium_max: int = DEFAULT_PRICE_PREMIUM_MAX
    require_loanable: bool = True
    min_days_since_listing: int = DEFAULT_MIN_DAYS_SINCE_LISTING
    earnings_buffer_days: int = DEFAULT_EARNINGS_BUFFER_DAYS

    def __post_init__(self) -> None:
        if not (self.price_hard_min < self.price_normal_max < self.price_premium_max):
            raise ValueError(
                "株価レンジは hard_min < normal_max < premium_max である必要がある"
            )
        if self.turnover_lookback_days < 1:
            raise ValueError("turnover_lookback_days は1以上")


@dataclass(frozen=True)
class ScreenResult:
    """1銘柄の判定結果。"""

    symbol: str
    passed: bool
    reason: RejectReason | None
    """除外理由。通過した場合は ``None``"""
    tier: PriceTier | None = None
    """通過した場合の株価レンジ枠"""
    price: float | None = None
    avg_turnover: Decimal | None = None


def classify_price_tier(
    price: float,
    hard_min: int = DEFAULT_PRICE_HARD_MIN,
    normal_max: int = DEFAULT_PRICE_NORMAL_MAX,
    premium_max: int = DEFAULT_PRICE_PREMIUM_MAX,
) -> PriceTier | None:
    """株価から枠を判定する（docs/03-universe.md §2）。

    日本株は単元100株なので 1単元 = 株価 × 100円。
    50万円という資金では、これが銘柄選定の最も厳しい制約になる。

    ====================  ==============  ==================================
    枠                    1単元           扱い
    ====================  ==============  ==================================
    NORMAL 300〜2,000円   3万〜20万円     スコア上位から通常採用
    PREMIUM 2,000〜3,000円 20万〜30万円   明確な優位がある場合のみ採用
    ====================  ==============  ==================================

    Returns:
        枠。範囲外は ``None``（除外を意味する）。

        - ``hard_min`` 未満: 1ティック（1円）が0.33%以上でコスト過大
        - ``premium_max`` 超: 1単元が資金の60%超で分散が成立しない
    """
    if price < hard_min:
        return None
    if price <= normal_max:
        return PriceTier.NORMAL
    if price <= premium_max:
        return PriceTier.PREMIUM
    return None


def average_turnover(
    bars: tuple[Bar, ...], lookback_days: int = DEFAULT_TURNOVER_LOOKBACK_DAYS
) -> Decimal | None:
    """直近 N 日の平均売買代金。

    J-Quants の日足には売買代金（``Va``）が入っているのでそれを使う。
    yfinance 由来のバーには無いので ``close * volume`` で近似する
    （`Bar.effective_turnover` が吸収する）。

    Returns:
        平均売買代金。バーが足りなければ ``None``。
        **足りないのに計算して返さない** — 少ない日数の平均は
        流動性の判定として信用できない。
    """
    if len(bars) < lookback_days:
        return None
    recent = bars[-lookback_days:]
    total = sum((Decimal(str(b.effective_turnover)) for b in recent), Decimal(0))
    return total / lookback_days


def passes_liquidity(
    avg_turnover_yen: Decimal,
    min_turnover_yen: Decimal = DEFAULT_MIN_AVG_TURNOVER_YEN,
) -> bool:
    """流動性フィルタ（B）。

    10〜15万円の注文が板を動かさない水準を確保する。
    自分の注文が価格に影響すると、バックテストの約定モデルが成立しない。
    """
    return avg_turnover_yen >= min_turnover_yen


def is_loanable(symbol: Symbol) -> bool:
    """貸借銘柄か（D）。

    **貸借銘柄なら制度信用で売建できる。** J-Quants の ``MrgnNm`` で判定する。

    一般信用（デイトレ）の在庫は証券会社側の情報で Stage A では取れないが、
    貸借かどうかは分かる。流動性による代理よりはるかに正確
    （docs/09-data-sources.md §0）。
    """
    return symbol.margin_type == LOANABLE_MARGIN_TYPE


def passes_market(symbol: Symbol, markets: tuple[str, ...] = DEFAULT_MARKETS) -> bool:
    """対象市場か（A）。

    ETF は市場区分が「その他」になるため、ここで自動的に落ちる（実測で確認）。
    """
    return symbol.market in markets


def screen(
    symbol: Symbol,
    bars: tuple[Bar, ...],
    config: FilterConfig | None = None,
    *,
    days_since_listing: int | None = None,
    days_to_earnings: int | None = None,
    short_restricted: bool = False,
    corporate_action_nearby: bool = False,
) -> ScreenResult:
    """1銘柄が Layer 1 を通過するか判定する。

    **判定は軽い順に行う。** 市場区分と信用区分は銘柄一覧だけで判定でき、
    売買代金の計算より安い。先に落とせば無駄な計算をしなくて済む。

    Args:
        symbol: 銘柄（市場区分・信用区分を含む）。
        bars: ``as_of`` までの日足。**未来のバーを含めてはならない。**
        days_since_listing: 上場からの営業日数。不明なら判定をスキップする。
        days_to_earnings: 次の決算発表までの営業日数。不明ならスキップ。

    Returns:
        判定結果。除外された場合は理由が入る。
    """
    cfg = config or FilterConfig()

    # A: 市場区分（銘柄一覧だけで判定できる。最も安い）
    if not passes_market(symbol, cfg.markets):
        return ScreenResult(symbol.code, False, RejectReason.MARKET)

    # D: 貸借銘柄（同上）
    if cfg.require_loanable and not is_loanable(symbol):
        return ScreenResult(symbol.code, False, RejectReason.NOT_LOANABLE)

    # G: 上場期間
    if (
        days_since_listing is not None
        and days_since_listing < cfg.min_days_since_listing
    ):
        return ScreenResult(symbol.code, False, RejectReason.TOO_NEW)

    # F: 決算発表の前後
    if days_to_earnings is not None and abs(days_to_earnings) <= cfg.earnings_buffer_days:
        return ScreenResult(symbol.code, False, RejectReason.EARNINGS)

    # E: 空売り価格規制
    if short_restricted:
        return ScreenResult(symbol.code, False, RejectReason.SHORT_RESTRICTED)

    # I: コーポレートアクションの前後
    if corporate_action_nearby:
        return ScreenResult(symbol.code, False, RejectReason.CORPORATE_ACTION)

    if not bars:
        return ScreenResult(symbol.code, False, RejectReason.NO_DATA)

    latest = bars[-1]

    # H: ストップ高/安・売買停止
    if latest.limit_up or latest.limit_down:
        return ScreenResult(symbol.code, False, RejectReason.LIMIT_HIT)
    if latest.volume <= 0:
        return ScreenResult(symbol.code, False, RejectReason.HALTED)

    # C: 株価レンジ
    tier = classify_price_tier(
        latest.close, cfg.price_hard_min, cfg.price_normal_max, cfg.price_premium_max
    )
    if tier is None:
        reason = (
            RejectReason.PRICE_TOO_LOW
            if latest.close < cfg.price_hard_min
            else RejectReason.PRICE_TOO_HIGH
        )
        return ScreenResult(symbol.code, False, reason, price=latest.close)

    # B: 流動性（最も重い。最後に判定する）
    avg = average_turnover(bars, cfg.turnover_lookback_days)
    if avg is None:
        return ScreenResult(
            symbol.code, False, RejectReason.NO_DATA, tier=tier, price=latest.close
        )
    if not passes_liquidity(avg, cfg.min_avg_turnover_yen):
        return ScreenResult(
            symbol.code,
            False,
            RejectReason.ILLIQUID,
            tier=tier,
            price=latest.close,
            avg_turnover=avg,
        )

    return ScreenResult(
        symbol.code, True, None, tier=tier, price=latest.close, avg_turnover=avg
    )
