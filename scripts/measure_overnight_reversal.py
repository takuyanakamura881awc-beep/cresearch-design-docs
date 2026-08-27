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
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from autotrader.data.store import BarStore
from autotrader.diagnostics import (
    ClusteredStats,
    clustered_stats,
    required_gross_bps,
    split_days,
)
from autotrader.provenance import banner
from autotrader.tick import DEFAULT_SPREAD_TICKS, spread_yen
from autotrader.types import Bar, Symbol

DATA_ROOT = Path("data")

ANNUAL_TARGET = 0.25
"""目標年利（意思決定ログ73）。**必要な gross を逆算するのに使う。**"""

PRIOR_MOVE_BUCKETS_PCT: tuple[float, ...] = (0.01, 0.02, 0.03, 0.04)
"""``|前日リターン|`` の下限バケット。

ギャップのスイープ（0.5/1.0/1.5/2.0%）より粗いのは、**日中の値動きは
ギャップより大きいのが普通**だから。実測の ATR% 中央値が3.33%
（`docs/00` 意思決定ログの確定値）なので、その前後を挟む刻みにした。
"""


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


def reversal_pairs(
    daily_bars: dict[str, tuple[Bar, ...]],
    topix100_codes: frozenset[str] = frozenset(),
) -> tuple[ReversalPair, ...]:
    """銘柄ごとの日足から、前日の値動きと当日の寄り付き後の値動きを組にする。

    **3日ぶんの日足が要る**（前々日終値・前日終値・当日の始値と終値）ので、
    各銘柄の最初の2日は除外する。価格が0以下の日も除外する（0除算対策）。

    **ルックアヘッドは構造的に防いでいる**（規約7）——`prior_move_pct` は
    当日のバーを一切参照しない。当日から使うのは始値と終値だけで、
    それは建てた後・手仕舞う時点の情報。
    """
    pairs: list[ReversalPair] = []
    for symbol, series in daily_bars.items():
        ordered = sorted(series, key=lambda b: b.timestamp)
        for two_ago, prev, today in zip(ordered, ordered[1:], ordered[2:], strict=False):
            if two_ago.close <= 0 or prev.close <= 0 or today.open <= 0:
                continue
            pairs.append(
                ReversalPair(
                    symbol=symbol,
                    day=today.timestamp.date(),
                    prior_move_pct=(prev.close - two_ago.close) / two_ago.close,
                    intraday_return_pct=(today.close - today.open) / today.open,
                    open_price=today.open,
                    topix100=symbol in topix100_codes,
                )
            )
    return tuple(pairs)


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


def load_symbols(*, topix100_only: bool) -> tuple[Symbol, ...]:
    """銘柄一覧。`scripts/measure_gap_fade.py` と同じ読み込み方をする。

    **スクリプト間で import せず、それぞれ自己完結させる**
    （スクリプトファイルは pythonpath に乗らないため）。
    """
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
    args = parser.parse_args()

    print("前日の値動きの反転を測る（寄成で建てられる初めての候補）")
    print(banner())

    symbols = load_symbols(topix100_only=args.topix100)
    if args.topix100:
        print(f"  **TOPIX100 のみ**（呼値0.1〜0.5円）: {len(symbols)}銘柄")
        print("  構成銘柄は検証期間の開始時点のもの（docs/03 §4.2）")
    topix100_codes = frozenset(s.code for s in symbols if s.is_topix100)

    store = BarStore(DATA_ROOT)
    daily = {s.code: store.read(s.code, "1d") for s in symbols}
    daily = {c: b for c, b in daily.items() if b}
    print(f"  日足あり: {len(daily)}銘柄")

    pairs = reversal_pairs(daily, topix100_codes)
    if not pairs:
        print("  データがない。先に python scripts/fetch_bars.py を実行する")
        return 1
    days = {p.day for p in pairs}
    print(f"  対象: {len(pairs)}件 / {len(days)}営業日（{min(days)}〜{max(days)}）")

    _report_buckets(pairs)
    _report_period_split(pairs)
    _report_verdict(pairs)

    print()
    print("**シグナルは前日大引けで確定するので、寄成注文で板寄せの価格を取れる。**")
    print("ギャップ・フェードが死んだ理由（始値が要るので循環する・意思決定ログ86）を")
    print("回避した初めての候補。**通ってもここでは採用しない**——5分足での")
    print("検証が本番であり、日足はその手前の足切り。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
