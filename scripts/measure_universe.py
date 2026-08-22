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
from autotrader.risk.sizing import max_affordable_price
from autotrader.types import Bar, Symbol
from autotrader.universe.builder import build
from autotrader.universe.filters import (
    DEFAULT_PRICE_HARD_MIN,
    FilterConfig,
    RejectReason,
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

WEIGHT_CANDIDATES = (0.25, 0.40, 0.60)
"""比較する「1銘柄あたり総資産の上限比率」。

**25% は docs/05-risk-management.md #7 の現行値。**
50万円 × 25% ÷ 100株 = 1,250円までしか1単元を建てられず、
ユニバースの株価上限3,000円と食い違う。どちらに寄せるかを
実測してから決めるための候補。
"""

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


def sweep_price_cap(
    as_of: date,
    source: JQuantsDataSource,
    symbols: tuple[Symbol, ...],
    bars: dict[str, tuple[Bar, ...]],
) -> None:
    """1銘柄あたりの上限比率ごとに、通過銘柄数がどう変わるかを測る。

    **収集済みの日足を使い回すので追加のAPI照会は発生しない。**

    【なぜこれを測るのか】

    ``docs/05-risk-management.md`` #7 の「1銘柄あたり総資産の25%」と、
    ``docs/03-universe.md`` の株価上限3,000円は50万円では両立しない。
    50万円 × 25% ÷ 100株 = 1,250円が、1単元を建てられる上限だから。

    25%を守ると株価1,250円超は**選定を通ってもサイジングで0株になる**。
    エラーにならず静かに機会を失うので、どちらに寄せるかを決める必要がある。
    その判断材料として、上限ごとの母集団サイズを並べる。
    """
    hr("5. 1銘柄あたりの上限比率と株価上限の突き合わせ")
    print("  50万円・単元100株では、上限比率が株価の上限を決めてしまう。")
    print("  （追加のAPI照会なし。収集済みの日足を使い回す）")
    print()

    baseline: int | None = None
    for weight in WEIGHT_CANDIDATES:
        ceiling = int(max_affordable_price(CAPITAL, weight))
        concurrent = int(1 / weight)
        # 通常枠/プレミアム枠の境界はこの比較では意味を持たないので、
        # 上限のすぐ下に置いて「上限以下が何銘柄か」だけを見る。
        config = FilterConfig(
            price_normal_max=max(DEFAULT_MIN_NORMAL_MAX, ceiling - 1),
            price_premium_max=ceiling,
        )
        label = f"上限 {weight:.0%}" + ("（現行 #7）" if weight == 0.25 else "　　　　")
        try:
            snapshot = build(as_of, source, config, bars_by_symbol=bars, symbols=symbols)
        except (DataSourceError, ValueError) as exc:
            print(f"  {label}: 測定できなかった — {exc}")
            continue

        if baseline is None:
            baseline = snapshot.size
            delta = ""
        else:
            delta = f"（現行比 +{snapshot.size - baseline}）"
        print(
            f"  {label}: 最大株価 {ceiling:>5,}円 / 同時保有 {concurrent}銘柄 "
            f"/ 通過 {snapshot.size:>4}銘柄{delta}"
        )

    print()
    print("  上限比率を上げるほど母集団は増えるが、")
    print("  1銘柄の逆行が資産に与える影響も比例して増える（25%→60%で2.4倍）。")
    print("  **これは安全装置の閾値なので、人が判断する**（CLAUDE.md）。")


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

    sweep_price_cap(as_of, source, symbols, bars)

    print()
    print("  この結果を config/universe.yaml と docs/03-universe.md に反映してから")
    print("  Phase 3（竹の実装）に進む。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
