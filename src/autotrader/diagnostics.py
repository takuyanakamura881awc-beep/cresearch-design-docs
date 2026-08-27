"""診断に共通して要る統計。**スクリプト間で重複させない。**

【なぜモジュールに出すのか】

`scripts/measure_gap_fade.py` と `scripts/measure_intraday_fill.py` が
**同じ日クラスタ統計をそれぞれ実装していた**。3つ目の診断
（`scripts/measure_overnight_reversal.py`）でも要るので、
規約「既存の関数を探してから新規に書く（同じことをする関数を二つ作らない）」
に従ってここへ出す。

【日クラスタとは何か・なぜ要るか】

バケットの t値を件数（5,786件など）から計算すると、**同じ日の銘柄同士が
市場要因で強く相関している**ことを無視することになる。ある日に市場全体が
上げれば、その日のギャップダウン銘柄はまとめてフェード側に振れる——
**独立な観測が5,786個あるのではなく、せいぜい日数ぶんしかない。**

実測では件数ベースの t値 8.0〜10.1 が、日クラスタで 0.6〜3.3 に落ちた
（`docs/00-overview.md` 意思決定ログ72・75）。**この差は結論を左右する**
——net bps が最大だったバケットの t値は 0.6 で、ゼロと区別できなかった。

日ごとに平均してから日をまたいで t値を出せば、実質的な標本数が日数になる。
**点推定もわずかに変わる**（日ごとに等ウェイトになるため）が、それは
意図した挙動——銘柄が多く該当した日を過大に扱わない。
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

__all__ = ["ClusteredStats", "clustered_stats", "split_days"]


@dataclass(frozen=True)
class ClusteredStats:
    """日ごとにまとめてから出した統計。**同じ日の銘柄は独立ではない。**"""

    days: int
    """実質的な標本数。**件数ではなくこちらが効く。**"""
    mean_bps: float
    """日ごとの平均を、さらに日をまたいで平均したもの。"""
    t_stat: float
    """平均がゼロと区別できるか。日をまたいだばらつきが0なら0を返す。"""


def clustered_stats(samples: Iterable[tuple[date, float]]) -> ClusteredStats | None:
    """``(営業日, 値)`` の列を、日ごとに平均してから日をまたいで集計する。

    **件数を増やしても日数が同じなら t値は変わらない。** 同じ日を1日9銘柄で
    見ても18銘柄で見ても、市場の動きが同じなら得られた情報は増えていない。

    Returns:
        該当日が2日未満なら ``None``（日をまたいだ標準偏差が計算できない）。
    """
    by_day: dict[date, list[float]] = defaultdict(list)
    for day, value in samples:
        by_day[day].append(value)
    if len(by_day) < 2:
        return None

    daily = [statistics.fmean(values) for values in by_day.values()]
    mean = statistics.fmean(daily)
    stderr = statistics.stdev(daily) / math.sqrt(len(daily))
    return ClusteredStats(
        days=len(daily),
        mean_bps=mean,
        t_stat=mean / stderr if stderr > 0 else 0.0,
    )


def split_days(days: Iterable[date]) -> tuple[frozenset[date], frozenset[date]]:
    """営業日を前半・後半に二分する。

    **`--stress-test` で竹にかけた規律を、他の診断にも同じ形で当てる**
    （`docs/00-overview.md` 意思決定ログ46）。片方の期間だけで勝っていれば
    偶然を疑う。**期間分割は「通れば合格」ではなく落とすための試験。**

    **銘柄ではなく日で切る。** 日数の中央値で分けるので、同じ日の観測が
    前半と後半にまたがることはない（該当件数は偏りうる——それ自体が情報になる）。

    Returns:
        ``(前半, 後半)``。営業日が2日未満なら ``(全部, 空)``。
    """
    ordered = sorted(set(days))
    if len(ordered) < 2:
        return (frozenset(ordered), frozenset())
    boundary = ordered[len(ordered) // 2]
    return (
        frozenset(d for d in ordered if d < boundary),
        frozenset(d for d in ordered if d >= boundary),
    )
