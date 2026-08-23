#!/usr/bin/env python3
"""竹を実データでバックテストする。

    python scripts/backtest_take.py
    python scripts/backtest_take.py --no-breakers   # ブレーカーの寄与を測るとき

事前に ``python scripts/fetch_bars.py`` でバーを取得しておく。

【この検証で見るもの・見ないもの】

**見る**: エンジンが最後まで走るか。ブレーカーが期待どおり発動するか。
1日のトレード数が想定（3〜5回）に近いか。

**見ない**: 合否。5分足は58日ぶんしかなく、
``docs/07-go-live-criteria.md`` の基準（総トレード数 > 100、シャープ > 1.0）を
評価するには短い。**ここで良い数字が出ても採用の根拠にしない。**

【Layer 2 を日次で回す】

監視50銘柄は毎日選び直す。当日より**前**の日足だけで指標を計算し
（`build_candidates` が入口で切る）、その日に建ててよい銘柄を決める。

固定のユニバースで全期間を回すと、
「後から見て良かった銘柄」を最初から見ていたことになりかねない。
"""

from __future__ import annotations

import argparse
import collections
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from autotrader.broker.replay import (
    SHORTABLE_MIN_TURNOVER_YEN,
    STAGE_A_SLIPPAGE_BPS,
)
from autotrader.data.store import BarStore
from autotrader.engine.backtest import BacktestConfig, BacktestResult, run
from autotrader.report.metrics import TRADING_DAYS_PER_YEAR
from autotrader.risk.limits import (
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_MAX_WEIGHT_PER_SYMBOL,
    max_atr_pct,
    max_atr_yen,
)
from autotrader.risk.sizing import average_turnover_of, max_affordable_price
from autotrader.strategy.take_intraday import TakeIntraday
from autotrader.tick import half_spread_bps, round_trip_cost_atr, spread_yen
from autotrader.types import Bar, PriceTier, Symbol, Trade
from autotrader.universe.filters import FilterConfig, screen
from autotrader.universe.selector import (
    DEFAULT_STAGE_A_WEIGHTS,
    SelectorConfig,
    build_candidates,
    select,
)

DATA_ROOT = Path("data")
CAPITAL = Decimal(500_000)


def hr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def load_symbols() -> tuple[Symbol, ...]:
    import json

    path = DATA_ROOT / "universe.json"
    if not path.is_file():
        raise SystemExit(f"{path} がない。先に python scripts/fetch_bars.py を実行する")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        Symbol(
            code=r["code"],
            name=r["name"],
            market=r.get("market"),
            margin_type=r.get("margin_type"),
            sector=r.get("sector"),
        )
        for r in payload["symbols"]
    )


def daily_watchlists(
    symbols: tuple[Symbol, ...],
    daily: dict[str, tuple[Bar, ...]],
    trading_days: tuple[date, ...],
    filters: FilterConfig,
    selector: SelectorConfig,
) -> tuple[dict[date, frozenset[str]], list[int], list[float]]:
    """営業日ごとに Layer 1 → Layer 2 を回して監視銘柄を決める。

    **その日より前の日足だけを使う。** `screen` に渡すバーも
    `build_candidates` に渡すバーも当日を含めない。
    当日の終値で流動性や株価を判定すると、寄り前に知りえない情報で選ぶことになる。

    Returns:
        (営業日 → 監視銘柄, 日ごとの選定数, 選ばれた銘柄の往復コスト（ATR単位）)。

        **コストを ATR 単位で出せるのはここだけ。** 約定側は ATR を
        持っておらず、トレードの値幅で代用すると指標が壊れる。
    """
    watchlist: dict[date, frozenset[str]] = {}
    counts: list[int] = []
    costs: list[float] = []
    """選ばれた銘柄の往復コスト（ATR単位）。**ATR を持っているのはここだけ。**"""

    for day in trading_days:
        past = {
            code: tuple(b for b in series if b.timestamp.date() < day)
            for code, series in daily.items()
        }
        # Layer 1（当日より前の日足で株価・流動性を判定）
        tiers: dict[str, PriceTier] = {}
        eligible: list[Symbol] = []
        for symbol in symbols:
            result = screen(symbol, past.get(symbol.code, ()), filters)
            if result.passed and result.tier is not None:
                tiers[symbol.code] = result.tier
                eligible.append(symbol)

        # Layer 2
        candidates = build_candidates(day, eligible, past, tiers, selector)
        picked = select(candidates, day, selector)
        watchlist[day] = frozenset(e.symbol.code for e in picked)
        counts.append(len(picked))

        chosen = frozenset(e.symbol.code for e in picked)
        costs.extend(
            round_trip_cost_atr(c.features.price, c.features.atr_yen)
            for c in candidates
            if c.symbol.code in chosen
        )

    return watchlist, counts, costs


def shortable_symbols(daily: dict[str, tuple[Bar, ...]]) -> frozenset[str]:
    """売建できる銘柄（Stage A の代理指標）。

    **日足で判定する。** 約定に使う5分足から導出すると、「直近20本の平均」が
    100分ぶんの売買代金になり、日次の閾値（10億円）と単位が食い違う。
    それをやると**ショートが1件も出ないのにエラーも出ない**
    （実際に踏んだので、判定をここに置いている）。

    一般信用（デイトレ）の在庫は証券会社側の情報で Stage A では取れないため、
    流動性上位であることで代理する。**Stage B で実データに差し替えたとき
    ショート成績が落ちうる**ことを織り込んでおく（docs/09 §3）。
    """
    result = set()
    for code, bars in daily.items():
        turnover = average_turnover_of(bars)
        if turnover is not None and turnover >= SHORTABLE_MIN_TURNOVER_YEN:
            result.add(code)
    return frozenset(result)


def _avg_notional(result: BacktestResult) -> float:
    """1トレードあたりの平均建玉額（円）。コストの比率を出すのに使う。"""
    if not result.trades:
        return 1.0
    return sum(t.entry_price * t.quantity for t in result.trades) / len(result.trades)


def _trade_cost_bps(trade: Trade) -> float:
    """そのトレードの往復コスト（bps）。

    **ATR 単位ではなく bps で出す。** ATR は約定時点の値を持っておらず、
    値幅（|出口 - 入口|）で代用すると損切り(1.5×ATR)・利確(2.5×ATR)・
    時間切れ(ほぼ0)を同列に扱うことになり、指標そのものが壊れる。

    ATR 単位のコストは ATR を持っている選定側で測る
    （`_report_selection_cost`）。ここでは厳密に出せる bps にとどめる。
    """
    return float(spread_yen(trade.entry_price)) / trade.entry_price * 10_000.0


def report(result: BacktestResult, n_days: int, label: str) -> None:
    hr(f"結果（{label}）")
    print(f"  期間            : {n_days}営業日")
    print(f"  トレード数      : {result.n_trades}")
    if result.n_trades:
        print(f"  勝率            : {result.win_rate:.1%}")
        pf = result.profit_factor
        print(f"  プロフィットファクタ: {'∞（要検査）' if pf == float('inf') else f'{pf:.2f}'}")
    print(f"  総リターン      : {result.total_return:+.2%}")

    # --- コストは実測。ブレーカーが総リターンを閾値に張り付かせても読める ---
    print()
    print(f"  **払ったコスト  : {result.total_cost_yen:>10,.0f}円"
          f"（資金の {result.cost_pct_of_capital:.2%}）**")
    if result.n_trades:
        print(
            f"  1トレード平均   : {result.cost_per_trade_yen:>10,.0f}円"
            f"（往復。建玉の {result.cost_per_trade_yen / _avg_notional(result):.3%}）"
        )
        costs = sorted(_trade_cost_bps(t) for t in result.trades)
        mid = len(costs) // 2
        print(
            f"  往復コスト(bps) : 中央値 {costs[mid]:.1f} / "
            f"最安 {costs[0]:.1f} / 最高 {costs[-1]:.1f}"
            f"（**{costs[-1] / costs[0]:.1f}倍の開き**）"
        )
    print(f"  コスト前        : {result.gross_return:+.2%}"
          "  ← net = gross - cost を解いただけ（推定ではない）")
    print()

    # **途中停止したら、リスク指標は数字を出さない。**
    # 残りの期間はエクイティカーブがフラットで、シャープも最大DDも
    # 「取引していない期間」に薄められる。比較に使える数字ではない。
    # report/metrics.py がサンプル不足で 0.0 を返すのと同じ思想。
    if result.halted_early:
        print("  最大DD          : 判定不能（途中停止）")
        print("  シャープ        : 判定不能（途中停止）")
    else:
        print(f"  最大DD          : {result.max_drawdown:.2%}")
        sharpe = result.sharpe
        note = "（サンプル不足で判定不能）" if sharpe == 0.0 and n_days < 21 else ""
        print(f"  シャープ        : {sharpe:.2f}{note}")
    print(f"  日次ブレーカー  : {result.breaker_days}日 発動")
    print(f"  途中停止        : {result.halted_early}")
    print(f"  レバレッジ見送り: {result.rejected_by_leverage}回")
    print(f"  売建不可で見送り: {result.skipped_shorts}回")

    if result.skipped_shorts and result.n_trades:
        shorts = sum(1 for t in result.trades if t.side.value == "short")
        if shorts == 0:
            print()
            print("  **ショートが1件も成立していない。** 売建可否の判定を確認する")

    if result.trades:
        reasons = collections.Counter(t.exit_reason for t in result.trades)
        print(f"  手仕舞い理由    : {dict(reasons)}")
        sides = collections.Counter(t.side.value for t in result.trades)
        print(f"  方向            : {dict(sides)}")
        per_day = collections.Counter(t.exit_time.date() for t in result.trades)
        traded = len(per_day)
        # **分母を2通り出す。** 「取引した日だけ」で割ると回転の多さが見え、
        # 「全営業日」で割ると実際の稼働率が見える。片方だけだと印象が偏る。
        print(
            f"  1日あたり       : 取引した{traded}日で平均 "
            f"{sum(per_day.values()) / traded:.1f}回 / "
            f"全{n_days}営業日なら平均 {result.n_trades / n_days:.1f}回"
        )
        # **回数そのものは制約ではない**（docs/04）。縛っているのは日次損益。
        # 目安から外れたことより、日次ブレーカーが何日発動したかを見る。
        if result.breaker_days:
            print(
                f"    → 日次ブレーカーが{result.breaker_days}日発動している。"
                "1トレードあたりのリスクが資金量に対して大きい可能性"
            )
        last_day = max(per_day)
        print(f"  最後に取引した日: {last_day}")

    if result.halted_early:
        print()
        print("  **途中停止している。** 残りの期間は取引していない。")
        print("  総リターンを年率換算してはならず、シャープ・最大DDも比較に使えない。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-breakers",
        action="store_true",
        help="ブレーカーを切って寄与を測る。**この成績を採用してはならない**",
    )
    parser.add_argument(
        "--flat-slippage",
        action="store_true",
        help=(
            f"約定コストを固定{STAGE_A_SLIPPAGE_BPS:.0f}bpsにする（tick モデル導入前の"
            "挙動）。過去の結果と比較するときだけ使う"
        ),
    )
    parser.add_argument(
        "--legacy-score",
        action="store_true",
        help=(
            "スコアのボラティリティ項を ATR円 から ATR%% に戻す（差し替え前の挙動）。"
            "選定がコストを見るようになったかの A/B に使う"
        ),
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=None,
        metavar="N",
        help=(
            "同時保有数。**指定すると1銘柄あたり比率・株価上限・ATR%%上限が連動する。**"
            "安全装置#7の値そのものは変えず、この実行だけの比較用"
        ),
    )
    args = parser.parse_args()
    if args.max_concurrent is not None and args.max_concurrent < 1:
        parser.error("--max-concurrent は1以上")

    print("竹を実データでバックテストする")

    hr("1. データの読み込み")
    symbols = load_symbols()
    store = BarStore(DATA_ROOT)
    daily = {s.code: store.read(s.code, "1d") for s in symbols}
    intraday = {s.code: store.read(s.code, "5m") for s in symbols}
    daily = {c: b for c, b in daily.items() if b}
    intraday = {c: b for c, b in intraday.items() if b}

    print(f"  銘柄一覧: {len(symbols)}銘柄")
    print(f"  日足あり: {len(daily)}銘柄")
    print(f"  5分足あり: {len(intraday)}銘柄")
    if not intraday:
        print("  NG: 5分足がない。先に python scripts/fetch_bars.py を実行する")
        return 1

    trading_days = tuple(
        sorted({b.timestamp.date() for bars in intraday.values() for b in bars})
    )
    print(f"  検証期間: {trading_days[0]} 〜 {trading_days[-1]}（{len(trading_days)}営業日）")

    hr("2. Layer 2 の日次選定")
    print("  当日より前の日足だけで毎日選び直す")
    # 旧スコアは「ATR円 の重みを ATR% に付け替える」だけ。重みの値は変えない
    weights = dict(DEFAULT_STAGE_A_WEIGHTS)
    if args.legacy_score:
        weights = {"atr_pct" if k == "atr_yen" else k: v for k, v in weights.items()}
        print("  **旧スコア**: ボラティリティ項を ATR% に戻して選定する")

    if args.max_concurrent is None:
        filters = FilterConfig()
        selector = SelectorConfig(weights=weights)
        weight = DEFAULT_MAX_WEIGHT_PER_SYMBOL
    else:
        # 同時保有数を絞ると1銘柄あたり比率が上がり、買える株価の上限も上がる。
        # ATR%上限は逆に下がる（1敗が日次ブレーカーに届かない条件）。
        # **この連動を手で外すと安全装置が黙って無効になる。**
        weight = 1.0 / args.max_concurrent
        ceiling = int(max_affordable_price(CAPITAL, weight))
        filters = FilterConfig(
            price_normal_max=max(1, ceiling - 1),
            price_premium_max=ceiling,
        )
        selector = SelectorConfig(
            max_atr_pct=max_atr_pct(max_weight_per_symbol=weight),
            weights=weights,
        )
        print(f"  同時保有 {args.max_concurrent} 銘柄として選定する")
        print(
            f"    1銘柄比率 {weight:.0%} / 株価上限 {ceiling:,}円 / "
            f"ATR%上限 {selector.max_atr_pct:.2%} / "
            f"ATR円上限 {float(max_atr_yen(CAPITAL)):.1f}円（比率に依存しない）"
        )
    watchlist, counts, sel_costs = daily_watchlists(
        tuple(s for s in symbols if s.code in daily), daily, trading_days, filters, selector
    )
    filled = sum(1 for c in counts if c >= selector.max_watchlist)
    print(f"  1日あたりの監視銘柄: 平均 {sum(counts) / len(counts):.1f} / "
          f"最小 {min(counts)} / 最大 {max(counts)}")
    print(f"  枠({selector.max_watchlist})が埋まった日: {filled}/{len(counts)}")
    if sel_costs:
        ordered = sorted(sel_costs)
        mid = len(ordered) // 2
        print(
            f"  往復コスト(ATR): 中央値 {ordered[mid]:.4f} / "
            f"最安 {ordered[0]:.4f} / 最高 {ordered[-1]:.4f}"
            f"（**{ordered[-1] / ordered[0]:.1f}倍の開き**）"
        )
        print("     スプレッド円 ÷ ATR円。**選定がコストを見ているかの判定はこれ**")

    if min(counts) == 0:
        empty = [d for d, c in zip(trading_days, counts, strict=True) if c == 0]
        print(f"  **選定できなかった日が {len(empty)} 日ある**: {empty[:5]}")
        print("     日足が足りていない可能性。fetch_bars.py の日足期間を確認する")

    hr("3. バックテスト")
    shortable = shortable_symbols(daily)
    config = BacktestConfig(
        initial_cash=CAPITAL,
        shortable=shortable,
        enforce_breakers=not args.no_breakers,
        slippage_bps=STAGE_A_SLIPPAGE_BPS if args.flat_slippage else None,
        max_concurrent=args.max_concurrent or DEFAULT_MAX_CONCURRENT,
        max_weight_per_symbol=weight,
    )
    print(f"  初期資金      : {int(CAPITAL):,}円")
    print(
        f"  売建可能      : {len(shortable)}/{len(daily)}銘柄"
        f"（日次売買代金 {int(SHORTABLE_MIN_TURNOVER_YEN):,}円以上・Stage A の代理）"
    )
    if config.slippage_bps is not None:
        print(
            f"  スリッページ  : **固定** 片道{config.slippage_bps:.0f}bps"
            "（旧モデル。株価を見ない）"
        )
    else:
        print(
            f"  スリッページ  : 呼値から導出（スプレッド{config.spread_ticks:.0f}tick想定）"
            "。薄い銘柄は+5bps"
        )
        for sample in (600.0, 1250.0, 2200.0):
            print(
                f"      {sample:>6,.0f}円 → 片道 {half_spread_bps(sample):.1f}bps"
            )
    print(
        f"  同時保有/比率 : {config.max_concurrent}銘柄 / "
        f"1銘柄 {config.max_weight_per_symbol:.0%}"
    )
    print(f"  当日クローズ  : {config.close_time}")
    print(f"  ブレーカー    : {'有効' if config.enforce_breakers else '**無効**'}")
    if args.no_breakers:
        print("  → この成績を採用してはならない。寄与の測定にのみ使う")

    result = run(TakeIntraday(), intraday, config, watchlist)
    report(result, len(trading_days), "ブレーカー無効" if args.no_breakers else "通常")

    hr("4. この結果の読み方")
    print(f"  総トレード数 {result.n_trades} に対し、合格基準は > 100"
          f"（docs/07-go-live-criteria.md）")
    print(f"  {len(trading_days)}営業日は年率換算({TRADING_DAYS_PER_YEAR}日)の"
          f"{len(trading_days) / TRADING_DAYS_PER_YEAR:.0%}にすぎない。")
    print()
    print("  **ここで見るのはエンジンの健全性であって成績の合否ではない。**")
    print("  良い数字が出ても採用の根拠にしない。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
