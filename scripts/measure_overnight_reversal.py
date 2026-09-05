#!/usr/bin/env python3
"""前日の値動きが翌日の寄り付きで行き過ぎ、その後戻るか——**寄成で建てられる初めての候補**。

    python scripts/measure_overnight_reversal.py
    python scripts/measure_overnight_reversal.py --topix100

事前に ``python scripts/fetch_bars.py`` で ``data/`` に日足を蓄積しておく
（新規のネットワーク取得は行わない。ローカルの ``BarStore`` だけを読む）。

【なぜこの仮説なのか】

4つの手法がすべて棄却された（`docs/00-overview.md` 意思決定ログ46・52・56・84）。
**そのうち最後のギャップ・フェードから、初めて「なぜ死んだか」を一般化できた。**

実測（意思決定ログ86）: エントリーを遅らせたときの優位は

    0分 -0.2bps → 5分 -18.7 → 15分 -22.1 → 30分 -13.7 → 60分 -13.0

**5分で崖のように落ちた後は横ばい。** つまり優位の正体は**板寄せの
オーバーシュート**——単一価格で約定するので、その直後に実勢へ寄る。
**遅延そのものは問題ではなく、板寄せの約定値を取れるかどうかが分かれ目だった。**

そこから出た設計原則:

===============================  ==================  ==========================
シグナルの確定時点               板寄せの価格        判定
===============================  ==================  ==========================
**前日引けまでの情報だけ**       寄成で**取れる**    この制約から自由
**当日の始値が要る**             循環して**取れない**  Stage A では成立しない
===============================  ==================  ==========================

ギャップは ``(当日始値 - 前日終値) / 前日終値`` なので後者だった。
**この診断が測るのは前者**——シグナルを前日大引けまでの情報だけで作れば、
寄成注文が使えて板寄せの価格を取れる。

【仮説】

前日に大きく動いた銘柄は、翌日の板寄せで行き過ぎ、寄り付き後に戻す。

===========  ==============================================================
シグナル     ``前日リターン = (前日終値 - 前々日終値) / 前々日終値``
             **前日大引け時点で確定する**
エントリー   寄成（＝当日始値）
手仕舞い     大引け。14:50 との差は +2.48bps で無害と実測済み（意思決定ログ82）
スコア       ``-sign(前日リターン) × (当日始値→終値)``。前日と逆に動けば正
===========  ==============================================================

【判定基準（結果を見る前に固定した・意思決定ログ87）】

1. ``|前日リターン|`` の下限バケットで **net が単調に改善する**
2. **日クラスタ t値**（件数ベースではない）が全期間で 2 以上
3. **前半・後半とも net が正**で、単調性が両方で保たれる

**3つとも通って初めて、5分足での検証に進む。** どれか1つでも外せば棄却。

【セクション0（データの健全性）を先に見ること】

日をまたぐ値（``prior_move_pct`` と保有期間の将来リターン）は、日足に
**価格水準の継ぎ目**があるとそこだけ嘘になる。同じバーの中で完結する値は
無傷なので、**平均を見てからでは気づけない**（意思決定ログ97）。
継ぎ目が出たらその銘柄の日足を作り直してから読む。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from autotrader.data.store import BarStore
from autotrader.diagnostics import (
    ClusteredStats,
    clustered_stats,
    drop_discontinuous_symbols,
    non_overlapping_days,
    required_gross_bps,
    split_days,
)
from autotrader.provenance import banner
from autotrader.tick import DEFAULT_SPREAD_TICKS, spread_yen
from autotrader.types import Bar, Symbol

DATA_ROOT = Path("data")

ANNUAL_TARGET = 0.25
"""目標年利（意思決定ログ73）。**必要な gross を逆算するのに使う。**"""

HORIZONS: tuple[int, ...] = (1, 2, 3, 5, 10)
"""保有営業日数。**1日以外は安全装置#2（当日中に閉じる）を破る。**

**これは「延ばせ」という提案ではなく、人間が判断するための材料**
（`CLAUDE.md`「人間が判断すること」）。5家族すべてが「優位 < コスト」で
死んでいるので、コスト側の前提を動かしたらどうなるかを測っておく。

**持ち越すと失うもの**: デイトレ信用の手数料0・金利0・貸株料0
（`docs/02-margin-rules.md`）、当日決済による翌日ギャップリスクの回避、
日次ブレーカー（安全装置#4）が前提にしている「1日で損益が確定する」構造。
"""

OVERNIGHT_RATE_ANNUAL = 0.03
"""持ち越し1日あたりの金利（年率）。**未実測の仮定。**

`docs/02-margin-rules.md` にはデイトレ信用の0%しか記載がなく、
**制度信用・一般信用（長期）の実際のレートは確認していない**。
`tick.DEFAULT_SPREAD_TICKS` と同じ扱いで、**保守的な側**に置き、
感度も出す（`_report_rate_sensitivity`）。

**実際に保有期間を延ばすなら、証券会社の公表レートで置き換えること。**
"""

TRADING_DAYS_PER_YEAR = 245
"""年率を日率に直すのに使う営業日数。"""

PRIOR_MOVE_BUCKETS_PCT: tuple[float, ...] = (0.01, 0.02, 0.03, 0.04)
"""``|前日リターン|`` の下限バケット。

ギャップのスイープ（0.5/1.0/1.5/2.0%）より粗いのは、**日中の値動きは
ギャップより大きいのが普通**だから。実測の ATR% 中央値が3.33%
（`docs/00` 意思決定ログの確定値）なので、その前後を挟む刻みにした。
"""

MAX_EXTREME_ROWS = 10
"""異常値として名指しで出す件数。**握り潰さず実物を見せる**（規約「エラーを握り潰さない」）。"""


def max_calendar_days(horizon: int) -> int:
    """営業日 ``horizon`` 日ぶんの出口までに許す暦日数。

    **index で N本先を取ると、日足が欠けている銘柄では数か月先の終値を
    「N営業日保有」として扱ってしまう。** 保有期間は営業日で数えるものなので、
    暦日で見て離れすぎた出口は採用しない（採用しなかった件数は報告する）。

    土日で最大2日、年末年始・大型連休で最大10日ほど空くので
    ``horizon * 2 + 12`` を上限にする（10営業日なら32暦日）。
    """
    return horizon * 2 + 12


@dataclass(frozen=True)
class ForwardExit:
    """保有 ``horizon`` 営業日の出口。**どの日のいくらで閉じたかまで残す。**

    リターンだけ持つと、異常値が出たときに原因を追えない。
    """

    horizon: int
    exit_day: date
    exit_close: float
    return_pct: float
    """当日始値からその日の終値までのリターン。"""


@dataclass(frozen=True)
class DroppedExit:
    """暦日で離れすぎたため採用しなかった出口。**黙って捨てず数える。**"""

    symbol: str
    day: date
    horizon: int
    exit_day: date

    @property
    def elapsed_days(self) -> int:
        return (self.exit_day - self.day).days


@dataclass(frozen=True)
class ExtremeReturn:
    """将来リターンの絶対値が大きかった観測。**原因を推測で片付けないための実物。**"""

    symbol: str
    day: date
    exit: ForwardExit
    open_price: float


@dataclass(frozen=True)
class ReversalPair:
    """1銘柄・1営業日ぶんの「前日の値動き」と「当日の寄り付き後の値動き」。"""

    symbol: str
    day: date
    prior_move_pct: float
    """(前日終値 - 前々日終値) / 前々日終値。**前日大引けで確定する。**

    これが `gap_pct` との決定的な違い。ギャップは当日の始値が要るので
    寄成注文に間に合わないが、これは間に合う（意思決定ログ86）。
    """
    intraday_return_pct: float
    """(当日終値 - 当日始値) / 当日始値。**寄成で建てて大引けで手仕舞う。**"""
    open_price: float
    """当日始値。**コストは株価で決まる**（`autotrader.tick`）。"""
    topix100: bool = False
    """TOPIX100 構成銘柄か。**呼値が1桁違うのでコストに直接効く**（意思決定ログ61）。"""
    forward_exits: tuple[ForwardExit, ...] = ()
    """保有営業日数ごとの出口。

    1日は ``intraday_return_pct`` と同じ。**将来の終値を使うが、これは
    シグナルではなく成績**——建てた後に確定する情報なので先読みではない。

    含まないもの: 足りない日数（銘柄の終端付近）と、
    **暦日で離れすぎた出口**（`max_calendar_days`）。
    """

    @property
    def forward_returns(self) -> tuple[tuple[int, float], ...]:
        """``(保有営業日数, リターン)``。集計はこちらだけ見れば足りる。"""
        return tuple((e.horizon, e.return_pct) for e in self.forward_exits)


def _walk(
    daily_bars: dict[str, tuple[Bar, ...]],
) -> Iterator[tuple[str, tuple[Bar, ...], int]]:
    """判定に足りる日足が揃った ``(銘柄, 時系列, 当日の位置)`` を返す。

    **3日ぶんの日足が要る**（前々日終値・前日終値・当日の始値と終値）ので、
    各銘柄の最初の2日は除外する。価格が0以下の日も除外する（0除算対策）。

    `reversal_pairs` と `forward_exit_drops` が**同じ走査を二度書かない**ため
    に切り出してある（規約「同じことをする関数を二つ作らない」）。
    """
    for symbol in sorted(daily_bars):
        ordered = tuple(sorted(daily_bars[symbol], key=lambda b: b.timestamp))
        for i in range(2, len(ordered)):
            two_ago, prev, today = ordered[i - 2], ordered[i - 1], ordered[i]
            if two_ago.close <= 0 or prev.close <= 0 or today.open <= 0:
                continue
            yield symbol, ordered, i


def _forward_exits(
    symbol: str, ordered: tuple[Bar, ...], i: int
) -> tuple[tuple[ForwardExit, ...], tuple[DroppedExit, ...]]:
    """``i`` 日目に建てたときの、保有期間ごとの出口と、採用しなかった出口。

    **判定は1か所にまとめてある。** 採用条件を `reversal_pairs` と診断とで
    別々に書くと、片方だけ直して食い違う。

    Returns:
        ``(採用した出口, 暦日で離れすぎて捨てた出口)``。
    """
    today = ordered[i]
    day = today.timestamp.date()
    exits: list[ForwardExit] = []
    dropped: list[DroppedExit] = []
    for horizon in HORIZONS:
        exit_index = i + horizon - 1
        if exit_index >= len(ordered):
            continue
        exit_bar = ordered[exit_index]
        if exit_bar.close <= 0:
            continue
        exit_day = exit_bar.timestamp.date()
        # **index ではなく暦日で見る。** 日足が欠けている銘柄では
        # ordered[i + N - 1] が数か月先になりうる（意思決定ログ97）
        if (exit_day - day).days > max_calendar_days(horizon):
            dropped.append(
                DroppedExit(symbol=symbol, day=day, horizon=horizon, exit_day=exit_day)
            )
            continue
        exits.append(
            ForwardExit(
                horizon=horizon,
                exit_day=exit_day,
                exit_close=exit_bar.close,
                return_pct=(exit_bar.close - today.open) / today.open,
            )
        )
    return tuple(exits), tuple(dropped)


def reversal_pairs(
    daily_bars: dict[str, tuple[Bar, ...]],
    topix100_codes: frozenset[str] = frozenset(),
) -> tuple[ReversalPair, ...]:
    """銘柄ごとの日足から、前日の値動きと当日の寄り付き後の値動きを組にする。

    **ルックアヘッドは構造的に防いでいる**（規約7）——`prior_move_pct` は
    当日のバーを一切参照しない。当日から使うのは始値と終値だけで、
    それは建てた後・手仕舞う時点の情報。
    """
    pairs: list[ReversalPair] = []
    for symbol, ordered, i in _walk(daily_bars):
        two_ago, prev, today = ordered[i - 2], ordered[i - 1], ordered[i]
        exits, _ = _forward_exits(symbol, ordered, i)
        pairs.append(
            ReversalPair(
                symbol=symbol,
                day=today.timestamp.date(),
                prior_move_pct=(prev.close - two_ago.close) / two_ago.close,
                intraday_return_pct=(today.close - today.open) / today.open,
                open_price=today.open,
                topix100=symbol in topix100_codes,
                forward_exits=exits,
            )
        )
    return tuple(pairs)


def forward_exit_drops(
    daily_bars: dict[str, tuple[Bar, ...]],
) -> tuple[DroppedExit, ...]:
    """暦日で離れすぎたため採用しなかった出口を全部集める。

    **捨てた件数を報告するためだけの関数。** 判定そのものは `_forward_exits`
    に一本化してあるので、ここと本編で条件がずれることはない。
    """
    drops: list[DroppedExit] = []
    for symbol, ordered, i in _walk(daily_bars):
        drops.extend(_forward_exits(symbol, ordered, i)[1])
    return tuple(drops)


def extreme_forward_returns(
    pairs: tuple[ReversalPair, ...], limit: int = MAX_EXTREME_ROWS
) -> tuple[ExtremeReturn, ...]:
    """将来リターンの絶対値が大きい順に ``limit`` 件。

    **原因を推測で片付けないための実物。** 平均が桁違いになったとき、
    どの銘柄のどの日がそれを作っているかを名指しで出す。
    """
    rows = [
        ExtremeReturn(symbol=p.symbol, day=p.day, exit=e, open_price=p.open_price)
        for p in pairs
        for e in p.forward_exits
    ]
    rows.sort(key=lambda r: abs(r.exit.return_pct), reverse=True)
    return tuple(rows[:limit])


def reversal_score(pair: ReversalPair) -> float:
    """前日と逆に動いたら正（反転）、同じ方向に伸びたら負（継続）。

    前日が動いていない日は符号がないので0を返す。
    """
    if pair.prior_move_pct > 0:
        return -pair.intraday_return_pct
    if pair.prior_move_pct < 0:
        return pair.intraday_return_pct
    return 0.0


def round_trip_cost_bps(
    pair: ReversalPair, n_ticks: float = DEFAULT_SPREAD_TICKS
) -> float:
    """往復コスト（bps）。``autotrader.tick`` をそのまま使う。

    **約定コストのモデルを診断ごとに作り直さない**
    （`docs/00` 意思決定ログ33以降で呼値ベースに統一済み）。
    """
    spread = spread_yen(pair.open_price, n_ticks, topix100=pair.topix100)
    return float(spread) / pair.open_price * 10_000.0


@dataclass(frozen=True)
class BucketStats:
    """1バケット（``|前日リターン|`` がある下限以上の日）の集計。"""

    n: int
    gross_bps: float
    cost_bps: float
    clustered: ClusteredStats | None
    """日クラスタの統計。**判定に使うのはこちら**（意思決定ログ72）。"""

    @property
    def net_bps(self) -> float:
        """コストを引いた後。**これが正でなければ取引する意味がない。**"""
        return self.gross_bps - self.cost_bps

    @property
    def clustered_net_bps(self) -> float | None:
        """日クラスタの平均からコストを引いたもの。"""
        if self.clustered is None:
            return None
        return self.clustered.mean_bps - self.cost_bps


def bucket_stats(
    pairs: tuple[ReversalPair, ...],
    threshold: float,
    n_ticks: float = DEFAULT_SPREAD_TICKS,
) -> BucketStats | None:
    """``|prior_move_pct| >= threshold`` の日だけを集計する。

    Returns:
        該当が2件未満なら ``None``。
    """
    bucket = [p for p in pairs if abs(p.prior_move_pct) >= threshold]
    if len(bucket) < 2:
        return None
    return BucketStats(
        n=len(bucket),
        gross_bps=statistics.fmean(reversal_score(p) * 10_000.0 for p in bucket),
        cost_bps=statistics.fmean(round_trip_cost_bps(p, n_ticks) for p in bucket),
        clustered=clustered_stats((p.day, reversal_score(p) * 10_000.0) for p in bucket),
    )


def reversal_score_at(pair: ReversalPair, horizon: int) -> float | None:
    """``horizon`` 営業日持ったときの反転スコア。該当が無ければ ``None``。"""
    if pair.prior_move_pct == 0:
        return None
    forward = dict(pair.forward_returns).get(horizon)
    if forward is None:
        return None
    return -forward if pair.prior_move_pct > 0 else forward


def holding_cost_bps(
    pair: ReversalPair,
    horizon: int,
    *,
    n_ticks: float = DEFAULT_SPREAD_TICKS,
    annual_rate: float = OVERNIGHT_RATE_ANNUAL,
) -> float:
    """``horizon`` 営業日持ったときの総コスト（bps）。

    往復スプレッド（1回ぶん・保有期間によらない）に、**持ち越した日数ぶんの
    金利**を足す。当日決済（``horizon == 1``）なら金利は0——
    デイトレ信用は手数料0・金利0・貸株料0（`docs/02-margin-rules.md`）。

    **``annual_rate`` は未実測の仮定**（`OVERNIGHT_RATE_ANNUAL`）。
    """
    spread = round_trip_cost_bps(pair, n_ticks)
    nights = max(0, horizon - 1)
    return spread + annual_rate / TRADING_DAYS_PER_YEAR * nights * 10_000.0


def _report_horizons(pairs: tuple[ReversalPair, ...]) -> None:
    """保有期間を延ばすと優位とコストの比がどう変わるか。

    **これは「延ばせ」という提案ではない。** 5家族すべてが「優位 < コスト」で
    死んだので、**コスト側の前提を動かしたらどうなるか**を人間が判断できる
    材料として測る（`CLAUDE.md`「人間が判断すること」）。

    **重要な算術**: ``horizon`` 日持つと回転は 1/N になるので、同じ日次
    リターンを出すには1トレードあたり **N倍の net** が要る。一方コストは
    往復1回ぶん（＋金利×日数）しか増えない。つまり
    **「延ばせばコストの重みが下がる」は"損を薄める"意味では正しいが、
    "目標に届く"意味では逆**——目標に届くには gross が N倍より速く伸びる
    必要がある（`diagnostics.required_gross_bps` の docstring 参照）。
    """
    hr("4. 保有期間を延ばすとどうなるか")
    print("  **1日以外は安全装置#2（当日中に閉じる）を破る。**")
    print("  デイトレ信用の手数料0・金利0・貸株料0、翌日ギャップリスクの回避、")
    print("  日次ブレーカーが前提にする「1日で損益が確定する」構造をすべて失う。")
    print(f"  金利は年{OVERNIGHT_RATE_ANNUAL:.1%}と仮定（**未実測**）。")
    print()

    threshold = PRIOR_MOVE_BUCKETS_PCT[1]
    bucket = tuple(p for p in pairs if abs(p.prior_move_pct) >= threshold)
    if len(bucket) < 2:
        print("  該当が足りない")
        return
    print(f"  対象: |前日リターン| >= {threshold:.1%} の {len(bucket)}件")
    print()
    print("  **点推定は全観測から。t値だけ重なりを補正する。**")
    print("  3日保有を毎日建てるとD日とD+1日の玉が同じ日を共有し、独立な観測が")
    print("  日数/N しかなくなる（重なったまま t値を出すと約√N倍 過大・意思決定ログ90）。")
    print("  **間引くのは t値のためだけ**——点推定まで間引くと(N-1)/N を捨てる")
    print("  うえ、どの日から数え始めたかに依存する（意思決定ログ91）。")
    print()
    print(
        f"  {'保有':>5} {'gross':>9} {'コスト':>9} {'net':>9} "
        f"{'net/日':>8} {'年利':>8} {'t値':>6} {'必要':>8} {'判定':>4}"
    )
    print("  " + "-" * 72)
    rows: list[tuple[int, float, float]] = []
    for horizon in HORIZONS:
        # **点推定は全観測。** 間引かない
        samples = tuple(
            (p.day, v * 10_000.0)
            for p in bucket
            if (v := reversal_score_at(p, horizon)) is not None
        )
        stats = clustered_stats(samples)
        if stats is None:
            continue
        cost = statistics.fmean(holding_cost_bps(p, horizon) for p in bucket)
        need = required_gross_bps(ANNUAL_TARGET, cost_bps=cost, horizon_days=horizon)
        net = stats.mean_bps - cost
        per_day = net / horizon
        annual = (float((1.0 + per_day / 10_000.0) ** 240) - 1.0) * 100.0

        # **t値だけ重なりを補正する。** 位相ごとに出して平均する
        # （1つの位相だけ見ると、その位相の当たり外れが t値に混ざる）
        t_values = [
            phase_stats.t_stat
            for phase in range(horizon)
            if (days := non_overlapping_days((p.day for p in bucket), horizon, phase))
            and (
                phase_stats := clustered_stats(
                    (d, v) for d, v in samples if d in days
                )
            )
            is not None
        ]
        t_stat = statistics.fmean(t_values) if t_values else 0.0
        rows.append((horizon, net, per_day))
        print(
            f"  {horizon:>4}日 {stats.mean_bps:>+8.2f}b "
            f"{cost:>8.2f}b {net:>+8.2f}b {per_day:>+7.2f}b {annual:>+7.1f}% "
            f"{t_stat:>5.1f} {need:>7.1f}b {'○' if stats.mean_bps >= need else '×':>4}"
        )
    print()
    print("  **保有期間をまたいで比べられるのは net/日 と年利だけ。**")
    print(f"  目標は年利{ANNUAL_TARGET:.0%}〜35%（意思決定ログ73）。")
    print("  **必要gross は保有期間にほぼ比例して上がる**（回転が 1/N になるため）。")
    print("  gross がそれより速く伸びなければ、延ばしても目標には近づかない。")

    _report_phase_stability(bucket)


def _report_phase_stability(bucket: tuple[ReversalPair, ...]) -> None:
    """位相を変えると推定がどれだけ動くか。**動くならその推定は信用できない。**

    実測では5日保有の gross が、先頭から数え始めるか1日ずらすかだけで
    +31.5 → +10.0bps と3倍動いた（意思決定ログ91）。
    **この振れ幅そのものが不確かさ**であり、統計量を足すより直接的に効く。
    """
    print()
    print("  【位相を変えるとどれだけ動くか】**動くなら推定を信用しない**")
    print("  N日おきに間引くとき、何日目から数え始めるかを振る。")
    print()
    print(f"  {'保有':>5} {'gross(全体)':>12} {'位相の最小':>11} {'位相の最大':>11} {'振れ幅':>9}")
    print("  " + "-" * 54)
    for horizon in HORIZONS:
        samples = tuple(
            (p.day, v * 10_000.0)
            for p in bucket
            if (v := reversal_score_at(p, horizon)) is not None
        )
        overall = clustered_stats(samples)
        if overall is None:
            continue
        if horizon == 1:
            print(f"  {horizon:>4}日 {overall.mean_bps:>+11.2f}b {'—（重なりなし）':>28}")
            continue
        means: list[float] = []
        for phase in range(horizon):
            days = non_overlapping_days((p.day for p in bucket), horizon, phase)
            phase_stats = clustered_stats((d, v) for d, v in samples if d in days)
            if phase_stats is not None:
                means.append(phase_stats.mean_bps)
        if not means:
            continue
        print(
            f"  {horizon:>4}日 {overall.mean_bps:>+11.2f}b {min(means):>+10.2f}b "
            f"{max(means):>+10.2f}b {max(means) - min(means):>+8.2f}b"
        )
    print()
    print("  **振れ幅が gross そのものと同じ桁なら、その保有期間の推定は無意味。**")


def _report_rate_sensitivity(pairs: tuple[ReversalPair, ...]) -> None:
    """金利の想定を変えたときに結論が変わるか。

    **金利は未実測**（`OVERNIGHT_RATE_ANNUAL`）なので、
    `DEFAULT_SPREAD_TICKS` と同じ扱いで感度を出す（意思決定ログ60）。
    **変わらないなら、レートを調べに行く必要はない。**
    """
    threshold = PRIOR_MOVE_BUCKETS_PCT[1]
    bucket = tuple(p for p in pairs if abs(p.prior_move_pct) >= threshold)
    if len(bucket) < 2:
        return
    rates = (0.0, 0.02, 0.03, 0.05)
    print()
    print("  【金利の想定に対する感度】**gross は金利に依存しない**ので確実")
    print()
    header = "  ".join(f"年{r:.0%}" for r in rates)
    print(f"  {'保有':>5} {'gross':>9}   net: {header}")
    print("  " + "-" * 56)
    for horizon in HORIZONS:
        samples = tuple(
            (p.day, v * 10_000.0)
            for p in bucket
            if (v := reversal_score_at(p, horizon)) is not None
        )
        stats = clustered_stats(samples)
        if stats is None:
            continue
        cells: list[str] = []
        for rate in rates:
            cost = statistics.fmean(
                holding_cost_bps(p, horizon, annual_rate=rate) for p in bucket
            )
            cells.append(f"{stats.mean_bps - cost:>+6.1f}b")
        nets = "  ".join(cells)
        print(f"  {horizon:>4}日 {stats.mean_bps:>+8.2f}b        {nets}")
    print()
    print("  **どの想定でも判定が変わらないなら、金利を調べに行く必要はない。**")


def _report_data_sanity(
    daily_bars: dict[str, tuple[Bar, ...]],
) -> dict[str, tuple[Bar, ...]]:
    """日足そのものが壊れていないかを、集計より先に確かめる。

    **実測でセクション4だけが桁違いになった**（2日保有 gross +6,502bps、
    年利 +1.9e31%）。セクション1〜3は同じバーの中で完結する値しか使わない
    ので無傷だった——**日をまたぐ値だけが壊れる**という形は、
    価格水準の継ぎ目を強く示唆する（意思決定ログ97）。

    Returns:
        継ぎ目のあった銘柄を除いた日足。**除いた銘柄は数えて名指しで出す**
        （規約「エラーを握り潰さない」）。
    """
    hr("0. データの健全性（集計より先に確かめる）")
    print("  **実測でセクション4だけが桁違いになった**（2日保有 gross +6,502bps・")
    print("  年利 +1.9e31%）。セクション1〜3は同じバーの中で完結する値しか")
    print("  使わないので無傷だった——**日をまたぐ値だけが壊れる**という形は、")
    print("  価格水準の継ぎ目を強く示唆する（意思決定ログ97）。")
    print()
    return drop_discontinuous_symbols(daily_bars)


def _report_forward_integrity(
    daily_bars: dict[str, tuple[Bar, ...]], pairs: tuple[ReversalPair, ...]
) -> None:
    """出口の取り方が保有期間として妥当かを確かめる。

    **以前は index で N本先を取っていた**ので、日足が欠けている銘柄では
    数か月先の終値が「N営業日保有」として混ざりえた（意思決定ログ97）。
    暦日で弾くように直したので、**弾いた件数と、残った中での異常値**を出す。
    """
    print()
    drops = forward_exit_drops(daily_bars)
    if drops:
        worst = max(drops, key=lambda d: d.elapsed_days)
        by_horizon: dict[int, int] = {}
        for d in drops:
            by_horizon[d.horizon] = by_horizon.get(d.horizon, 0) + 1
        detail = " / ".join(f"{h}日:{n}件" for h, n in sorted(by_horizon.items()))
        print(f"  暦日で離れすぎて捨てた出口: {len(drops)}件（{detail}）")
        print(
            f"    最悪 {worst.symbol} {worst.day} の{worst.horizon}日保有が"
            f"{worst.elapsed_days}暦日先（上限{max_calendar_days(worst.horizon)}日）"
        )
    else:
        print("  暦日で離れすぎて捨てた出口: なし")

    extremes = extreme_forward_returns(pairs)
    if not extremes:
        return
    print()
    print("  【将来リターンの絶対値が大きい順】**丸めない・切らない**")
    print(
        f"  {'銘柄':<8} {'建てた日':<12} {'保有':>4} {'始値':>9} "
        f"{'出口日':<12} {'出口終値':>10} {'リターン':>10}"
    )
    print("  " + "-" * 70)
    for r in extremes:
        print(
            f"  {r.symbol:<8} {r.day!s:<12} {r.exit.horizon:>3}日 "
            f"{r.open_price:>9.1f} {r.exit.exit_day!s:<12} "
            f"{r.exit.exit_close:>10.1f} {r.exit.return_pct * 100:>+9.1f}%"
        )
    print()
    print("  **ここに桁外れの行が残っていたら、集計を読む前にその銘柄を見る。**")


def load_symbols(*, topix100_only: bool, cheap: bool = False) -> tuple[Symbol, ...]:
    """銘柄一覧。`scripts/measure_gap_fade.py` と同じ読み込み方をする。

    **スクリプト間で import せず、それぞれ自己完結させる**
    （スクリプトファイルは pythonpath に乗らないため）。

    Args:
        topix100_only: TOPIX100 構成銘柄だけに絞る。
        cheap: `scripts/measure_cost_landscape.py` が**成績を一切見ずに**
            切り出したユニバースを使う（意思決定ログ95）。
            コスト10bps以下・流動性3億円以上・資金120万円で建てられる287銘柄で、
            **中型・小型の高株価帯**が中心。Layer 1（≤1,250円）と TOPIX100（大型）
            という両端だけを試して飛ばしていた領域。
    """
    if cheap:
        path = DATA_ROOT / "universe_cheap.json"
        if not path.is_file():
            raise SystemExit(
                f"{path} がない。"
                "先に python scripts/measure_cost_landscape.py --refresh を実行する"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        return tuple(_to_symbol(r) for r in payload["symbols"])

    if topix100_only:
        # **検証期間の開始時点の一覧を使う。** 現在の一覧で過去を測るのは
        # サバイバーシップバイアス（`docs/03-universe.md` §4.2・意思決定ログ66）
        for name in ("master_scale_historical.json", "master_scale.json"):
            path = DATA_ROOT / name
            if path.is_file():
                payload = json.loads(path.read_text(encoding="utf-8"))
                symbols = tuple(_to_symbol(r) for r in payload["symbols"])
                return tuple(s for s in symbols if s.is_topix100)
        raise SystemExit(
            "TOPIX100 の一覧が無い。先に python scripts/measure_topix100.py --refresh"
        )

    path = DATA_ROOT / "universe.json"
    if not path.is_file():
        raise SystemExit(f"{path} がない。先に python scripts/fetch_bars.py を実行する")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(_to_symbol(r) for r in payload["symbols"])


def _to_symbol(row: dict[str, str | None]) -> Symbol:
    code = row["code"]
    name = row["name"]
    assert code is not None and name is not None
    return Symbol(
        code=code,
        name=name,
        market=row.get("market"),
        margin_type=row.get("margin_type"),
        sector=row.get("sector"),
        scale_category=row.get("scale_category"),
    )


def hr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def _format_bucket(stats: BucketStats | None) -> str:
    if stats is None:
        return f"{'—':>8} {'—':>9} {'—':>9} {'—':>9} {'—':>6} {'—':>7}"
    cluster_net = stats.clustered_net_bps
    days = stats.clustered.days if stats.clustered else 0
    t_stat = stats.clustered.t_stat if stats.clustered else 0.0
    return (
        f"{stats.n:>8} {stats.gross_bps:>+8.2f}b {stats.cost_bps:>8.2f}b "
        f"{stats.net_bps:>+8.2f}b {days:>6} {t_stat:>6.1f}"
        + (f" {cluster_net:>+8.2f}b" if cluster_net is not None else f" {'—':>9}")
    )


def _report_buckets(pairs: tuple[ReversalPair, ...]) -> None:
    hr("1. 前日の値動きの大きさで分ける")
    baseline = bucket_stats(pairs, 0.0)
    if baseline is not None:
        print(
            f"  全日ベースライン: 件数 {baseline.n:>6} / "
            f"gross {baseline.gross_bps:>+7.2f}bps"
        )
    print()
    print(
        f"  {'|前日|下限':<10} {'件数':>8} {'gross':>9} {'コスト':>9} "
        f"{'net':>9} {'日数':>6} {'t値(日)':>7} {'net(日)':>9}"
    )
    print("  " + "-" * 74)
    for threshold in PRIOR_MOVE_BUCKETS_PCT:
        print(f"  {threshold:>8.1%}  {_format_bucket(bucket_stats(pairs, threshold))}")
    print()
    print("  **判定に使うのは t値(日) と net(日)。** 同じ日の銘柄は市場要因で")
    print("  強く相関するので、件数ベースの t値は過大に出る（意思決定ログ72）。")

    if baseline is not None:
        print()
        print(
            f"  【合格ライン】年利{ANNUAL_TARGET:.0%}に要る gross"
            f"（コスト{baseline.cost_bps:.1f}bps 前提）"
        )
        for deployment, note in ((1.0, "常に満玉"), (0.62, "実測の建玉率")):
            need = required_gross_bps(
                ANNUAL_TARGET, cost_bps=baseline.cost_bps, deployment=deployment
            )
            print(f"    建玉率 {deployment:>4.0%}（{note}）: **{need:>5.1f}bps**")
        print("  **これは基準ではなく算術。** 目標とコストと建玉率から一意に決まる。")


def _report_period_split(pairs: tuple[ReversalPair, ...]) -> None:
    hr("2. 前半・後半で符号が保たれるか")
    first_days, second_days = split_days(p.day for p in pairs)
    if not second_days:
        print("  営業日が足りず判定不能")
        return
    first = tuple(p for p in pairs if p.day in first_days)
    second = tuple(p for p in pairs if p.day in second_days)
    print(f"  前半 {min(first_days)}〜{max(first_days)}（{len(first_days)}営業日）")
    print(f"  後半 {min(second_days)}〜{max(second_days)}（{len(second_days)}営業日）")
    print()
    print(
        f"  {'|前日|下限':<10} {'前半 net':>10} {'t値(日)':>8}   "
        f"{'後半 net':>10} {'t値(日)':>8}"
    )
    print("  " + "-" * 56)
    for threshold in PRIOR_MOVE_BUCKETS_PCT:
        cells: list[str] = []
        for subset in (first, second):
            stats = bucket_stats(subset, threshold)
            if stats is None or stats.clustered is None:
                cells.append(f"{'—':>10} {'—':>8}")
                continue
            cells.append(
                f"{stats.clustered_net_bps:>+9.2f}b {stats.clustered.t_stat:>7.1f}"
            )
        print(f"  {threshold:>8.1%}  {cells[0]}   {cells[1]}")
    print()
    print("  **片方の期間だけが正なら棄却。** 期間分割は「通れば合格」ではなく")
    print("  **落とすための試験**（意思決定ログ46）。")


def _report_verdict(pairs: tuple[ReversalPair, ...]) -> None:
    """事前登録した3条件を機械的に判定する。

    **結果を見てから基準を動かさないために、コードに埋め込む**
    （意思決定ログ87）。95.5%という僅差を棄却した規律と同じ扱い。
    """
    hr("3. 事前登録した判定")
    all_stats = [bucket_stats(pairs, t) for t in PRIOR_MOVE_BUCKETS_PCT]
    nets = [s.clustered_net_bps for s in all_stats if s and s.clustered_net_bps is not None]

    monotone = len(nets) == len(PRIOR_MOVE_BUCKETS_PCT) and all(
        b >= a for a, b in zip(nets, nets[1:], strict=False)
    )
    strong = any(
        s.clustered is not None and s.clustered.t_stat >= 2.0 and (s.clustered_net_bps or 0) > 0
        for s in all_stats
        if s
    )

    first_days, second_days = split_days(p.day for p in pairs)
    halves_positive = False
    if second_days:
        first = tuple(p for p in pairs if p.day in first_days)
        second = tuple(p for p in pairs if p.day in second_days)
        halves_positive = all(
            (s := bucket_stats(subset, t)) is not None
            and (s.clustered_net_bps or -1.0) > 0
            for subset in (first, second)
            for t in PRIOR_MOVE_BUCKETS_PCT[-1:]
        )

    for label, passed in (
        ("① net(日) がバケットで単調に改善する", monotone),
        ("② どこかのバケットで t値(日) >= 2 かつ net(日) > 0", strong),
        ("③ 前半・後半とも最大バケットで net(日) > 0", halves_positive),
    ):
        print(f"  {'○' if passed else '×'} {label}")

    print()
    if monotone and strong and halves_positive:
        print("  → **3つとも通過。** 5分足での検証に進む価値がある")
        print("    （ただし5分足は42営業日しかない。80営業日を待つ）")
    else:
        print("  → **棄却。** 事前登録どおり、どれか1つでも外せば採用しない")
        print("    基準を後から緩めない（意思決定ログ46・75と同じ規律）")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--topix100",
        action="store_true",
        help="TOPIX100 構成銘柄だけで測る（呼値0.1〜0.5円でコストが1桁下がる）",
    )
    parser.add_argument(
        "--cheap",
        action="store_true",
        help=(
            "コストで切り出したユニバースで測る（意思決定ログ95）。"
            "**成績を一切見ずに**コスト10bps以下・流動性3億円以上で選んだ287銘柄。"
            "中型・小型の高株価帯が中心で、これまで飛ばしていた領域"
        ),
    )
    args = parser.parse_args()
    if args.topix100 and args.cheap:
        raise SystemExit("--topix100 と --cheap は同時に指定できない")

    print("前日の値動きの反転を測る（寄成で建てられる初めての候補）")
    print(banner())

    symbols = load_symbols(topix100_only=args.topix100, cheap=args.cheap)
    if args.topix100:
        print(f"  **TOPIX100 のみ**（呼値0.1〜0.5円）: {len(symbols)}銘柄")
        print("  構成銘柄は検証期間の開始時点のもの（docs/03 §4.2）")
    elif args.cheap:
        print(f"  **コストで切り出したユニバース**: {len(symbols)}銘柄")
        print("  **成績を一切見ずに**コスト10bps以下・流動性3億円以上で選んだ")
        print("  （意思決定ログ95）。中型・小型の高株価帯が中心。")
        print()
        print("  **判定基準は TOPIX100 のときと同じものをそのまま使う。**")
        print("  ユニバースを変えただけで基準を緩めない（意思決定ログ46・75）。")
    topix100_codes = frozenset(s.code for s in symbols if s.is_topix100)

    store = BarStore(DATA_ROOT)
    daily = {s.code: store.read(s.code, "1d") for s in symbols}
    daily = {c: b for c, b in daily.items() if b}
    print(f"  日足あり: {len(daily)}銘柄")

    daily = _report_data_sanity(daily)
    if not daily:
        print("  健全な日足が残らなかった。日足を取り直す")
        return 1

    pairs = reversal_pairs(daily, topix100_codes)
    if not pairs:
        print("  データがない。先に python scripts/fetch_bars.py を実行する")
        return 1
    _report_forward_integrity(daily, pairs)

    days = {p.day for p in pairs}
    print()
    print(f"  対象: {len(pairs)}件 / {len(days)}営業日（{min(days)}〜{max(days)}）")

    _report_buckets(pairs)
    _report_period_split(pairs)
    _report_verdict(pairs)
    _report_horizons(pairs)
    _report_rate_sensitivity(pairs)

    print()
    print("**シグナルは前日大引けで確定するので、寄成注文で板寄せの価格を取れる。**")
    print("ギャップ・フェードが死んだ理由（始値が要るので循環する・意思決定ログ86）を")
    print("回避した初めての候補。**通ってもここでは採用しない**——5分足での")
    print("検証が本番であり、日足はその手前の足切り。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
