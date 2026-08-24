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
import logging
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
from autotrader.provenance import banner
from autotrader.report.metrics import TRADING_DAYS_PER_YEAR
from autotrader.risk.limits import (
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_MAX_WEIGHT_PER_SYMBOL,
    max_atr_pct,
    max_atr_yen,
)
from autotrader.risk.sizing import average_turnover_of, max_affordable_price
from autotrader.strategy.random_baseline import RandomEntry, entry_probability_for
from autotrader.strategy.take_intraday import TakeIntraday, TakeIntradayConfig
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

EXPERIMENT_SEEDS = 20
"""`--experiment` でランダム分布を作るシード数。"""

DISTRIBUTION_DRIFT_LIMIT = 0.5
"""ランダム分布をエントリー確率をまたいで使い回してよいずれの上限。

**超えたら使い回さず、変種ごとに作り直す。** 警告を出して不正な比較を
続けるくらいなら、遅くても正しい比較をする。
"""

EXPERIMENT_VARIANTS: tuple[tuple[str, TakeIntradayConfig], ...] = (
    ("竹（現状）", TakeIntradayConfig()),
    ("ORB 反転", TakeIntradayConfig(invert_breakout=True)),
    ("VWAP乖離のみ 1.5%", TakeIntradayConfig(enable_breakout=False)),
    (
        "VWAP乖離のみ 1.0%",
        TakeIntradayConfig(enable_breakout=False, min_deviation_pct=0.010),
    ),
    (
        "VWAP乖離のみ 0.7%",
        TakeIntradayConfig(enable_breakout=False, min_deviation_pct=0.007),
    ),
)
"""検定にかける変種。**閾値を下げるのはサンプルを増やして検定可能にするため**で、
勝つ値を探しているのではない。勝ち負けはランダム分布との比較でしか判定しない。"""

MIN_BASELINE_SEEDS = 20
"""ランダムベースラインの最小シード数。

**1〜数本では分布にならない。** 竹が「上位20%に入った」と言うには
少なくともこれくらい要る。少ないシードでの比較は運と区別できない。
"""


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


def report_by_signal(result: BacktestResult) -> None:
    """エントリーシグナル別に損益を分解する。

    **竹は orb / orb+vwap / vwap_reversion を混ぜている。**
    全体が負けていても、片方だけ優位がある可能性は潰しておく。

    見るのは **gross**（コスト前）。net はコストの重い銘柄を多く引いた
    シグナルが不利に見えるだけで、優位の有無は判定できない。
    """
    if not result.trades:
        return
    groups: dict[str, list[Trade]] = collections.defaultdict(list)
    for trade in result.trades:
        groups[trade.entry_reason or "(不明)"].append(trade)

    print()
    print("  --- エントリーシグナル別（**見るのは gross**）---")
    print(
        f"  {'シグナル':<16} {'件数':>5} {'勝率':>7} {'gross計':>10} "
        f"{'gross/件':>9} {'cost/件':>8}"
    )
    for reason, trades in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        gross = sum(t.gross_pnl for t in trades)
        cost = sum(t.cost_yen for t in trades)
        wins = sum(1 for t in trades if t.pnl > 0)
        print(
            f"  {reason:<16} {len(trades):>5} {wins / len(trades):>6.1%} "
            f"{gross:>+9,.0f}円 {gross / len(trades):>+8,.0f}円 "
            f"{cost / len(trades):>7,.0f}円"
        )


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


def _gross_per_trade(result: BacktestResult) -> float:
    """1トレードあたりの gross（円）。**比較はこれで正規化する。**

    総額で比べるとトレード数の差が混ざる。変種ごとにトレード数は揃わない。
    """
    if result.n_trades == 0:
        return 0.0
    return sum(t.gross_pnl for t in result.trades) / result.n_trades


def random_distribution(
    intraday: dict[str, tuple[Bar, ...]],
    config: BacktestConfig,
    watchlist: dict[date, frozenset[str]],
    n_seeds: int,
    probability: float,
) -> list[tuple[float, int]]:
    """ランダムエントリーの ``(gross/件, トレード数)`` を n_seeds ぶん集める。

    **トレード数も返す。** これがないと、エントリー確率を変えても
    件数が動いていない（＝確率が効いていない）ことに気づけず、
    分布の使い回しの妥当性チェックが空回りする。

    **返済拒否の警告を抑える。** 各シードで別々に出るので、
    20本も回すと本題の結果が埋もれる。本番の1回では抑えない。
    """
    engine_logger = logging.getLogger("autotrader.engine.backtest")
    previous = engine_logger.level
    engine_logger.setLevel(logging.ERROR)
    try:
        values = []
        for seed in range(n_seeds):
            outcome = run(
                RandomEntry(seed=seed, entry_probability=probability),
                intraday,
                config,
                watchlist,
            )
            if outcome.n_trades:
                values.append((_gross_per_trade(outcome), outcome.n_trades))
        return values
    finally:
        engine_logger.setLevel(previous)


def _percentile_of(value: float, distribution: list[float]) -> float:
    """``value`` が分布の何割を上回るか。"""
    if not distribution:
        return 0.0
    return sum(1 for v in distribution if value > v) / len(distribution)


def _verdict(pct: float) -> str:
    if pct >= 0.95:
        return "仮説が生きている"
    if pct >= 0.80:
        return "示唆はあるが決定的でない"
    return "**棄却**"


def _baseline_probability(
    take: BacktestResult,
    intraday: dict[str, tuple[Bar, ...]],
    watchlist: dict[date, frozenset[str]],
) -> tuple[float, int, int]:
    n_bars = len({b.timestamp for bars in intraday.values() for b in bars})
    n_symbols = max((len(v) for v in watchlist.values()), default=1)
    return entry_probability_for(take.n_trades, n_symbols, n_bars), n_symbols, n_bars


def run_random_baseline(
    take: BacktestResult,
    intraday: dict[str, tuple[Bar, ...]],
    config: BacktestConfig,
    watchlist: dict[date, frozenset[str]],
    n_seeds: int,
    n_days: int,
) -> None:
    """エントリーだけをランダムにしたベースラインと比べる。

    **見るのは「竹の gross がランダムの分布のどこに落ちるか」だけ。**

    手仕舞い（1.5×ATR 損切り / 2.5×ATR 利確 / 180分 / 14:50クローズ）は
    それ自体が損益を生む。エントリーが何もしていなくても数字は動くので、
    **比較対象なしに「gross ≈ 0 だから優位がない」とは言えない。**
    """
    hr(f"ランダムエントリーとの比較（{n_seeds}シード）")
    if take.n_trades == 0:
        print("  竹のトレードが0件。比較できない")
        return

    probability, n_symbols, n_bars = _baseline_probability(take, intraday, watchlist)
    print(
        f"  エントリー確率 {probability:.5f}"
        f"（竹の{take.n_trades}トレードに合わせた目安。"
        f"{n_symbols}銘柄 × {n_bars}バー）"
    )
    print("  手仕舞い・同時保有数・レバレッジ・売建可否はすべて竹と同一")
    print()

    samples = random_distribution(intraday, config, watchlist, n_seeds, probability)
    if len(samples) < 2:
        print("  ランダム側がほとんど約定しなかった。確率の見積もりを疑う")
        return

    per_trade = [v for v, _ in samples]
    take_per_trade = _gross_per_trade(take)
    ordered = sorted(per_trade)
    pct = _percentile_of(take_per_trade, per_trade)
    median_count = sorted(n for _, n in samples)[len(samples) // 2]

    print(f"  {'':16} {'トレード数':>10} {'gross/件':>12}")
    print(f"  {'竹':16} {take.n_trades:>10} {take_per_trade:>+11,.1f}円")
    print(f"  {'ランダム 件数中央値':16} {median_count:>10}")
    print(f"  {'ランダム 中央値':16} {'':>10} {ordered[len(ordered) // 2]:>+11,.1f}円")
    print(f"  {'ランダム 最低':16} {'':>10} {ordered[0]:>+11,.1f}円")
    print(f"  {'ランダム 最高':16} {'':>10} {ordered[-1]:>+11,.1f}円")
    print()
    print(f"  **竹はランダム{len(per_trade)}本の {pct:.0%} を上回った** → {_verdict(pct)}")
    print()
    print(f"  注意: {n_days}営業日・パラメータ未検証。これは「竹の否定」ではなく")
    print("        「現パラメータの竹が、この期間で、ランダムと区別できるか」の検定")


def run_experiment(
    take: BacktestResult,
    intraday: dict[str, tuple[Bar, ...]],
    config: BacktestConfig,
    watchlist: dict[date, frozenset[str]],
    n_days: int,
) -> None:
    """変種をまとめて同じランダム分布に当てる。

    【分布を1回だけ計算する理由】

    変種ごとに20本回すと80バックテストになる。1トレードあたりで
    正規化しているので、分布はエントリー確率にほとんど依存しない。
    **依存しないことをここで実際に確かめてから使い回す。**
    """
    hr("変種の一括検定（ランダム分布に対するパーセンタイル）")
    if take.n_trades == 0:
        print("  竹のトレードが0件。比較できない")
        return

    probability, n_symbols, n_bars = _baseline_probability(take, intraday, watchlist)
    reusable = True
    print(f"  ランダム分布を {EXPERIMENT_SEEDS} シードで作る（p={probability:.5f}）")
    samples = random_distribution(
        intraday, config, watchlist, EXPERIMENT_SEEDS, probability
    )
    if len(samples) < 2:
        print("  ランダム側がほとんど約定しなかった。確率の見積もりを疑う")
        return
    distribution = [v for v, _ in samples]

    # **使い回してよいかを確かめる。** p を2倍にして中央値が動かないこと
    doubled = random_distribution(
        intraday, config, watchlist, EXPERIMENT_SEEDS, min(1.0, probability * 2)
    )
    ordered = sorted(distribution)
    base_median = ordered[len(ordered) // 2]
    base_count = sorted(n for _, n in samples)[len(samples) // 2]
    if doubled:
        other_values = sorted(v for v, _ in doubled)
        other = other_values[len(other_values) // 2]
        other_count = sorted(n for _, n in doubled)[len(doubled) // 2]
        drift = abs(other - base_median)
        scale = max(abs(base_median), abs(other), 1.0)
        print(
            f"  p 倍化の確認: 件数 {base_count} → {other_count} / "
            f"中央値 {base_median:+.1f}円 → {other:+.1f}円（ずれ {drift / scale:.0%}）"
        )
        if other_count == base_count:
            # **確率を変えても件数が動いていない = 検定になっていない**
            print("     **件数が動いていない。上限（1銘柄1日1回・同時保有数）で")
            print("     飽和しており、この確認は成立していない**")
            reusable = False
        elif drift / scale > DISTRIBUTION_DRIFT_LIMIT:
            print("     **分布がエントリー確率に敏感。変種ごとに作り直す**")
            reusable = False

    print(
        f"  ランダム: 中央値 {base_median:+.1f}円 / "
        f"最低 {ordered[0]:+.1f}円 / 最高 {ordered[-1]:+.1f}円"
    )
    print()
    print(f"  {'変種':<26} {'件数':>6} {'gross/件':>10} {'順位':>7}  判定")
    print("  " + "-" * 70)

    engine_logger = logging.getLogger("autotrader.engine.backtest")
    previous_level = engine_logger.level
    engine_logger.setLevel(logging.ERROR)
    try:
        for label, cfg in EXPERIMENT_VARIANTS:
            outcome = run(TakeIntraday(cfg), intraday, config, watchlist)
            if outcome.n_trades == 0:
                print(f"  {label:<26} {0:>6}  —  トレードが出ない")
                continue
            if reusable:
                against = distribution
            else:
                # **その変種のトレード数に合わせて作り直す。**
                # 使い回せないと分かっているのに使うのは検定ではない
                against = [
                    v
                    for v, _ in random_distribution(
                        intraday,
                        config,
                        watchlist,
                        EXPERIMENT_SEEDS,
                        entry_probability_for(outcome.n_trades, n_symbols, n_bars),
                    )
                ]
                if len(against) < 2:
                    print(f"  {label:<26} {outcome.n_trades:>6}  —  分布を作れない")
                    continue
            value = _gross_per_trade(outcome)
            pct = _percentile_of(value, against)
            print(
                f"  {label:<26} {outcome.n_trades:>6} {value:>+9,.1f}円 "
                f"{pct:>6.0%}  {_verdict(pct)}"
            )
    finally:
        engine_logger.setLevel(previous_level)

    print()
    print(f"  **{n_days}営業日は in-sample。ここで勝っても採用しない。**")
    print(f"  {len(EXPERIMENT_VARIANTS)}変種を試している以上、偶然95%を超えるものが")
    print(
        f"  出る確率は約{1 - 0.95 ** len(EXPERIMENT_VARIANTS):.0%}。"
        "勝った変種は**仮説として記録するだけ**で、"
    )
    print("  確認は5分足が80営業日たまってから out-of-sample で行う。")


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
        "--experiment",
        action="store_true",
        help=(
            "竹と複数の変種（ORB反転・VWAP乖離のみ）を、"
            "**すべて同じランダム分布に対するパーセンタイル**で並べる。"
            "39営業日は in-sample なので、勝っても採用せず仮説として記録する"
        ),
    )
    parser.add_argument(
        "--invert-breakout",
        action="store_true",
        help="シグナルA の方向を反転する（上抜けで売り）。**仮説検定用**",
    )
    parser.add_argument(
        "--disable-breakout",
        action="store_true",
        help="シグナルA を切って VWAP乖離だけにする",
    )
    parser.add_argument(
        "--min-deviation-pct",
        type=float,
        default=None,
        metavar="P",
        help="VWAP乖離の下限（既定0.015）。下げるとサンプルが増える",
    )
    parser.add_argument(
        "--random-baseline",
        type=int,
        default=0,
        metavar="N",
        help=(
            "エントリーだけをランダムにしたベースラインを N シード回し、"
            "竹の gross がその分布のどこに落ちるかを出す。20〜30 を推奨"
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
    if args.random_baseline < 0:
        parser.error("--random-baseline は0以上")
    if 0 < args.random_baseline < MIN_BASELINE_SEEDS:
        parser.error(
            f"--random-baseline は {MIN_BASELINE_SEEDS} 以上にする。"
            "少ないシードでは分布にならず、1点対1点の比較と変わらない"
        )

    print("竹を実データでバックテストする")
    print(banner())

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

    strategy_config = TakeIntradayConfig(
        enable_breakout=not args.disable_breakout,
        invert_breakout=args.invert_breakout,
        **(
            {"min_deviation_pct": args.min_deviation_pct}
            if args.min_deviation_pct is not None
            else {}
        ),
    )
    if args.disable_breakout or args.invert_breakout or args.min_deviation_pct:
        print(
            f"  **変種**: ブレイク {'切' if args.disable_breakout else '入'}"
            f" / 反転 {'あり' if args.invert_breakout else 'なし'}"
            f" / VWAP乖離下限 {strategy_config.min_deviation_pct:.1%}"
        )

    result = run(TakeIntraday(strategy_config), intraday, config, watchlist)
    report(result, len(trading_days), "ブレーカー無効" if args.no_breakers else "通常")
    report_by_signal(result)

    if args.experiment:
        run_experiment(result, intraday, config, watchlist, len(trading_days))
    elif args.random_baseline:
        run_random_baseline(
            result, intraday, config, watchlist, args.random_baseline, len(trading_days)
        )

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
