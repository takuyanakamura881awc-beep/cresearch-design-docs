"""呼値の単位（tick）と、そこから導く約定コスト。

【なぜ独立したモジュールなのか】

**約定コストの正体はスプレッドで、スプレッドは tick より狭くなれない。**
これは市場の構造であって、戦略にもブローカーにも属さない。
`broker/replay.py`（約定価格）と `universe/selector.py`（ATR の下限）の
両方から使うので、どちらの下にも置かない。

【中心にある恒等式】

::

    往復コスト（ATR単位） = スプレッド円 ÷ ATR円

**株価が式に出てこない。** 効くのは「ATR が円で何円か」だけ。
株価600円で ATR 3.33%（= 20円）の銘柄と、株価2,200円で ATR 0.91%（= 20円）の
銘柄は、**まったく同じコスト**になる。

これまで「ATR% の下限2%」で判定していたのは、株価帯が 300〜1,250円に
固定されていたから成立していた近似にすぎない。株価帯を動かすなら
円建てで判定しないと壊れる。

【手数料はここに含めない】

デイトレ信用は手数料0・金利0・貸株料0（`docs/02-margin-rules.md`）。
ここで扱うのは**市場の相手方に渡るぶん**であって、証券会社に払う費用ではない。
証券会社を変えても安くならない（`docs/00-overview.md` 意思決定ログ30）。

【この表の出典と、確認すべきこと】

東証の呼値の単位。**2026-08 時点の値で、一次資料での再確認が要る**
（作成時に jpx.co.jp へ到達できず、二次情報に依存している）。
我々の株価帯（300〜2,500円）では通常銘柄の1円だけが効くので、
高価格帯の行が多少ずれていても実害はない。

**2027-03-01 に東証は呼値の単位を STR（Spread to Tick Ratio）ベースの
銘柄別方式へ変更する。** 指数構成銘柄かどうかではなく銘柄ごとの流動性で
決まるようになるため、**その時点でこの表は作り直しになる**。
流動性の高い銘柄の tick は細かくなる方向なので、我々には追い風。
"""

from __future__ import annotations

from decimal import Decimal

__all__ = [
    "DEFAULT_COST_ATR_MULTIPLE",
    "DEFAULT_SPREAD_TICKS",
    "half_spread_bps",
    "min_atr_yen",
    "round_trip_cost_atr",
    "spread_yen",
    "tick_size",
]


_REGULAR_TABLE: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal(3_000), Decimal(1)),
    (Decimal(5_000), Decimal(5)),
    (Decimal(30_000), Decimal(10)),
    (Decimal(50_000), Decimal(50)),
    (Decimal(300_000), Decimal(100)),
    (Decimal(500_000), Decimal(500)),
    (Decimal(3_000_000), Decimal(1_000)),
    (Decimal(5_000_000), Decimal(5_000)),
    (Decimal(30_000_000), Decimal(10_000)),
    (Decimal(50_000_000), Decimal(50_000)),
)
"""通常銘柄の呼値。``(この価格以下, 呼値)`` の昇順。

**我々の株価帯（300〜2,500円）で効くのは先頭行の「3,000円以下 → 1円」だけ。**
"""

_REGULAR_ABOVE = Decimal(100_000)
"""通常銘柄で表の最大を超えたときの呼値。"""

_TOPIX100_TABLE: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal(1_000), Decimal("0.1")),
    (Decimal(3_000), Decimal("0.5")),
    (Decimal(10_000), Decimal(1)),
    (Decimal(30_000), Decimal(5)),
    (Decimal(100_000), Decimal(10)),
    (Decimal(300_000), Decimal(50)),
    (Decimal(1_000_000), Decimal(100)),
    (Decimal(3_000_000), Decimal(500)),
    (Decimal(10_000_000), Decimal(1_000)),
    (Decimal(30_000_000), Decimal(5_000)),
)
"""TOPIX100構成銘柄の呼値。通常銘柄より細かい。

**我々の株価帯にはほとんど入ってこない。** TOPIX100 は大型株で、
1銘柄あたり上限25%（= 株価1,250円まで）の制約とほぼ両立しないため。
同時保有数を絞って株価上限を上げると入ってくる可能性があり、
そのとき tick が1/2〜1/10になるので**コスト面では有利に働く**。
"""

_TOPIX100_ABOVE = Decimal(10_000)
"""TOPIX100構成銘柄で表の最大を超えたときの呼値。"""


DEFAULT_SPREAD_TICKS = 2.0
"""スプレッドが呼値の何本ぶんあるとみなすか。**これは仮定であって実測ではない。**

**1tick に張り付く前提を置かない**（CLAUDE.md 規約5「検証できないものは
保守的な側に倒す」）。板が最良気配で1tickに詰まっているのは流動性の
極めて高い銘柄だけで、売買代金3億円の銘柄では常時そうとは限らない。

**この 2.0 が、いま残っている最大の当て推量。** Stage A では板が取れないので
原理的に測れない。測る手段は2つあり、どちらも入金不要:

- 日足OHLCからの推定（Corwin-Schultz 2012 / Abdi-Ranaldo 2017）
- 証券会社のリアルタイム板を録画する（`docs/09-data-sources.md`）

**実測に置き換えるまで、この値を小さくしない。**
"""

DEFAULT_COST_ATR_MULTIPLE = 5.0
"""ATR がスプレッドの何倍あれば「値幅がある」とみなすか。

**往復コストが ATR の 1/5（20%）を超えないこと**と同義。
かつて「ATR% の下限 2%」としていたのは、往復コスト40bps の5倍という
同じ趣旨の計算で、株価帯が固定という前提のうえに成り立っていた。
"""


def tick_size(price: float, *, topix100: bool = False) -> Decimal:
    """``price`` に適用される呼値の単位（円）。

    Args:
        price: 株価（円）。
        topix100: TOPIX100構成銘柄か。細かい方の表を使う。

    Raises:
        ValueError: 株価が正の値でない場合。

    Note:
        境界は**その価格を含む**（1,000円ちょうどは「1,000円以下」の行）。
    """
    if price <= 0:
        raise ValueError(f"株価は正の値である必要がある: {price}")

    table = _TOPIX100_TABLE if topix100 else _REGULAR_TABLE
    above = _TOPIX100_ABOVE if topix100 else _REGULAR_ABOVE
    target = Decimal(str(price))
    for upper, tick in table:
        if target <= upper:
            return tick
    return above


def spread_yen(
    price: float,
    n_ticks: float = DEFAULT_SPREAD_TICKS,
    *,
    topix100: bool = False,
) -> Decimal:
    """想定する売買スプレッド（円）。**最良売気配と最良買気配の差**。

    往復で成行を叩くと、ちょうどこの1本ぶんを払う
    （買いで半分、売りで半分）。

    Raises:
        ValueError: ``n_ticks`` が正の値でない場合。
    """
    if n_ticks <= 0:
        raise ValueError(f"スプレッドの本数は正の値である必要がある: {n_ticks}")
    return tick_size(price, topix100=topix100) * Decimal(str(n_ticks))


def half_spread_bps(
    price: float,
    n_ticks: float = DEFAULT_SPREAD_TICKS,
    *,
    topix100: bool = False,
) -> float:
    """片道で払うスプレッド（bps）。約定価格をずらす幅。

    **成行で板を叩くと仲値からスプレッドの半分ぶん不利な側で約定する。**
    往復ではこの2倍＝スプレッド1本ぶん。
    """
    spread = spread_yen(price, n_ticks, topix100=topix100)
    return float(spread) / 2.0 / price * 10_000.0


def round_trip_cost_atr(
    price: float,
    atr_yen: float,
    n_ticks: float = DEFAULT_SPREAD_TICKS,
    *,
    topix100: bool = False,
) -> float:
    """往復コストが ATR 何個ぶんか。**戦略の損益を左右する唯一のコスト指標**。

    損切り1.5×ATR / 利確2.5×ATR で組んでいる以上、コストも ATR で
    測らないと大小が比較できない。bps で見ると株価帯をまたいだ比較を誤る。

    Raises:
        ValueError: ``atr_yen`` が正の値でない場合。
    """
    if atr_yen <= 0:
        raise ValueError(f"ATR は正の値である必要がある: {atr_yen}")
    return float(spread_yen(price, n_ticks, topix100=topix100)) / atr_yen


def min_atr_yen(
    price: float,
    cost_multiple: float = DEFAULT_COST_ATR_MULTIPLE,
    n_ticks: float = DEFAULT_SPREAD_TICKS,
    *,
    topix100: bool = False,
) -> Decimal:
    """コスト負けしない ATR の下限（円）。

    ``ATR円 ≥ スプレッド円 × cost_multiple``。

    **株価から自動で決まる。** 定数を直書きせず導出するのは
    `risk.limits.max_atr_pct` や `risk.sizing.max_affordable_price` と同じ理由で、
    株価帯や tick が変わったときにここが勝手に追随するため。

    かつての「ATR% の下限 2%」は、株価600円あたりでこの式を評価した値に
    ほぼ一致する（2円 × 5 = 10円 ≒ 600円 × 1.67%）。
    **株価帯を動かすと合わなくなるので、円建てに作り替えてある。**

    Raises:
        ValueError: ``cost_multiple`` が正の値でない場合。
    """
    if cost_multiple <= 0:
        raise ValueError(f"倍率は正の値である必要がある: {cost_multiple}")
    return spread_yen(price, n_ticks, topix100=topix100) * Decimal(str(cost_multiple))
