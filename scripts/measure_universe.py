#!/usr/bin/env python3
"""Layer 1 の通過銘柄数を実測する。

    python scripts/measure_universe.py

【何を確かめるのか】

設計では「最終的に100〜200銘柄」と想定しているが、**これは未検証の見込み**。
実測で確定しているのは出発点の1,483銘柄（プライム かつ 貸借）までで、
そこから流動性（20日平均売買代金10億円以上）と株価レンジ（300〜3,000円）で
どこまで絞られるかは分かっていない。

- **少なすぎる**（数十銘柄）→ 日次50銘柄を選べず、竹が成立しない
- **多すぎる**（500銘柄超）→ 絞り込みが効いておらず、選定の意味が薄い

どちらでも閾値を見直す必要がある。**Phase 3（竹の実装）に進む前に確認する。**

【リクエスト数】

銘柄一覧が1回（ページングあり）＋ 日足が営業日ぶん。
日足は ``date=`` で全銘柄を一括取得する（``code=`` の銘柄ループだと
1,483回になり、5件/分では5時間かかる）。

20営業日ぶん × 数ページ ≒ 40〜60リクエスト。12秒間隔で **8〜12分**。
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from autotrader.config import load_credentials, mask
from autotrader.data.base import DataSourceError, RateLimitError
from autotrader.data.jquants import JQuantsDataSource
from autotrader.risk.limits import DEFAULT_DAILY_BREAKER_PCT, DEFAULT_STOP_ATR_MULT
from autotrader.risk.sizing import (
    calc_quantity,
    max_affordable_price,
    target_notional,
)
from autotrader.types import Bar, PriceTier, Symbol, UniverseEntry
from autotrader.universe.builder import build
from autotrader.universe.filters import (
    DEFAULT_PRICE_HARD_MIN,
    FilterConfig,
    RejectReason,
)
from autotrader.universe.selector import (
    SelectorConfig,
    build_candidates,
    select,
)

TURNOVER_DAYS = 20
"""流動性の判定に必要な営業日数。"""

WATCHLIST_SLOTS = 50
"""Layer 2 が日次で選ぶ監視銘柄数（Stage B の WebSocket 制限に由来）。"""

DESIGN_ASSUMPTION = (100, 200)
"""設計時の見込み（docs/03-universe.md）。**未検証の推定値。**"""

CAPITAL = Decimal(500_000)
"""運用資金（円）。config/universe.yaml の capital.total と一致させること。"""

DEFAULT_MIN_NORMAL_MAX = DEFAULT_PRICE_HARD_MIN + 1
"""通常枠の上限の下限。FilterConfig が hard_min < normal_max を要求するため。"""

MAX_WEIGHT_PER_SYMBOL = 0.25
"""1銘柄あたり総資産の上限（docs/05-risk-management.md #7）。**固定。**

【実測で分かった、この値の本当の意味】

25%は恣意的な値ではなく、他の2つの安全装置から逆算された値だった。
損切りは 1.5 × ATR（config/strategies.yaml）、Layer 2 の ATR% 下限は 2%。
1敗あたりの総資産インパクトは ``上限比率 × ATR% × 1.5`` になる::

    上限      ATR 2%    ATR 3%    ATR 4%
    25%       -0.75%    -1.12%    -1.50%
    40%       -1.20%    -1.80%    -2.40%  ← 1敗で日次ブレーカー(-2%)到達
    60%       -1.80%    -2.70%    -3.60%  ← 同上

**上限比率を緩めることは、実質的に日次ブレーカーを無効化することに等しい。**
1日5〜15トレードする前提が「1敗で当日終了」に変わってしまう。
だから母集団はここではなく流動性下限で回復させる。
"""

TURNOVER_CANDIDATES = (
    Decimal(1_000_000_000),
    Decimal(500_000_000),
    Decimal(300_000_000),
    Decimal(200_000_000),
)
"""比較する20日平均売買代金の下限。

**これは安全装置ではなく品質閾値。** 10〜15万円の注文が板を動かさなければよい::

    注文        10億円     5億円     3億円
    10万円      0.010%    0.020%    0.033%
    15万円      0.015%    0.030%    0.050%

3億円銘柄でも売買代金の0.05%。現行の10億円は保守的すぎる。
"""

MAX_ORDER_YEN = Decimal(150_000)
"""1銘柄あたりの目標建玉額の上限（config/risk.yaml の target_position_yen）。

**注意: 現行の 150,000円 は 25%上限（125,000円）を超えており不整合。**
ここは板インパクトの見積もりに使うので、大きい側＝保守的な値のままにしてある。
"""

STOP_ATR_MULT = DEFAULT_STOP_ATR_MULT
"""損切り幅の ATR 倍率。config/strategies.yaml と一致させること。"""

DAILY_BREAKER_PCT = DEFAULT_DAILY_BREAKER_PCT
"""日次損失上限（安全装置 #4）。"""

PRICE_BANDS = (300, 500, 800, 1000, 1250)
"""株価帯の区切り。通常枠とプレミアム枠の新しい境界を数字で決めるために出す。"""

ACCEPTABLE = (100, 500)
"""合否の判定に使う範囲。**見込み値とは別に、目的から決める。**

- 下限100 = 監視枠50の2倍。日々の出入りで枠が埋まらなくなるのを避ける
- 上限500 = 出発点1,483の約1/3。これを超えると Layer 1 が
  「取引できない銘柄を落とす」役割を果たしていない
"""


def hr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def collect_daily_bars(
    source: JQuantsDataSource, end: date, business_days: int
) -> dict[str, tuple[Bar, ...]]:
    """``end`` から遡って営業日ぶんの日足を全銘柄まとめて集める。

    **``date=`` で一括取得する。** 銘柄ごとのループは約4倍のリクエストを要し、
    5件/分の制約下では現実的でない（1,483銘柄で5時間超）。
    """
    collected: dict[str, list[Bar]] = defaultdict(list)
    days_found = 0
    probe = end
    attempts = 0

    while days_found < business_days and attempts < business_days * 2:
        attempts += 1
        if probe.weekday() >= 5:  # 土日は照会しない
            probe -= timedelta(days=1)
            continue

        try:
            bars = source.get_bars_for_date(probe)
        except RateLimitError as exc:
            print(f"  中断: レート制限 — {exc}")
            print("    → 収集済みのぶんだけで判定する（結果は参考値）")
            break
        except DataSourceError:
            probe -= timedelta(days=1)
            continue

        for code, day_bars in bars.items():
            collected[code].extend(day_bars)
        days_found += 1
        print(f"  {probe} : {len(bars):>5}銘柄  （{days_found}/{business_days}日）")
        probe -= timedelta(days=1)

    # 時刻の昇順に整える（screen は末尾を最新として扱う）
    return {
        code: tuple(sorted(bars, key=lambda b: b.timestamp))
        for code, bars in collected.items()
    }


def price_ceiling() -> int:
    """1単元が上限比率に収まる最大株価。**50万円 × 25% ÷ 100株 = 1,250円。**"""
    return int(max_affordable_price(CAPITAL, MAX_WEIGHT_PER_SYMBOL))


def config_for(ceiling: int, min_turnover: Decimal) -> FilterConfig:
    """指定の株価上限・流動性下限での Layer 1 設定。

    通常枠/プレミアム枠の境界はこの比較では意味を持たないので、
    上限のすぐ下に置いて「上限以下が何銘柄か」だけを見る。
    """
    return FilterConfig(
        min_avg_turnover_yen=min_turnover,
        price_normal_max=max(DEFAULT_MIN_NORMAL_MAX, ceiling - 1),
        price_premium_max=ceiling,
    )


def sweep_turnover(
    as_of: date,
    source: JQuantsDataSource,
    symbols: tuple[Symbol, ...],
    bars: dict[str, tuple[Bar, ...]],
) -> None:
    """流動性下限ごとに、株価上限内の通過銘柄数がどう変わるかを測る。

    **収集済みの日足を使い回すので追加のAPI照会は発生しない。**

    【なぜ流動性を動かすのか】

    ``docs/05-risk-management.md`` #7 の「1銘柄あたり総資産の25%」を守ると、
    50万円では株価1,250円が上限になる（1単元 = 株価 × 100株）。
    実測ではこの条件で55銘柄しか残らず、監視枠50に対して1.1倍しかない。
    **Layer 2 の選定が実質機能しない。**

    かといって上限比率は緩められない（`MAX_WEIGHT_PER_SYMBOL` 参照。
    緩めると日次ブレーカーが無効化される）。

    そこで**安全装置ではない側**＝流動性下限を動かして母集団を回復させる。
    どこまで下げれば100銘柄を超えるかを、ここで数字にする。
    """
    ceiling = price_ceiling()

    hr("5. 流動性下限のスイープ（1銘柄あたり上限25%は固定）")
    print(f"  1銘柄あたり上限 {MAX_WEIGHT_PER_SYMBOL:.0%} → 買える最大株価 {ceiling:,}円")
    print("  安全装置は動かさない。品質閾値である流動性下限だけを動かす。")
    print("  （追加のAPI照会なし。収集済みの日足を使い回す）")
    print()

    adopted: Decimal | None = None
    for min_turnover in TURNOVER_CANDIDATES:
        try:
            snapshot = build(
                as_of,
                source,
                config_for(ceiling, min_turnover),
                bars_by_symbol=bars,
                symbols=symbols,
            )
        except (DataSourceError, ValueError) as exc:
            print(f"  {min_turnover / 10**8:>4.0f}億円以上: 測定できなかった — {exc}")
            continue

        okay = ACCEPTABLE[0] <= snapshot.size <= ACCEPTABLE[1]
        if okay and adopted is None:
            adopted = min_turnover
            mark = "  ← 採用候補（100銘柄を超える最も厳しい水準）"
        elif snapshot.size < ACCEPTABLE[0]:
            mark = f"  （{ACCEPTABLE[0]}銘柄に届かない）"
        else:
            mark = ""
        ratio = snapshot.size / WATCHLIST_SLOTS
        print(
            f"  {min_turnover / 10**8:>4.0f}億円以上: 通過 {snapshot.size:>4}銘柄"
            f"（監視枠{WATCHLIST_SLOTS}の {ratio:>4.1f}倍）{mark}"
        )

    print()
    if adopted is None:
        print("  NG: どの水準でも母集団が足りない。")
        print("      株価下限300円の見直しか、監視枠50の縮小を検討する。")
    else:
        impact = MAX_ORDER_YEN / adopted
        print(f"  → 採用候補は {adopted / 10**8:.0f}億円以上。")
        print(
            f"     この水準では {MAX_ORDER_YEN:,}円の注文が売買代金の {impact:.3%} で、"
            "板を動かさない。"
        )

    _print_price_distribution(as_of, source, symbols, bars, adopted or TURNOVER_CANDIDATES[-1])


def _print_price_distribution(
    as_of: date,
    source: JQuantsDataSource,
    symbols: tuple[Symbol, ...],
    bars: dict[str, tuple[Bar, ...]],
    min_turnover: Decimal,
) -> None:
    """採用候補の水準で、株価帯ごとの銘柄数を出す。

    枠の境界そのものは分布ではなく安全装置から導出してある
    （通常枠1,000円 = 資金 ÷ max_concurrent(5) ÷ 100株、
    プレミアム上限1,250円 = 資金 × 25% ÷ 100株）。
    ここで見るのは、その境界で母集団が偏りすぎていないかの確認。
    """
    hr(f"6. 株価帯の分布（流動性 {min_turnover / 10**8:.0f}億円以上）")
    print("  枠の境界を決めるための内訳。1単元 = 株価 × 100株。")
    print()

    previous = 0
    for upper in PRICE_BANDS[1:]:
        try:
            snapshot = build(
                as_of,
                source,
                config_for(upper, min_turnover),
                bars_by_symbol=bars,
                symbols=symbols,
            )
        except (DataSourceError, ValueError) as exc:
            print(f"  〜{upper:,}円: 測定できなかった — {exc}")
            continue
        band = snapshot.size - previous
        weight = upper * 100 / float(CAPITAL)
        print(
            f"  {PRICE_BANDS[PRICE_BANDS.index(upper) - 1]:>5,}〜{upper:>5,}円"
            f"（1単元 最大{upper * 100:>7,}円 = 資産の{weight:>4.0%}）: "
            f"{band:>4}銘柄  （累計 {snapshot.size:>4}）"
        )
        previous = snapshot.size


def report_layer2(
    as_of: date,
    source: JQuantsDataSource,
    symbols: tuple[Symbol, ...],
    bars: dict[str, tuple[Bar, ...]],
) -> None:
    """Layer 1 → Layer 2 を実データで通し、監視枠が埋まるかを確かめる。

    【なぜこれを測るのか】

    Layer 1 の通過数が監視枠50の2.7倍あっても、**Layer 2 はさらに
    ATR% >= 2% で足切りする**。ここで大きく削れると「50枠に対して候補が
    50前後」となり、選定がまた無意味になる。

    Layer 1 の数字だけを見て「母集団は足りた」と判断すると、
    この段階の目減りを見落とす。**通しで測る。**

    追加のAPI照会はゼロ（収集済みの日足で `compute_features` の
    必要本数 20 をちょうど満たす）。
    """
    hr("7. Layer 2 まで通す（監視枠が埋まるか）")

    config = FilterConfig()
    try:
        snapshot = build(as_of, source, config, bars_by_symbol=bars, symbols=symbols)
    except (DataSourceError, ValueError) as exc:
        print(f"  NG: Layer 1 を構築できなかった — {exc}")
        return

    by_code = {s.code: s for s in symbols}
    passed = [by_code[r.symbol] for r in snapshot.passed if r.symbol in by_code]
    tiers = {r.symbol: r.tier for r in snapshot.passed if r.tier is not None}

    # 翌営業日を売買日とみなす。当日のバーは build_candidates が入口で落とす
    trade_date = as_of + timedelta(days=1)
    selector = SelectorConfig()
    candidates = build_candidates(trade_date, passed, bars, tiers, selector)

    print(f"  Layer 1 通過        : {snapshot.size:>4}銘柄")
    print(
        f"  指標を計算できた    : {len(candidates):>4}銘柄"
        f"（日足{selector.min_bars}本が必要。不足 {snapshot.size - len(candidates)}）"
    )
    if not candidates:
        print("  NG: 指標を計算できる銘柄がない。収集した営業日数を確認する")
        return

    atrs = sorted(c.features.atr_pct for c in candidates)
    quiet = sum(1 for a in atrs if a < selector.min_atr_pct)
    wild = sum(1 for a in atrs if a > selector.max_atr_pct)
    eligible = len(candidates) - quiet - wild
    print(
        f"  ATR% の分布         : 中央値 {atrs[len(atrs) // 2]:.2%} / "
        f"上位25% {atrs[int(len(atrs) * 0.75)]:.2%} / 最大 {atrs[-1]:.2%}"
    )
    print(
        f"  ATR% < {selector.min_atr_pct:.2%}（コスト負け）  : {quiet:>4}銘柄で除外"
    )
    print(
        f"  ATR% > {selector.max_atr_pct:.2%}（1敗でブレーカー）: {wild:>4}銘柄で除外"
    )
    print(
        f"  → 範囲内            : {eligible:>4}銘柄"
        f"（{eligible / len(candidates):.0%}）"
    )

    picked = select(candidates, trade_date, selector)
    n_premium = sum(1 for e in picked if e.price_tier is PriceTier.PREMIUM)
    print()
    print(
        f"  **選定された        : {len(picked):>4}銘柄"
        f"（監視枠 {selector.max_watchlist}）**"
        f"  通常{len(picked) - n_premium} / プレミアム{n_premium}"
    )

    if len(picked) < selector.max_watchlist:
        print()
        print(f"  監視枠 {selector.max_watchlist} を埋められていない。")
        print("  → 流動性下限をさらに下げる（3億→2億で +25銘柄）か、")
        print("     max_watchlist を実態に合わせて下げるかを判断する。")

    _print_loss_impact(picked, bars)

    if picked:
        print()
        print("  上位10銘柄:")
        for entry in picked[:10]:
            bar = bars[entry.symbol.code][-1]
            print(
                f"    {entry.symbol.code}  {bar.close:>7,.0f}円  "
                f"スコア {entry.score:.3f}  ATR% {entry.atr_pct:>5.2%}  "
                f"{entry.price_tier.value}  {entry.symbol.name[:16]}"
            )


def _print_loss_impact(
    picked: tuple[UniverseEntry, ...], bars: dict[str, tuple[Bar, ...]]
) -> None:
    """選定された銘柄の「1敗あたり総資産インパクト」を出す。

    **ATR% 上限が実際に効いているかの検算。**

    1敗の損失 = 建玉 × 損切り幅 = 建玉 × 1.5 × ATR%。
    単元100株が最小単位なので、この値はこれ以上小さくできない。

    **「N敗で到達」は勝ちを挟まない連敗の場合の数字。**
    日次ブレーカーは損益 -2% で判定するのであって敗数ではない。
    利確2.5×ATR / 損切1.5×ATR なので1勝が約1.7敗を打ち消し、
    勝ちが挟まればもっと回せる（docs/04）。
    """
    if not picked:
        return

    losses = []
    for entry in picked:
        price = bars[entry.symbol.code][-1].close
        qty = calc_quantity(target_notional(CAPITAL), price)
        if qty <= 0:
            continue
        notional = qty * price
        losses.append((entry.symbol.code, notional / float(CAPITAL),
                       notional * STOP_ATR_MULT * entry.atr_pct / float(CAPITAL)))

    if not losses:
        print()
        print("  NG: 選定された銘柄が1つも発注できない（1単元も買えない）")
        return

    values = sorted(v for _, _, v in losses)
    worst_code, worst_weight, worst = max(losses, key=lambda x: x[2])
    median = values[len(values) // 2]
    over = [c for c, _, v in losses if v >= DAILY_BREAKER_PCT]

    print()
    print("  1敗あたりの総資産インパクト（1単元が最小単位なのでこれ以上下げられない）")
    print(
        f"    中央値 -{median:.2%}（**勝ちを挟まず**{DAILY_BREAKER_PCT / median:.1f}連敗で"
        f"日次ブレーカー -{DAILY_BREAKER_PCT:.0%} 到達）"
    )
    print(
        f"    最悪   -{worst:.2%}  {worst_code}（建玉が資金の{worst_weight:.0%}）"
    )
    if over:
        print(f"    **NG: 1敗でブレーカー到達する銘柄が {len(over)} 件: {over[:5]}**")
        print("       → ATR% 上限が効いていない。導出を確認する")
    else:
        print("    OK: 1敗でブレーカーに達する銘柄はない")


def main() -> int:
    print("Layer 1 の通過銘柄数を実測する")
    print(f"実行日: {date.today()}")

    try:
        creds = load_credentials(require_kabus=False)
    except RuntimeError as exc:
        print(f"NG: {exc}")
        return 1
    print(f"JQUANTS_API_KEY: {mask(creds.jquants_api_key)}")

    source = JQuantsDataSource(creds.jquants_api_key)

    # 契約範囲の終端を探す（範囲外は送信前に弾かれる）
    hr("1. 基準日の決定")
    as_of: date | None = None
    for back in range(80, 100):
        probe = date.today() - timedelta(days=back)
        if probe.weekday() >= 5:
            continue
        try:
            if source.get_bars_for_date(probe):
                as_of = probe
                break
        except DataSourceError:
            continue

    if as_of is None:
        print("  NG: 基準日を決められなかった")
        return 1
    print(f"  基準日: {as_of}（契約範囲内の最新営業日）")

    hr(f"2. 日足の収集（{TURNOVER_DAYS}営業日ぶん）")
    print("  date= で全銘柄を一括取得する。12秒間隔なので数分かかる")
    bars = collect_daily_bars(source, as_of, TURNOVER_DAYS)
    print(f"  収集完了: {len(bars)}銘柄")

    hr("3. Layer 1 の絞り込み")
    symbols = source.list_symbols(as_of)
    if symbols is None:
        print("  NG: 銘柄一覧を取得できなかった")
        return 1

    config = FilterConfig()
    print(f"  市場          : {', '.join(config.markets)}")
    print(
        f"  売買代金      : {config.min_avg_turnover_yen:,} 円以上"
        f"（{config.turnover_lookback_days}日平均）"
    )
    print(f"  株価レンジ    : {config.price_hard_min:,} 〜 {config.price_premium_max:,} 円")
    print(f"  貸借銘柄のみ  : {config.require_loanable}")

    try:
        snapshot = build(as_of, source, config, bars_by_symbol=bars, symbols=symbols)
    except DataSourceError as exc:
        print(f"  NG: {exc}")
        return 1

    print()
    print(f"  全上場        : {snapshot.total_listed:>5}")
    for reason in RejectReason:
        count = snapshot.reject_counts.get(reason, 0)
        if count:
            print(f"  除外 {reason.value:<18}: {count:>5}")
    print(f"  **通過        : {snapshot.size:>5}銘柄**")

    hr("4. 判定")
    print(f"  設計時の想定: {DESIGN_ASSUMPTION[0]}〜{DESIGN_ASSUMPTION[1]}銘柄（未検証の見込み）")
    print(f"  許容範囲    : {ACCEPTABLE[0]}〜{ACCEPTABLE[1]}銘柄")
    print(f"  実測        : {snapshot.size}銘柄")
    print()

    if not DESIGN_ASSUMPTION[0] <= snapshot.size <= DESIGN_ASSUMPTION[1]:
        print("  想定の範囲外。ただし想定は未検証の見込み値なので、")
        print("  「実測が外れた」ではなく「想定が外れた」と読む。")
        print()

    if snapshot.size < ACCEPTABLE[0]:
        print("  NG: 少なすぎる")
        print(f"  → 日次{WATCHLIST_SLOTS}銘柄を選ぶ母集団として不足。竹が成立しない")
        print("     売買代金の閾値を下げるか、株価レンジの上限を上げることを検討")
    elif snapshot.size > ACCEPTABLE[1]:
        print("  NG: 多すぎる")
        print("  → 絞り込みが効いていない。選定の意味が薄い")
        print("     売買代金の閾値を上げることを検討")
    else:
        print("  OK: 許容範囲内。閾値の変更は不要")
        print(f"     日次{WATCHLIST_SLOTS}枠に対して母集団が"
              f"{snapshot.size / WATCHLIST_SLOTS:.1f}倍あり、選定の余地がある")

    sweep_turnover(as_of, source, symbols, bars)
    report_layer2(as_of, source, symbols, bars)

    print()
    print("  この結果を config/universe.yaml と docs/03-universe.md に反映してから")
    print("  Phase 3（竹の実装）に進む。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
