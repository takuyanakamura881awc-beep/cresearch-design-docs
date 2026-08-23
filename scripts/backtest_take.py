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

from autotrader.broker.replay import SHORTABLE_MIN_TURNOVER_YEN
from autotrader.data.store import BarStore
from autotrader.engine.backtest import BacktestConfig, BacktestResult, run
from autotrader.report.metrics import TRADING_DAYS_PER_YEAR
from autotrader.risk.sizing import average_turnover_of
from autotrader.strategy.take_intraday import TakeIntraday
from autotrader.types import Bar, PriceTier, Symbol
from autotrader.universe.filters import FilterConfig, screen
from autotrader.universe.selector import SelectorConfig, build_candidates, select

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
) -> tuple[dict[date, frozenset[str]], list[int]]:
    """営業日ごとに Layer 1 → Layer 2 を回して監視銘柄を決める。

    **その日より前の日足だけを使う。** `screen` に渡すバーも
    `build_candidates` に渡すバーも当日を含めない。
    当日の終値で流動性や株価を判定すると、寄り前に知りえない情報で選ぶことになる。

    Returns:
        (営業日 → 監視銘柄, 日ごとの選定数)。
    """
    watchlist: dict[date, frozenset[str]] = {}
    counts: list[int] = []

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

    return watchlist, counts


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


def report(result: BacktestResult, n_days: int, label: str) -> None:
    hr(f"結果（{label}）")
    print(f"  期間            : {n_days}営業日")
    print(f"  トレード数      : {result.n_trades}")
    if result.n_trades:
        print(f"  勝率            : {result.win_rate:.1%}")
        pf = result.profit_factor
        print(f"  プロフィットファクタ: {'∞（要検査）' if pf == float('inf') else f'{pf:.2f}'}")
    print(f"  総リターン      : {result.total_return:+.2%}")

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
        if sum(per_day.values()) / traded > 5:
            print("    → 取引した日は想定（3〜5回）を超えている。発火条件が緩い可能性")
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
    args = parser.parse_args()

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
    filters = FilterConfig()
    selector = SelectorConfig()
    watchlist, counts = daily_watchlists(
        tuple(s for s in symbols if s.code in daily), daily, trading_days, filters, selector
    )
    filled = sum(1 for c in counts if c >= selector.max_watchlist)
    print(f"  1日あたりの監視銘柄: 平均 {sum(counts) / len(counts):.1f} / "
          f"最小 {min(counts)} / 最大 {max(counts)}")
    print(f"  枠({selector.max_watchlist})が埋まった日: {filled}/{len(counts)}")
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
    )
    print(f"  初期資金      : {int(CAPITAL):,}円")
    print(
        f"  売建可能      : {len(shortable)}/{len(daily)}銘柄"
        f"（日次売買代金 {int(SHORTABLE_MIN_TURNOVER_YEN):,}円以上・Stage A の代理）"
    )
    print(f"  スリッページ  : 片道{config.slippage_bps:.0f}bps（薄い銘柄は+5bps）")
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
