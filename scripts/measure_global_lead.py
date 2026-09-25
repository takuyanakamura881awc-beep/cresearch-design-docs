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

MIN_HALF_DAYS = 15
"""期間分割した片側に要る最低日数。

**上位バケットは定義上サンプルが小さい。** 495営業日の上位10%は約50日で、
半分に割れば片側25日——`MIN_BUCKET_DAYS`(30) では**構造的に必ず判定不能**に
なってしまう。半期は定義上まるごと半分なので、そこだけ閾値を下げる。

**下げたぶん「判定不能」と「測って負け」を区別して出す**
（「レート制限エラーをデータなしと混同しない」と同じ原則）。
"""


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


CONTINUATION = 1
"""夜間の方向についていく（上げたら買い）。"""

REVERSAL = -1
"""夜間の方向に逆らう（上げたら売り）。"""

DIRECTIONS: tuple[tuple[str, int], ...] = (("継続", CONTINUATION), ("反転", REVERSAL))
"""**2つで1つの検定。** gross は符号が反転するだけなので変種を増やしていない。

**ただしコストは両方が払う**ので ``net(継続)`` と ``net(反転)`` が同時に
正になることはない。「向きを選べば必ず勝てる」形にはならない。
"""


def residual_score(
    overnight_move: float, day: MarketDay, sign: int = CONTINUATION
) -> float:
    """夜間の方向に ``sign`` を掛けて建てたときの、寄り付き後の取り分（bps）。

    **2つの向きは別の変種ではない。** 同じ1つの量の符号違いなので、
    多重比較の分母は増えない（この方針は結果を見る前に登録してある）。

    **だが判定は向きごとに回す必要がある**——コストは両方が払うので、
    ``net`` は単なる符号反転にならない（``+g-c`` と ``-g-c``）。
    初版は継続方向しか判定していなかったので、両方を回すよう直した。
    """
    if overnight_move > 0:
        return sign * day.intraday_bps
    if overnight_move < 0:
        return -sign * day.intraday_bps
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
    sign: int = CONTINUATION,
    *,
    min_days: int = MIN_BUCKET_DAYS,
) -> BucketStats | None:
    """``|夜間の変動|`` の順位が ``threshold`` 以上の日だけを集計する。

    **市場全体の1日を1観測として数える。** 銘柄ごとに数えると同じ市場の
    動きを何度も数えることになり、t値が過大に出る（意思決定ログ72）。
    """
    by_day = {d.day: d for d in days}
    samples = [
        (day, residual_score(overnight[day], by_day[day], sign))
        for day, rank in ranks.items()
        if rank >= threshold and day in by_day and day in overnight
    ]
    if len(samples) < min_days:
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
) -> dict[tuple[str, str], bool]:
    """セクション2: 寄り付き後に残っている部分。**ここだけが検定。**"""
    hr("2. 寄り付き後に残っているか（寄成で取りに行ける部分）")
    print("  夜間の方向に**ついていく（継続）／逆らう（反転）**の両方を出す。")
    print("  **同じ量の符号違いなので変種は増えていない**（結果を見る前に登録済み）。")
    print("  **ただしコストは両方が払う**ので、net が同時に正になることはない。")
    print()
    print("  シグナルは 05:00 JST に確定しているので、**寄成で板寄せに参加できる**")
    print("  ——ギャップ・フェードを殺した循環（意思決定ログ86）が起きない。")
    print()
    print(f"  順位は**直近{TRAILING_WINDOW}営業日の中での相対位置**。全期間の分位点を")
    print("  使うと事後診断になる（意思決定ログ47〜50 で回り道した）。")

    verdicts: dict[tuple[str, str], bool] = {}
    for spec in series:
        night = overnight.get(spec.key, {})
        rank = ranks.get(spec.key, {})
        print()
        print(f"  【{spec.key}】{spec.note}")
        if spec.continuous:
            print("  **24時間取引。日足の区切りが「東京から見た夜間」に対応しない**")
            print("  ——東京の日中セッションを含む24時間を1本にまとめている。")
            print("  **この系列の結果は「関係が無い」ではなく「測れていない」と読む。**")
        print(
            f"  {'|変動|順位':<10} {'日数':>7} {'gross(継続)':>12} {'コスト':>9} "
            f"{'net(継続)':>11} {'net(反転)':>11} {'t値(継続)':>9}"
        )
        print("  " + "-" * 76)
        for threshold in RANK_BUCKETS:
            cont = bucket_stats(night, rank, days, threshold, CONTINUATION)
            rev = bucket_stats(night, rank, days, threshold, REVERSAL)
            if cont is None or rev is None:
                print(f"  {threshold:>8.0%}以上 {'—（日数不足）':>20}")
                continue
            print(
                f"  {threshold:>8.0%}以上 {cont.n:>7} {cont.gross_bps:>+11.2f}b "
                f"{cont.cost_bps:>8.2f}b {cont.net_bps:>+10.2f}b "
                f"{rev.net_bps:>+10.2f}b {cont.t_stat:>9.1f}"
            )
        for label, sign in DIRECTIONS:
            print(f"    ［{label}］")
            all_stats = [
                bucket_stats(night, rank, days, t, sign) for t in RANK_BUCKETS
            ]
            verdicts[(spec.key, label)] = _verdict(
                night, rank, days, all_stats, sign
            )
    return verdicts


def _verdict(
    overnight: dict[date, float],
    ranks: dict[date, float],
    days: tuple[MarketDay, ...],
    all_stats: list[BucketStats | None],
    sign: int = CONTINUATION,
) -> bool:
    """事前登録した3条件を、**その向きについて**機械的に判定する。

    **結果を見てから基準を動かさないために、コードに埋め込む**（意思決定ログ87）。

    **初版は継続方向しか回していなかった。** `residual_score` の docstring で
    「反転は符号を反転して読めばよい」と事前登録していたのに、判定の実装が
    片方しか見ていなかった——**基準の変更ではなく、実装の取りこぼしの修正**。
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
    halves_measurable = False
    if second_days:
        top = RANK_BUCKETS[-1]
        halves = [
            bucket_stats(
                overnight,
                {d: r for d, r in ranks.items() if d in half},
                tuple(d for d in days if d.day in half),
                top,
                sign,
                min_days=MIN_HALF_DAYS,
            )
            for half in (first_days, second_days)
        ]
        halves_measurable = all(h is not None for h in halves)
        halves_positive = halves_measurable and all(
            h is not None and h.net_bps > 0 for h in halves
        )

    for label, passed in (
        ("① net が順位バケットで単調に改善する", monotone),
        ("② どこかのバケットで t値 >= 2 かつ net > 0", strong),
        ("③ 前半・後半とも最大バケットで net > 0", halves_positive),
    ):
        mark = "○" if passed else "×"
        if label.startswith("③") and not halves_measurable:
            # **「測れなかった」と「測って負け」を混同しない。**
            # どちらも合格にはしないが、次に何をすべきかが変わる
            mark = "?"
            label += "（判定不能: 片側が" + f"{MIN_HALF_DAYS}日に届かない）"
        print(f"    {mark} {label}")
    return monotone and strong and halves_positive


TRADING_DAYS_PER_YEAR = 245
"""年利に翻訳するときの営業日数。"""


def _report_capacity(
    series: tuple[ExternalSeries, ...],
    overnight: dict[str, dict[date, float]],
    ranks: dict[str, dict[date, float]],
    days: tuple[MarketDay, ...],
) -> dict[tuple[str, str], float]:
    """1回あたりの bps を**年利に翻訳する**。

    【この節を後から足した理由——判定基準に穴があった】

    事前登録した3条件は「**1回あたりの優位があるか**」しか見ていない。
    だが年利に効くのは ``net × 建てられる日数`` であって net 単独ではない。

    **選別すると建てる日が減る。** 上位10%のバケットで建てるということは、
    **年の9割は現金で寝ている**ということ。1回あたりが良くても
    回数が足りなければ年利は伸びない。

    **同じ罠を VIX のセクションでは織り込んでいた**
    （`measure_overnight_reversal.py` の `_report_vix_conditioning` は
    建玉率つきの必要 gross を出している）。**こちらで抜けていたのは
    一貫性の欠如で、基準を後から厳しくしたのではない。**

    `diagnostics.required_gross_bps` は最初から `deployment` を受け取る。
    使っていなかっただけ。

    Returns:
        ``{(系列, 向き): 年利%}``。
    """
    hr("3. 1回あたりの bps を年利に翻訳する")
    print("  **3条件は「1回あたりの優位」しか見ていない。**")
    print("  年利に効くのは `net × 建てられる日数`——**選別すると日数が減る。**")
    print("  上位10%で建てるとは、**年の9割を現金で寝かせる**ということ。")
    print()
    total = len(days)
    annuals: dict[tuple[str, str], float] = {}
    for spec in series:
        night = overnight.get(spec.key, {})
        rank = ranks.get(spec.key, {})
        print(f"  【{spec.key}】")
        print(
            f"  {'向き':<5} {'順位':>6} {'日数':>6} {'建玉率':>7} {'年間':>7} "
            f"{'net':>9} {'年利':>8} {'必要gross':>10} {'実測':>9} {'判定':>4}"
        )
        print("  " + "-" * 78)
        for label, sign in DIRECTIONS:
            for threshold in RANK_BUCKETS:
                stats = bucket_stats(night, rank, days, threshold, sign)
                if stats is None:
                    continue
                deployment = stats.n / total if total else 0.0
                per_year = deployment * TRADING_DAYS_PER_YEAR
                annual = stats.net_bps * per_year / 10_000.0 * 100.0
                need = required_gross_bps(
                    ANNUAL_TARGET, cost_bps=stats.cost_bps, deployment=deployment
                )
                annuals[(spec.key, label)] = max(
                    annuals.get((spec.key, label), -1e9), annual
                )
                ok = "○" if stats.gross_bps >= need else "×"
                print(
                    f"  {label:<5} {threshold:>5.0%} {stats.n:>6} {deployment:>6.1%} "
                    f"{per_year:>6.1f}日 {stats.net_bps:>+8.2f}b {annual:>+7.2f}% "
                    f"{need:>9.1f}b {stats.gross_bps:>+8.2f}b {ok:>4}"
                )
    print()
    print("  **必要gross が跳ね上がるのは、建玉率が分母に入るから**")
    print("  （意思決定ログ89 の「保有期間を延ばすと必要 gross が比例して上がる」")
    print("  と同じ算術）。選別も延長も、回転を落とすという意味では同じ。")
    return annuals


def _report_conclusion(
    verdicts: dict[tuple[str, str], bool],
    days: tuple[MarketDay, ...],
    annuals: dict[tuple[str, str], float],
) -> None:
    hr("4. 事前登録した結論")
    survivors = [f"{key}（{label}）" for (key, label), ok in verdicts.items() if ok]
    cost = statistics.median(d.cost_bps for d in days) if days else 0.0
    need = required_gross_bps(ANNUAL_TARGET, cost_bps=cost)
    print(f"  合格ライン: 年利{ANNUAL_TARGET:.0%}・建玉率100%・コスト{cost:.1f}bps")
    print(f"  → 必要 gross **{need:.1f}bps**（基準ではなく算術）")
    print()
    if not survivors:
        print("  → **どの系列も、どちらの向きも3条件を通らなかった。**")
        print("     夜間の海外の値動きは寄り付きで織り込み済みで、方向性の線は閉じる。")
        return

    print(f"  → **3条件を通った: {', '.join(survivors)}**")
    print()
    best = max(
        (annuals.get(k, 0.0) for k, ok in verdicts.items() if ok), default=0.0
    )
    print(f"  **だが年利に翻訳すると最良で {best:+.2f}%。**")
    print("  3条件は「1回あたりの優位」しか見ておらず、**建玉率を見ていなかった**")
    print("  （セクション3）。選別で日数が減るぶん、年利は目標に遠く届かない。")
    print()
    print("  **採用しない理由はほかに2つある。**")
    print()
    print("  1. **測っているのは市場全体の方向**であって銘柄固有の優位ではない。")
    print("     意思決定ログ71 で、まさにこの性質を「レバレッジ1倍・市場中立に")
    print("     近い設計を積んできた本システムとは**別の商品**」として棄却している。")
    print("     **対象を市場βに変えるかどうかは人間が判断すること**（CLAUDE.md）。")
    print()
    print("  2. **S&P500 と NASDAQ は互いに強く相関している。** 両方が通っても")
    print("     独立な2つの証拠ではなく、実質1つ。多重比較の分母として数えない。")
    print()
    print("  **out-of-sample の確認が要る。** 5分足が80営業日に届けば、")
    print("  日中の約定モデルで測り直せる。")


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
    annuals = _report_capacity(available, overnight, ranks, days)
    _report_conclusion(verdicts, days, annuals)

    print()
    print("**この診断の価値は、通らなかった場合にもある。** 夜間の情報が寄り付きで")
    print("織り込み済みだと確定すれば、方向性の線を探し続ける理由が消える。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
