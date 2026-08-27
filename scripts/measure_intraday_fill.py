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
    """``CUTOFF`` 以前の最後の5分足の終値。**実際に手仕舞える価格。**"""
    prev_close: float | None
    """前日の日足終値。ギャップの計算に要る。銘柄の初日は ``None``。"""

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

    def fade_bps(self, *, to_cutoff: bool) -> float | None:
        """ギャップと逆に動いたら正。``to_cutoff`` で 14:50 と大引けを切り替える。

        `scripts/measure_gap_fade.py` の `fade_score` と同じ符号の規約。
        **式を写しているのではなく、同じ規約を別の測定単位（bps・14:50 まで）で
        使っている**——あちらは日足だけ、こちらは5分足を突き合わせる。
        """
        gap = self.gap_pct
        if gap is None or gap == 0:
            return None
        move = self.return_to_cutoff_bps if to_cutoff else self.return_to_close_bps
        return -move if gap > 0 else move


def intraday_paths(
    daily_bars: dict[str, tuple[Bar, ...]],
    intraday_bars: dict[str, tuple[Bar, ...]],
    cutoff: time = CUTOFF,
) -> tuple[IntradayPath, ...]:
    """日足と5分足を、銘柄 × 営業日で突き合わせる。

    **両方そろっている日だけを返す。** 5分足がまだ58日ぶんしかないので、
    日足（2年）の大半は対象外になる——それが正しい挙動で、
    片方しか無い日を無理に埋めない。

    ``cutoff`` 以前の5分足が1本も無い日は除く（手仕舞えないので測れない）。
    価格が0以下の日も除く（0除算対策）。
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
                closable = [b for b in session if b.timestamp.time() <= cutoff]
                if closable and session[0].open > 0:
                    paths.append(
                        IntradayPath(
                            symbol=symbol,
                            day=day,
                            daily_open=bar.open,
                            daily_close=bar.close,
                            first_bar_open=session[0].open,
                            cutoff_close=closable[-1].close,
                            prev_close=prev_close,
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
    diffs = [abs(p.open_mismatch_bps) for p in paths]
    exact = sum(1 for d in diffs if d < 0.5)
    print(f"  対象      : {len(diffs)}件")
    print(f"  一致(0.5bps未満): {exact}件（{exact / len(diffs):.1%}）")
    diffs.sort()
    print(f"  ずれの中央値    : {diffs[len(diffs) // 2]:.2f}bps")
    print(f"  ずれの最大      : {diffs[-1]:.2f}bps")
    print()
    if exact / len(diffs) > 0.95:
        print("  → **整合している。** 日足の始値を使ってよい")
    else:
        print("  → **整合していない。** 日足の始値を前提にした実験を見直す必要がある")


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

    candidates = [
        p for p in paths if (g := p.gap_pct) is not None and abs(g) >= GAP_THRESHOLD_PCT
    ]
    if len(candidates) < 2:
        print("  該当が足りない。5分足がもっと貯まってから")
        return

    print(f"  該当: {len(candidates)}件")
    print()
    for label, to_cutoff in (("14:50 で手仕舞い", True), ("大引けで手仕舞い", False)):
        samples = tuple(
            (p.day, v) for p in candidates if (v := p.fade_bps(to_cutoff=to_cutoff)) is not None
        )
        stats = clustered_mean(samples)
        if stats is None:
            continue
        print(
            f"  {label:<20} gross {stats.mean_bps:>+8.2f}bps "
            f"（{stats.days}日 / t={stats.t_stat:>5.1f}）"
        )

    print()
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

    paths = intraday_paths(daily, intraday)
    if not paths:
        print("  日足と5分足が両方そろう日が無い。先に python scripts/fetch_bars.py")
        return 1
    days = {p.day for p in paths}
    print(f"  突き合わせ: {len(paths)}件 / {len(days)}営業日")
    print(f"  期間      : {min(days)} 〜 {max(days)}")

    _report_open_integrity(paths)
    _report_tail(paths)
    _report_gap_fade(paths)

    print()
    print("**80営業日を待つ必要があるのは戦略の検証であって、仮定のズレの測定ではない。**")
    print("ズレが大きいなら、5分足が貯まる前に前提を作り直す必要がある。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
