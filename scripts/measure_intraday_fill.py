#!/usr/bin/env python3
"""日足で測ってきた前提が、5分足の現実とどれだけずれるかを測る。

    python scripts/measure_intraday_fill.py

【なぜ今これをやるのか】

**これまでの実験（竹・VWAP乖離・ギャップフェード）のうち、日足で測った
ものはすべて「始値で建てて大引けで手仕舞う」という仮定に乗っている。**
だが実際の運用は2箇所でずれる:

===========  ==========================  ==============================
             日足での仮定                実際の運用
===========  ==========================  ==============================
エントリー   日足の始値                  寄り付き（最初の5分足）
クローズ     日足の終値（15:00）         **14:50**（安全装置#2）
===========  ==========================  ==============================

**14:50 は動かせない。** デイトレ信用の建玉を閉じ損ねると翌営業日に
強制決済され1注文2,200円（目標リターンの約5日分）。`MarketScheduler.CLOSE_ALL_TIME`
はそのための余裕であって、成績のために遅らせてよい値ではない。

つまり**大引け前10分の値動きは、そもそも取りに行けない。** もし平均回帰が
引け際に完成するなら（引けの板寄せは実際そういう場になりやすい）、
日足で測った優位はまるごと幻ということになる。

【80営業日を待たなくてよい理由】

待つ必要があるのは**戦略の検証**（ウォークフォワード）であって、
**仮定のズレの測定**ではない。ズレは平均の差であり、42営業日 × 200銘柄超で
十分に測れる。むしろ**戦略を実装する前に測るべき**——ズレが大きいなら、
実装しても意味がないことが先に分かる。

【測ること】

1. **日足の始値 == 最初の5分足の始値 か**（データの整合性）。
   ずれていれば、これまでの `gap_pct` の計算そのものが疑わしくなる
2. **始値→14:50 と 始値→大引け の差**（安全装置#2による切り落とし）
3. **ギャップ該当日に限った同じ差**——フェードの優位が引け際に
   集中しているかどうか

**日クラスタで見る**（意思決定ログ72）。同じ日の銘柄は市場要因で相関する。
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path

from autotrader.data.store import BarStore
from autotrader.engine.scheduler import MarketScheduler
from autotrader.provenance import banner
from autotrader.types import Bar, Symbol

DATA_ROOT = Path("data")

CUTOFF = MarketScheduler.CLOSE_ALL_TIME
"""クローズ時刻。**`scheduler` の値をそのまま使う**——診断ごとに定義し直さない。"""

BAR_MINUTES = 5
"""5分足の1本の長さ。"""

LAST_BAR_START = time(
    (CUTOFF.hour * 60 + CUTOFF.minute - BAR_MINUTES) // 60,
    (CUTOFF.hour * 60 + CUTOFF.minute - BAR_MINUTES) % 60,
)
"""手仕舞いに使える最後のバーの**開始**時刻。

**yfinance の5分足は区間の開始時刻でラベルされる。** つまり `14:50` の
バーは 14:50〜14:55 を表し、その終値は **14:55 の価格**。
`timestamp <= 14:50` で拾うと、**14:55 まで持ち越した価格で手仕舞ったこと**
になり、5分ぶん先読みする。

区間が 14:50 までに終わるバー（開始 ≤ 14:45）だけを使う。
規約「検証できないものは保守的な側に倒す」——ラベルが区間終了だった場合、
この選び方は1本ぶん損をするだけで、優位を水増しはしない。
"""

GAP_THRESHOLD_PCT = 0.010
"""ギャップ該当日を絞る下限。

**日クラスタ t値が最も高かった 1.0% を使う**（意思決定ログ75）。
net bps が最大だった 2.0% は t=0.6 で、結果を見てから選んだ基準だった。
"""


@dataclass(frozen=True)
class IntradayPath:
    """1銘柄・1営業日ぶんの、日足と5分足の突き合わせ。"""

    symbol: str
    day: date
    daily_open: float
    daily_close: float
    first_bar_open: float
    """その日の最初の5分足の始値。**日足の始値と一致するはず。**"""
    cutoff_close: float
    """区間が ``CUTOFF`` までに終わる最後の5分足の終値。**実際に手仕舞える価格。**

    バーのラベルは区間の**開始**時刻なので、14:50 で手仕舞うなら
    使えるのは 14:45 開始のバー（`LAST_BAR_START`）まで。
    """
    first_bar_at: time
    """その日の最初の5分足の時刻。**始値がずれる原因の切り分けに要る。**"""
    last_bar_at: time
    """その日の最後の5分足の時刻（カットオフを問わない）。"""
    bar_count: int
    """その日の5分足の本数。**欠損の量を測る一番直接的な指標。**

    東証は 9:00〜11:30 と 12:30〜15:30（2024年11月から15:30引け）なので、
    完全なら 30 + 36 = 66本になるはず。
    """
    prev_close: float | None
    """前日の日足終値。ギャップの計算に要る。銘柄の初日は ``None``。"""
    topix100: bool = False
    """TOPIX100 構成銘柄か。**net 正が出たのはこの銘柄群だけ**（意思決定ログ67）。

    Layer 1（小型〜中型）と混ぜて測ると比較にならない。
    """

    @property
    def open_mismatch_bps(self) -> float:
        """最初の5分足の始値が、日足の始値からどれだけずれているか。"""
        return (self.first_bar_open - self.daily_open) / self.daily_open * 10_000.0

    @property
    def return_to_cutoff_bps(self) -> float:
        """始値 → 14:50。**実際に取れるぶん。**"""
        return (self.cutoff_close - self.daily_open) / self.daily_open * 10_000.0

    @property
    def return_to_close_bps(self) -> float:
        """始値 → 大引け。**日足での仮定。**"""
        return (self.daily_close - self.daily_open) / self.daily_open * 10_000.0

    @property
    def tail_bps(self) -> float:
        """14:50 → 大引け。**取りに行けない部分。**"""
        return self.return_to_close_bps - self.return_to_cutoff_bps

    @property
    def gap_pct(self) -> float | None:
        """(当日始値 - 前日終値) / 前日終値。前日終値が無ければ ``None``。"""
        if self.prev_close is None or self.prev_close <= 0:
            return None
        return (self.daily_open - self.prev_close) / self.prev_close

    def fade_bps(
        self, *, to_cutoff: bool, entry_at_first_bar: bool = False
    ) -> float | None:
        """ギャップと逆に動いたら正。エントリーと手仕舞いの取り方を切り替える。

        `scripts/measure_gap_fade.py` の `fade_score` と同じ符号の規約。
        **式を写しているのではなく、同じ規約を別の測定単位（bps・14:50 まで）で
        使っている**——あちらは日足だけ、こちらは5分足を突き合わせる。

        Args:
            to_cutoff: ``True`` なら 14:50 まで、``False`` なら大引けまで。
            entry_at_first_bar: ``True`` なら**実際に建てられる最初のバー**の
                始値で建てる。**Yahoo の日本株5分足は寄り付きの1本を
                構造的に落とす**（意思決定ログ83）ので、日足の始値で建てる
                という仮定は64%の日で実現できない。

        **``gap_pct`` は常に日足の始値で計算する。** シグナルの定義は
        「前日終値 vs 当日始値」であり、そちらは実測で正しいと確認済み
        （意思決定ログ82）。変えるのは**建てられる価格**だけ。
        """
        gap = self.gap_pct
        if gap is None or gap == 0:
            return None
        entry = self.first_bar_open if entry_at_first_bar else self.daily_open
        exit_price = self.cutoff_close if to_cutoff else self.daily_close
        move = (exit_price - entry) / entry * 10_000.0
        return -move if gap > 0 else move


def intraday_paths(
    daily_bars: dict[str, tuple[Bar, ...]],
    intraday_bars: dict[str, tuple[Bar, ...]],
    last_bar_start: time = LAST_BAR_START,
    topix100_codes: frozenset[str] = frozenset(),
) -> tuple[IntradayPath, ...]:
    """日足と5分足を、銘柄 × 営業日で突き合わせる。

    **両方そろっている日だけを返す。** 5分足がまだ58日ぶんしかないので、
    日足（2年）の大半は対象外になる——それが正しい挙動で、
    片方しか無い日を無理に埋めない。

    ``last_bar_start`` 以前に始まる5分足が1本も無い日は除く
    （手仕舞えないので測れない）。価格が0以下の日も除く（0除算対策）。

    **``last_bar_start`` は「開始時刻」であって手仕舞い時刻ではない**
    （`LAST_BAR_START` の docstring 参照）。
    """
    paths: list[IntradayPath] = []
    for symbol, daily in daily_bars.items():
        intraday = intraday_bars.get(symbol)
        if not intraday:
            continue

        by_day: dict[date, list[Bar]] = defaultdict(list)
        for bar in intraday:
            by_day[bar.timestamp.date()].append(bar)

        ordered = sorted(daily, key=lambda b: b.timestamp)
        prev_close: float | None = None
        for bar in ordered:
            day = bar.timestamp.date()
            session = by_day.get(day)
            if session and bar.open > 0 and bar.close > 0:
                session.sort(key=lambda b: b.timestamp)
                closable = [b for b in session if b.timestamp.time() <= last_bar_start]
                if closable and session[0].open > 0:
                    paths.append(
                        IntradayPath(
                            symbol=symbol,
                            day=day,
                            daily_open=bar.open,
                            daily_close=bar.close,
                            first_bar_open=session[0].open,
                            cutoff_close=closable[-1].close,
                            first_bar_at=session[0].timestamp.time(),
                            last_bar_at=session[-1].timestamp.time(),
                            bar_count=len(session),
                            prev_close=prev_close,
                            topix100=symbol in topix100_codes,
                        )
                    )
            # **前日終値は5分足の有無と無関係に進める。**
            # ここで飛ばすと、5分足の初日のギャップが「前日なし」になる
            prev_close = bar.close
    return tuple(paths)


@dataclass(frozen=True)
class ClusteredMean:
    """日ごとにまとめてから出した平均と t値（意思決定ログ72）。"""

    days: int
    mean_bps: float
    t_stat: float


def clustered_mean(samples: tuple[tuple[date, float], ...]) -> ClusteredMean | None:
    """``(営業日, 値)`` の列を、日ごとに平均してから日をまたいで集計する。

    **件数ではなく日数が実質的な標本数。** 同じ日の銘柄は市場要因で
    強く相関するので、件数ベースの t値は過大に出る。

    Returns:
        該当日が2日未満なら ``None``。
    """
    by_day: dict[date, list[float]] = defaultdict(list)
    for day, value in samples:
        by_day[day].append(value)
    if len(by_day) < 2:
        return None
    daily = [statistics.fmean(values) for values in by_day.values()]
    mean = statistics.fmean(daily)
    stderr = statistics.stdev(daily) / math.sqrt(len(daily))
    return ClusteredMean(
        days=len(daily),
        mean_bps=mean,
        t_stat=mean / stderr if stderr > 0 else 0.0,
    )


def load_symbols() -> tuple[Symbol, ...]:
    """`universe.json` と TOPIX100 の一覧を合わせて読む。

    **`scripts/fetch_bars.py` が5分足を貯めている集合と揃える**
    （意思決定ログ77）。片方だけ見ると、測れるはずの銘柄を取りこぼす。
    """
    import json

    symbols: dict[str, Symbol] = {}
    for name in ("universe.json", "master_scale_historical.json", "master_scale.json"):
        path = DATA_ROOT / name
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in payload["symbols"]:
            symbol = Symbol(
                code=row["code"],
                name=row["name"],
                market=row.get("market"),
                margin_type=row.get("margin_type"),
                sector=row.get("sector"),
                scale_category=row.get("scale_category"),
            )
            # universe.json は無条件、規模区分の一覧は TOPIX100 だけ
            if name == "universe.json" or symbol.is_topix100:
                symbols.setdefault(symbol.code, symbol)
    if not symbols:
        raise SystemExit(
            f"{DATA_ROOT} に銘柄一覧が無い。先に python scripts/fetch_bars.py を実行する"
        )
    return tuple(symbols.values())


def hr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def _report_open_integrity(paths: tuple[IntradayPath, ...]) -> None:
    """日足の始値と、最初の5分足の始値が一致するか。

    **ずれていれば、これまでの `gap_pct` の計算そのものが疑わしくなる。**
    ギャップは「前日終値 vs 当日始値」で定義しており、その始値を
    日足から取っているため。
    """
    hr("1. 日足の始値 == 最初の5分足の始値 か")
    print("  **ずれていれば、これまでの gap_pct の計算そのものが疑わしい。**")
    print()
    diffs = sorted(abs(p.open_mismatch_bps) for p in paths)
    exact = sum(1 for d in diffs if d < 0.5)
    print(f"  対象            : {len(diffs)}件")
    print(f"  一致(0.5bps未満): {exact}件（{exact / len(diffs):.1%}）")
    print(f"  ずれの中央値    : {diffs[len(diffs) // 2]:.2f}bps")
    print(f"  ずれの最大      : {diffs[-1]:.2f}bps")

    _report_open_mismatch_cause(paths)

    print()
    if exact / len(diffs) > 0.95:
        print("  → **整合している。** 日足の始値を使ってよい")
    else:
        print("  → **整合していない。** 原因の切り分けを上の内訳で見ること")


def _histogram(label: str, times: list[time], limit: int = 5) -> None:
    counts: dict[time, int] = defaultdict(int)
    for t in times:
        counts[t] += 1
    total = len(times)
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    print(f"  {label}（上位{limit}件 / 全{len(counts)}種）")
    for at, n in top:
        print(f"    {at.strftime('%H:%M')}  {n:>6}件（{n / total:>5.1%}）")


def _report_open_mismatch_cause(paths: tuple[IntradayPath, ...]) -> None:
    """始値がずれる原因を切り分ける。

    **考えられる原因は2つあり、対処がまったく違う。**

    1. **最初の5分足が寄り付きのバーではない**（9:00 のバーが欠けている）
       → 日足の始値は正しく、5分足側の欠損。寄り付きで建てる前提は保てる
    2. **9:00 のバーはあるのに値がずれる**
       → 日足と5分足で価格の基準が違う（調整の差など）。
       **この場合は日足ベースの実験すべてが疑わしくなる**

    最初のバーの時刻で分けて中央値を見れば、どちらかが分かる。
    """
    print()
    print("  【原因の切り分け】")
    _histogram("最初のバーの時刻", [p.first_bar_at for p in paths])
    print()
    _histogram("最後のバーの時刻", [p.last_bar_at for p in paths])

    session_open = min(p.first_bar_at for p in paths)
    on_open = sorted(abs(p.open_mismatch_bps) for p in paths if p.first_bar_at == session_open)
    late = sorted(abs(p.open_mismatch_bps) for p in paths if p.first_bar_at != session_open)
    print()
    print(f"  最初のバーが {session_open.strftime('%H:%M')}（＝寄り付き）かどうかで分ける:")
    groups = (
        (f"{session_open.strftime('%H:%M')} から", on_open),
        ("それ以降から", late),
    )
    for label, values in groups:
        if not values:
            print(f"    {label:<12} 該当なし")
            continue
        matched = sum(1 for v in values if v < 0.5)
        print(
            f"    {label:<12} {len(values):>6}件 / 一致 {matched / len(values):>5.1%} / "
            f"ずれの中央値 {values[len(values) // 2]:>7.2f}bps"
        )
    print()
    print("  **寄り付きのバーがある日でもずれるなら、日足と5分足で価格の基準が違う**")
    print("  ——その場合は日足ベースの実験すべてが疑わしくなる。")
    print("  **寄り付きのバーが無い日だけずれるなら、5分足側の欠損**で、")
    print("  日足の始値そのものは信用してよい。")


MIN_BARS_FOR_ANALYSIS = 50
"""成績の集計に使う最低のバー本数。

**実測で1本しか無い日があった。** そういう日の「最初のバー」は
寄り付きとは無関係な時刻なので、混ぜると `entry_at_first_bar` の
測定が壊れる。完全な1日が66本なので、その3/4を下限にする。
"""

SESSION_BARS = 66
"""完全な1日の5分足の本数。

東証は 9:00〜11:30（30本）と 12:30〜15:30（36本）。
**2024年11月から15:30引け**になっている。
"""


def _report_completeness(paths: tuple[IntradayPath, ...]) -> None:
    """5分足がどれだけ欠けているかを、日付と銘柄に分けて診断する。

    **寄り付きのバーが64%の日で欠けている**（意思決定ログ82）。
    日足の実験には影響しないが、**寄り付きで建てる戦略の5分足検証を直撃する**
    ——9:00 のバーが無ければ 9:05 で建てるしかなく、その時点で価格は
    中央値39bps動いている。追いかけている優位（0〜24bps）より大きい。

    **欠損の原因で対処が変わる:**

    - **日付で決まる**（古い日ほど欠ける）→ Yahoo が古い日中データを
      間引いている。週1回の取得では**取りこぼしを埋められない**ので、
      取得頻度を上げるか別のデータ源が要る
    - **銘柄で決まる**→ 流動性。その銘柄を検証から外せばよい
    """
    hr("1.5 5分足はどれだけ欠けているか")
    print(f"  完全な1日は {SESSION_BARS}本（9:00〜11:30 の30本 + 12:30〜15:30 の36本）。")
    print("  **東証は2024年11月から15:30引け。**")
    print()

    counts = sorted(p.bar_count for p in paths)
    print(f"  1日あたりの本数: 中央値 {counts[len(counts) // 2]}本 / "
          f"最小 {counts[0]}本 / 最大 {counts[-1]}本")
    full = sum(1 for c in counts if c >= SESSION_BARS)
    print(f"  {SESSION_BARS}本以上そろった日: {full}件（{full / len(counts):.1%}）")

    session_open = min(p.first_bar_at for p in paths)
    days = sorted({p.day for p in paths})
    boundary = days[len(days) // 2]

    print()
    print(f"  【日付で決まるか】{session_open.strftime('%H:%M')} のバーがある割合")
    for label, subset in (
        (f"前半（〜{boundary}）", [p for p in paths if p.day < boundary]),
        (f"後半（{boundary}〜）", [p for p in paths if p.day >= boundary]),
    ):
        if not subset:
            continue
        has_open = sum(1 for p in subset if p.first_bar_at == session_open)
        median_bars = sorted(p.bar_count for p in subset)[len(subset) // 2]
        print(
            f"    {label:<22} {has_open / len(subset):>6.1%} "
            f"（{len(subset)}件 / 本数の中央値 {median_bars}）"
        )
    print("    → **後半のほうが明らかに高いなら、Yahoo が古い日を間引いている。**")
    print("      週1回の取得では取りこぼしを埋められないので、頻度を上げる必要がある")

    print()
    print(f"  【銘柄で決まるか】{session_open.strftime('%H:%M')} のバーがある割合")
    by_symbol: dict[str, list[bool]] = defaultdict(list)
    for path in paths:
        by_symbol[path.symbol].append(path.first_bar_at == session_open)
    rates = sorted(sum(v) / len(v) for v in by_symbol.values())
    buckets = [0, 0, 0, 0]
    for rate in rates:
        buckets[min(int(rate * 4), 3)] += 1
    for i, n in enumerate(buckets):
        print(f"    {i * 25:>3}〜{(i + 1) * 25:>3}%: {n:>4}銘柄")
    print("    → **銘柄がきれいに二分されるなら流動性の問題**で、")
    print("      欠ける銘柄を検証から外せばよい。**一様に散らばるなら日付側の問題**")


def _report_tail(paths: tuple[IntradayPath, ...]) -> None:
    """14:50 で切ることで失う（または得する）ぶん。"""
    hr(f"2. {CUTOFF.strftime('%H:%M')} で切ると何が変わるか")
    print("  **14:50 は安全装置#2。動かせない。** 閉じ損ねると強制決済で")
    print("  1注文2,200円（目標リターンの約5日分）。")
    print()
    for label, values in (
        ("始値→14:50", [(p.day, p.return_to_cutoff_bps) for p in paths]),
        ("始値→大引け", [(p.day, p.return_to_close_bps) for p in paths]),
        ("14:50→大引け（取りに行けない部分）", [(p.day, p.tail_bps) for p in paths]),
    ):
        stats = clustered_mean(tuple(values))
        if stats is None:
            continue
        print(
            f"  {label:<36} {stats.mean_bps:>+8.2f}bps "
            f"（{stats.days}日 / t={stats.t_stat:>5.1f}）"
        )


def _report_gap_fade(paths: tuple[IntradayPath, ...]) -> None:
    """ギャップ該当日で、フェードの優位が 14:50 までに出ているか。

    **これが本題。** 大引け前10分に優位が集中しているなら、
    日足で測ってきた数字は取りに行けない。
    """
    hr(f"3. ギャップ {GAP_THRESHOLD_PCT:.1%} 以上の日で、優位は 14:50 までに出るか")
    print(f"  **閾値 {GAP_THRESHOLD_PCT:.1%} は日クラスタ t値が最も高かったバケット**")
    print("  （意思決定ログ75）。net bps が最大の 2.0% は t=0.6 だった。")
    print()

    matched = [
        p for p in paths if (g := p.gap_pct) is not None and abs(g) >= GAP_THRESHOLD_PCT
    ]
    # **バーがほとんど無い日を混ぜない。** 実測で1本しか無い日があり、
    # そういう日の「最初のバー」は寄り付きとは無関係な時刻になる。
    candidates = [p for p in matched if p.bar_count >= MIN_BARS_FOR_ANALYSIS]
    dropped = len(matched) - len(candidates)
    if dropped:
        print(f"  バー本数が {MIN_BARS_FOR_ANALYSIS}本未満で除外: {dropped}件")
        print()
    if len(candidates) < 2:
        print("  該当が足りない。5分足がもっと貯まってから")
        return

    # **銘柄群を分ける。** net 正が出たのは TOPIX100 だけ（意思決定ログ67）で、
    # Layer 1（小型〜中型）では3手法とも棄却された。混ぜると比較にならない。
    groups = (
        ("全銘柄", tuple(candidates)),
        ("TOPIX100", tuple(p for p in candidates if p.topix100)),
        ("Layer 1", tuple(p for p in candidates if not p.topix100)),
    )
    for group_label, group in groups:
        if len(group) < 2:
            print(f"  【{group_label}】該当が足りない（{len(group)}件）")
            continue
        print(f"  【{group_label}】{len(group)}件")
        variants = (
            ("日足の始値→大引け（従来の仮定）", False, False),
            (f"日足の始値→{CUTOFF.strftime('%H:%M')}", False, True),
            (f"最初のバー→{CUTOFF.strftime('%H:%M')}（実現可能）", True, True),
        )
        for label, at_first_bar, to_cutoff in variants:
            samples = tuple(
                (p.day, v)
                for p in group
                if (
                    v := p.fade_bps(
                        to_cutoff=to_cutoff, entry_at_first_bar=at_first_bar
                    )
                )
                is not None
            )
            stats = clustered_mean(samples)
            if stats is None:
                continue
            print(
                f"    {label:<34} gross {stats.mean_bps:>+8.2f}bps "
                f"（{stats.days}日 / t={stats.t_stat:>5.1f}）"
            )
        print()

    print("  **「実現可能」の行が本題。** Yahoo の日本株5分足は寄り付きの1本を")
    print("  構造的に落とすので（意思決定ログ83）、日足の始値で建てる仮定は")
    print("  64%の日で実現できない。**この行が従来の仮定と大きく違うなら、")
    print("  優位はあっても取りに行けない。**")
    print()
    print("  **42営業日では判定できない。** 日足の486営業日に対して1割に満たず、")
    print("  t値もほぼゼロになる。ここで見るのは**優位の有無ではなく、")
    print("  14:50 と大引けの差**——それは平均の差なので少ない日数でも測れる。")

    print("  **gross はコスト前。** TOPIX100 の往復コストは約4.8bps、")
    print("  通常銘柄なら約21bps（意思決定ログ67）。")
    print()
    print("  → 14:50 と大引けで**大きく違うなら**、日足で測った優位の一部は")
    print("    そもそも取りに行けない。**ほぼ同じなら**、14:50 の制約は無害")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    print("日足の仮定と5分足の現実のズレを測る")
    print(banner())

    store = BarStore(DATA_ROOT)
    symbols = load_symbols()
    daily = {s.code: store.read(s.code, "1d") for s in symbols}
    intraday = {s.code: store.read(s.code, "5m") for s in symbols}
    daily = {c: b for c, b in daily.items() if b}
    intraday = {c: b for c, b in intraday.items() if b}
    print(f"  銘柄: {len(symbols)} / 日足あり {len(daily)} / 5分足あり {len(intraday)}")

    topix100_codes = frozenset(s.code for s in symbols if s.is_topix100)
    print(f"  うち TOPIX100: {len(topix100_codes)}銘柄")
    paths = intraday_paths(daily, intraday, topix100_codes=topix100_codes)
    if not paths:
        print("  日足と5分足が両方そろう日が無い。先に python scripts/fetch_bars.py")
        return 1
    days = {p.day for p in paths}
    print(f"  突き合わせ: {len(paths)}件 / {len(days)}営業日")
    print(f"  期間      : {min(days)} 〜 {max(days)}")

    _report_open_integrity(paths)
    _report_completeness(paths)
    _report_tail(paths)
    _report_gap_fade(paths)

    print()
    print("**80営業日を待つ必要があるのは戦略の検証であって、仮定のズレの測定ではない。**")
    print("ズレが大きいなら、5分足が貯まる前に前提を作り直す必要がある。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
