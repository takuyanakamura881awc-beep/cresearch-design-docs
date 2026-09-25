#!/usr/bin/env python3
"""海外の夜間の値動きは、東京の寄り付きに**どこまで織り込まれているか**。

    python scripts/measure_global_lead.py --refresh   # 外部系列を取り直す
    python scripts/measure_global_lead.py             # 手元のキャッシュで測る

【なぜこの診断なのか】

7家族すべてが「優位 0〜5bps < コスト 5〜13bps」で死んだ（意思決定ログ88・101）。
手がかりはすべて**日本株の価格そのもの**から作っていた。

外部指数にはこれまでの手がかりに無い性質が1つある——**確定時刻**。
米国の引けは 16:00 ET ＝ **翌 05:00 JST** で、東京の寄り付き 9:00 の4時間前。
執行可能性ゲート質問1（シグナルは何時に確定するか）を**構造的に通る**。
ギャップ・フェードが死んだ循環（始値が要るので板寄せに参加できない）が起きない。

【だが最大の反論を先に潰す】

**日経平均先物は CME・SGX・大阪の夜間セッションでほぼ24時間動いている。**
東京が寄り付く時点で、夜間の海外の値動きは**既に始値に織り込まれている**。
だから「ギャップ」が存在する。

「米国が下げた → 翌日の日本も下げる」は**予測ではなく、始値がもう
下がっているだけ**。**織り込み率を測ってからでないと、残りを解釈できない。**

【事前登録した判定（結果を見る前に固定する）】

セクション2（寄り付き後に残っている部分）だけが検定。

1. ``|夜間の変動|`` の順位バケットで **net(日) が単調に改善する**
2. どこかのバケットで **t値(日) >= 2 かつ net(日) > 0**
3. **前半・後半とも最大バケットで net(日) > 0**

**3つとも通って初めて「織り込み残りがある」と言う。** どれか1つでも外せば、
方向性の線（夜間の海外 → 翌日の東京）はここで閉じる。

【通っても自動では採用しない】

セクション2が測っているのは**市場全体の方向**であって、銘柄固有の優位ではない。
意思決定ログ71 で、まさにこの性質を「レバレッジ1倍・市場中立に近い設計を
積んできた本システムとは別の商品」として棄却している。

**通った場合は人間の判断に出す**（`CLAUDE.md`「人間が判断すること」）。
私からは材料を出すところまで。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from autotrader.data.external import (
    DEFAULT_SERIES,
    TRAILING_WINDOW,
    ExternalSeries,
    align_prior_session,
    fetch_external,
    log_returns,
    trailing_rank,
)
from autotrader.data.store import BarStore
from autotrader.diagnostics import (
    clustered_stats,
    drop_discontinuous_symbols,
    required_gross_bps,
    split_days,
)
from autotrader.execution_model import EntryStyle, ExitStyle
from autotrader.execution_model import round_trip_cost_bps as execution_cost_bps
from autotrader.provenance import banner
from autotrader.types import Bar, Symbol

DATA_ROOT = Path("data")

ANNUAL_TARGET = 0.25
"""目標年利（意思決定ログ73）。必要 gross の逆算に使う。"""

EXTERNAL_LOOKBACK_DAYS = 1100
"""外部系列を遡る日数。

日足に取得量の制限はないので、`TRAILING_WINDOW`（250）の助走を確保したうえで
日本株の日足2年（730日）と重なる範囲を十分に覆う。
"""

RANK_BUCKETS: tuple[float, ...] = (0.50, 0.75, 0.90)
"""``|夜間の変動|`` の順位の下限バケット。

3つに絞ってある。**外部系列5本 × バケット3で15セル**あり、これ以上増やすと
多重比較で偶然を拾う（意思決定ログ76 で約120セルを「証拠にならない」と記録済み）。
"""

MIN_BUCKET_DAYS = 30
"""バケットの判定に要る最低日数。**足りなければ「判定不能」と出す。**"""


@dataclass(frozen=True)
class MarketDay:
    """1営業日ぶんの、日本市場の平均的な値動き。

    **個別銘柄ではなく市場全体。** 夜間の海外の影響は市場要因として効くので、
    銘柄ごとに見ると市場の動きを何度も数えることになる（意思決定ログ72）。
    """

    day: date
    gap_bps: float
    """前日終値 → 当日始値。**夜間に織り込まれたぶん。**"""
    intraday_bps: float
    """当日始値 → 当日終値。**寄成で建てて大引けで手仕舞えば取れるぶん。**"""
    cost_bps: float
    """往復コスト。銘柄ごとの株価から出した中央値。"""
    symbols: int


def market_days(
    daily_bars: dict[str, tuple[Bar, ...]],
    topix100_codes: frozenset[str] = frozenset(),
    *,
    entry: EntryStyle = EntryStyle.MARKET,
    exit_: ExitStyle = ExitStyle.MARKET,
) -> tuple[MarketDay, ...]:
    """日足から、営業日ごとの市場平均のギャップと日中リターンを作る。

    **ルックアヘッドは構造的に防いでいる**（規約7）——各日の値は当日と前日の
    バーだけから決まり、未来のバーを一切参照しない。

    コストは `autotrader.execution_model` をそのまま使う。**診断ごとに
    コストモデルを作り直さない**（意思決定ログ33以降）。
    """
    gaps: dict[date, list[float]] = {}
    intradays: dict[date, list[float]] = {}
    costs: dict[date, list[float]] = {}

    for symbol in sorted(daily_bars):
        ordered = sorted(daily_bars[symbol], key=lambda b: b.timestamp)
        for prev, today in zip(ordered, ordered[1:], strict=False):
            if prev.close <= 0 or today.open <= 0:
                continue
            day = today.timestamp.date()
            gaps.setdefault(day, []).append(
                (today.open - prev.close) / prev.close * 10_000.0
            )
            intradays.setdefault(day, []).append(
                (today.close - today.open) / today.open * 10_000.0
            )
            costs.setdefault(day, []).append(
                execution_cost_bps(
                    today.open,
                    entry=entry,
                    exit_=exit_,
                    topix100=symbol in topix100_codes,
                )
            )

    return tuple(
        MarketDay(
            day=day,
            gap_bps=statistics.fmean(gaps[day]),
            intraday_bps=statistics.fmean(intradays[day]),
            cost_bps=statistics.median(costs[day]),
            symbols=len(gaps[day]),
        )
        for day in sorted(gaps)
    )


@dataclass(frozen=True)
class PricedIn:
    """夜間の変動が寄り付きにどれだけ入っているか。"""

    days: int
    slope: float
    """夜間の変動1%あたり、始値が何bps動いたか。"""
    r_squared: float
    """**これが高いほど「もう織り込まれている」。**"""


def priced_in(
    overnight: dict[date, float], days: tuple[MarketDay, ...]
) -> PricedIn | None:
    """``市場のギャップ = a + b × 夜間の変動`` を最小二乗で解く。

    **これは仮説検定ではなく事実確認。** 織り込み率を知らないと、
    寄り付き後に残っている部分を解釈できない。

    Returns:
        共通する日が3日未満、または夜間の変動に散らばりが無ければ ``None``。
    """
    pairs = [(overnight[d.day], d.gap_bps) for d in days if d.day in overnight]
    if len(pairs) < 3:
        return None
    xs = [x for x, _ in pairs]
    ys = [y for _, y in pairs]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    slope = sxy / sxx
    syy = sum((y - mean_y) ** 2 for y in ys)
    r_squared = (sxy * sxy / (sxx * syy)) if syy > 0 else 0.0
    # slope は「対数リターン1あたりのbps」なので、1% あたりに直す
    return PricedIn(days=len(pairs), slope=slope * 0.01, r_squared=r_squared)


def residual_score(overnight_move: float, day: MarketDay) -> float:
    """夜間の方向についていったときの、寄り付き後の取り分（bps）。

    夜間が上げなら買い、下げなら売り。**継続を賭ける向き**にそろえる
    ——反転を賭けたいなら符号を反転して読めばよいので、両方を別の変種として
    数えない（多重比較の分母を増やさない）。
    """
    if overnight_move > 0:
        return day.intraday_bps
    if overnight_move < 0:
        return -day.intraday_bps
    return 0.0


@dataclass(frozen=True)
class BucketStats:
    """1バケットぶんの集計。**`measure_overnight_reversal.py` と同じ形にそろえる。**"""

    n: int
    gross_bps: float
    cost_bps: float
    t_stat: float

    @property
    def net_bps(self) -> float:
        return self.gross_bps - self.cost_bps


def bucket_stats(
    overnight: dict[date, float],
    ranks: dict[date, float],
    days: tuple[MarketDay, ...],
    threshold: float,
) -> BucketStats | None:
    """``|夜間の変動|`` の順位が ``threshold`` 以上の日だけを集計する。

    **市場全体の1日を1観測として数える。** 銘柄ごとに数えると同じ市場の
    動きを何度も数えることになり、t値が過大に出る（意思決定ログ72）。
    """
    by_day = {d.day: d for d in days}
    samples = [
        (day, residual_score(overnight[day], by_day[day]))
        for day, rank in ranks.items()
        if rank >= threshold and day in by_day and day in overnight
    ]
    if len(samples) < MIN_BUCKET_DAYS:
        return None
    stats = clustered_stats(samples)
    if stats is None:
        return None
    return BucketStats(
        n=len(samples),
        gross_bps=stats.mean_bps,
        cost_bps=statistics.median(by_day[day].cost_bps for day, _ in samples),
        t_stat=stats.t_stat,
    )


def hr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def _report_priced_in(
    series: tuple[ExternalSeries, ...],
    overnight: dict[str, dict[date, float]],
    days: tuple[MarketDay, ...],
) -> None:
    """セクション1: 夜間の変動は寄り付きに入っているか。**事実確認。**"""
    hr("1. 夜間の変動は、寄り付きに既に織り込まれているか")
    print("  **日経平均先物は夜間もほぼ24時間動いている。** 東京が寄り付く時点で")
    print("  夜間の海外の値動きは始値に反映されているはず——だから「ギャップ」がある。")
    print()
    print("  市場のギャップ（前日終値→始値の全銘柄平均）を、夜間の変動で説明する。")
    print("  **R²が高いほど「もう織り込まれている」。** これは検定ではなく事実確認。")
    print()
    print(f"  {'系列':<8} {'日数':>6} {'1%あたり':>10} {'R²':>8}  メモ")
    print("  " + "-" * 62)
    for spec in series:
        fit = priced_in(overnight.get(spec.key, {}), days)
        if fit is None:
            print(f"  {spec.key:<8} {'—':>6} {'—':>10} {'—':>8}  データ不足")
            continue
        print(
            f"  {spec.key:<8} {fit.days:>6} {fit.slope:>+9.1f}b {fit.r_squared:>8.3f}"
            f"  {spec.note}"
        )
    print()
    print("  **R²が高い系列ほど、方向性の賭けとしては手遅れ**——始値がもう動いている。")
    print("  低い系列は「そもそも関係が薄い」のであって、優位があるとは限らない。")


def _format_bucket(stats: BucketStats | None) -> str:
    if stats is None:
        return f"{'—':>7} {'—':>9} {'—':>9} {'—':>9} {'—':>6}"
    return (
        f"{stats.n:>7} {stats.gross_bps:>+8.2f}b {stats.cost_bps:>8.2f}b "
        f"{stats.net_bps:>+8.2f}b {stats.t_stat:>6.1f}"
    )


def _report_residual(
    series: tuple[ExternalSeries, ...],
    overnight: dict[str, dict[date, float]],
    ranks: dict[str, dict[date, float]],
    days: tuple[MarketDay, ...],
) -> dict[str, bool]:
    """セクション2: 寄り付き後に残っている部分。**ここだけが検定。**"""
    hr("2. 寄り付き後に残っているか（寄成で取りに行ける部分）")
    print("  夜間が上げた日は買い、下げた日は売り、**始値で建てて大引けで手仕舞う**。")
    print("  シグナルは 05:00 JST に確定しているので、**寄成で板寄せに参加できる**")
    print("  ——ギャップ・フェードを殺した循環（意思決定ログ86）が起きない。")
    print()
    print(f"  順位は**直近{TRAILING_WINDOW}営業日の中での相対位置**。全期間の分位点を")
    print("  使うと事後診断になる（意思決定ログ47〜50 で回り道した）。")

    verdicts: dict[str, bool] = {}
    for spec in series:
        night = overnight.get(spec.key, {})
        rank = ranks.get(spec.key, {})
        print()
        print(f"  【{spec.key}】{spec.note}")
        print(
            f"  {'|変動|順位':<10} {'日数':>7} {'gross':>9} {'コスト':>9} "
            f"{'net':>9} {'t値':>6}"
        )
        print("  " + "-" * 56)
        all_stats = [bucket_stats(night, rank, days, t) for t in RANK_BUCKETS]
        for threshold, stats in zip(RANK_BUCKETS, all_stats, strict=True):
            print(f"  {threshold:>8.0%}以上 {_format_bucket(stats)}")
        verdicts[spec.key] = _verdict(night, rank, days, all_stats)
    return verdicts


def _verdict(
    overnight: dict[date, float],
    ranks: dict[date, float],
    days: tuple[MarketDay, ...],
    all_stats: list[BucketStats | None],
) -> bool:
    """事前登録した3条件を機械的に判定する。

    **結果を見てから基準を動かさないために、コードに埋め込む**（意思決定ログ87）。
    """
    nets = [s.net_bps for s in all_stats if s is not None]
    monotone = len(nets) == len(RANK_BUCKETS) and all(
        b >= a for a, b in zip(nets, nets[1:], strict=False)
    )
    strong = any(
        s is not None and s.t_stat >= 2.0 and s.net_bps > 0 for s in all_stats
    )

    first_days, second_days = split_days(d.day for d in days)
    halves_positive = False
    if second_days:
        top = RANK_BUCKETS[-1]
        halves_positive = all(
            (
                s := bucket_stats(
                    overnight,
                    {d: r for d, r in ranks.items() if d in half},
                    tuple(d for d in days if d.day in half),
                    top,
                )
            )
            is not None
            and s.net_bps > 0
            for half in (first_days, second_days)
        )

    for label, passed in (
        ("① net が順位バケットで単調に改善する", monotone),
        ("② どこかのバケットで t値 >= 2 かつ net > 0", strong),
        ("③ 前半・後半とも最大バケットで net > 0", halves_positive),
    ):
        print(f"    {'○' if passed else '×'} {label}")
    return monotone and strong and halves_positive


def _report_conclusion(verdicts: dict[str, bool], days: tuple[MarketDay, ...]) -> None:
    hr("3. 事前登録した結論")
    survivors = [k for k, ok in verdicts.items() if ok]
    cost = statistics.median(d.cost_bps for d in days) if days else 0.0
    need = required_gross_bps(ANNUAL_TARGET, cost_bps=cost)
    print(f"  合格ライン: 年利{ANNUAL_TARGET:.0%}・建玉率100%・コスト{cost:.1f}bps")
    print(f"  → 必要 gross **{need:.1f}bps**（基準ではなく算術）")
    print()
    if not survivors:
        print("  → **どの系列も3条件を通らなかった。**")
        print("     夜間の海外の値動きは寄り付きで織り込み済みで、方向性の線は閉じる。")
        print("     残るのは (a) 銘柄ごとの織り込みの過不足（断面）、")
        print("     (b) VIX による**リバーサルの効き方**の条件づけ（Nagel 2012）。")
        print()
        print("     (b) は次のコマンドで測る:")
        print("       python scripts/measure_overnight_reversal.py --cheap --by-vix")
        return

    print(f"  → **3条件を通った系列: {', '.join(survivors)}**")
    print()
    print("  **だが自動では採用しない。** ここで測っているのは市場全体の方向で、")
    print("  銘柄固有の優位ではない。意思決定ログ71 で、まさにこの性質を")
    print("  「レバレッジ1倍・市場中立に近い設計を積んできた本システムとは")
    print("  **別の商品**」として棄却している。")
    print()
    print("  **対象を市場βに変えるかどうかは人間が判断すること**（CLAUDE.md）。")
    print("  私からは材料を出すところまで。")


def load_symbols(*, cheap: bool) -> tuple[Symbol, ...]:
    """銘柄一覧。既定は**コストで切り出した清浄なユニバース**（意思決定ログ99）。"""
    path = DATA_ROOT / ("universe_cheap.json" if cheap else "universe.json")
    if not path.is_file():
        raise SystemExit(
            f"{path} がない。先に "
            "python scripts/measure_cost_landscape.py --refresh を実行する"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        Symbol(
            code=str(r["code"]),
            name=str(r["name"]),
            market=r.get("market"),
            margin_type=r.get("margin_type"),
            sector=r.get("sector"),
            scale_category=r.get("scale_category"),
        )
        for r in payload["symbols"]
    )


def _refresh(store: BarStore) -> None:
    """外部系列を取り直して保存する。

    **日足に取得量の制限はない**ので、5分足のような週次の蓄積は要らない
    （取らなかった週が永久に失われるのは5分足だけ）。いつ実行してもよい。
    """
    hr("外部系列を取得する")
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=EXTERNAL_LOOKBACK_DAYS)
    print(f"  {len(DEFAULT_SERIES)}系列 × {start} 〜 {end}")
    fetched = fetch_external(DEFAULT_SERIES, start, end)
    for spec in DEFAULT_SERIES:
        bars = fetched.get(spec.key, ())
        if not bars:
            print(f"    {spec.key:<8} 取得できず（{spec.ticker}）")
            continue
        store.write(spec.key, "1d", bars)
        store.record_fetch(spec.key, "1d", start, end, "yfinance", len(bars))
        print(f"    {spec.key:<8} {len(bars):>5}本  {bars[0].timestamp.date()} 〜 "
              f"{bars[-1].timestamp.date()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh", action="store_true", help="外部系列を yfinance から取り直す"
    )
    parser.add_argument(
        "--layer1",
        action="store_true",
        help="Layer 1 の銘柄群で測る（既定はコストで切り出した清浄なユニバース）",
    )
    args = parser.parse_args()

    print("海外の夜間の値動きは、東京の寄り付きにどこまで織り込まれているか")
    print(banner())

    store = BarStore(DATA_ROOT)
    if args.refresh:
        _refresh(store)

    symbols = load_symbols(cheap=not args.layer1)
    print(f"  日本株: {len(symbols)}銘柄")
    topix100_codes = frozenset(s.code for s in symbols if s.is_topix100)

    daily = {s.code: store.read(s.code, "1d") for s in symbols}
    daily = {c: b for c, b in daily.items() if b}
    print(f"  日足あり: {len(daily)}銘柄")

    hr("0. データの健全性（集計より先に確かめる）")
    daily = drop_discontinuous_symbols(daily)
    if not daily:
        print("  健全な日足が残らなかった")
        return 1

    days = market_days(daily, topix100_codes)
    if len(days) < MIN_BUCKET_DAYS:
        print("  営業日が足りない")
        return 1
    print()
    print(f"  市場の営業日: {len(days)}日（{days[0].day} 〜 {days[-1].day}）")

    overnight: dict[str, dict[date, float]] = {}
    ranks: dict[str, dict[date, float]] = {}
    jp_days = [d.day for d in days]
    missing: list[str] = []
    for spec in DEFAULT_SERIES:
        bars = store.read(spec.key, "1d")
        if not bars:
            missing.append(spec.key)
            continue
        returns = log_returns(bars)
        # **順位付けは外部系列自身の時間軸で行う。**
        # 日本の営業日に写してから順位を付けると、助走の250日が
        # **日本側の観測を食いつぶす**（2年しかないので上位バケットが
        # 判定不能になる）。外部系列は EXTERNAL_LOOKBACK_DAYS ぶん持っている
        # ので、助走はそちらで吸収させる。
        ext_ranks = trailing_rank({d: abs(v) for d, v in returns.items()})
        # **外部日付 < 日本の営業日** を厳格に守る（先読み防止・規約7）
        mapping = align_prior_session(returns, jp_days)
        overnight[spec.key] = {
            jp: returns[ext] for jp, ext in mapping.items() if ext in returns
        }
        ranks[spec.key] = {
            jp: ext_ranks[ext] for jp, ext in mapping.items() if ext in ext_ranks
        }
    if missing:
        print(f"  **外部系列が無い: {', '.join(missing)}** → --refresh で取得する")
    available = tuple(s for s in DEFAULT_SERIES if s.key in overnight)
    if not available:
        print("  外部系列が1本も無い。python scripts/measure_global_lead.py --refresh")
        return 1

    _report_priced_in(available, overnight, days)
    verdicts = _report_residual(available, overnight, ranks, days)
    _report_conclusion(verdicts, days)

    print()
    print("**この診断の価値は、通らなかった場合にもある。** 夜間の情報が寄り付きで")
    print("織り込み済みだと確定すれば、方向性の線を探し続ける理由が消える。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
