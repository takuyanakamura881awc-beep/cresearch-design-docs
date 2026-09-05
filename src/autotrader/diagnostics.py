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

from autotrader.types import Bar

__all__ = [
    "ClusteredStats",
    "PriceDiscontinuity",
    "clustered_stats",
    "drop_discontinuous_symbols",
    "non_overlapping_days",
    "price_discontinuities",
    "required_gross_bps",
    "split_days",
]

TRADING_DAYS_PER_MONTH = 20
MONTHS_PER_YEAR = 12

MAX_DAILY_CLOSE_RATIO = 1.8
"""隣接する日足の終値比の上限。**超えたら値動きではなくデータの継ぎ目を疑う。**

東証には制限値幅があり、終値が前日終値の1.8倍（または 1/1.8）になることは
**制度上ほぼ起こらない**。株価1,000円台なら制限値幅は±20〜30%、
連続ストップ高で拡大しても±40%程度にとどまる。

一方 `BarStore.write` は週次の取得をマージし、yfinance の ``auto_adjust=True`` は
**その取得時点から見た調整**を返す。分割をまたいで別々の週に取得すると、
古いバーは未調整・新しいバーは調整済みのまま**同じ系列に混ざる**。
同じバーの中で完結する値（当日の始値→終値）は無事なのに、
**日をまたぐ値だけが壊れる**（意思決定ログ97）。

**日をまたぐ値を使う診断は、集計より先にこれを確かめること。**
`gap_pct`（前日終値→当日始値）も `prior_move_pct`（前々日→前日）も該当する。
"""

MAX_DISCONTINUITY_ROWS = 10
"""名指しで出す件数。**握り潰さず実物を見せる**（規約「エラーを握り潰さない」）。"""


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


def required_gross_bps(
    annual_target: float,
    *,
    cost_bps: float,
    deployment: float = 1.0,
    horizon_days: int = 1,
) -> float:
    """目標を達成するのに1トレードあたり要る gross（bps）。

    **これは基準ではなく算術。** 目標（年利）とコストと建玉率が決まれば
    一意に決まるので、実験のたびに逆算しなくて済むようにここへ置く。

    導出::

        日次リターン = 建玉率 × 1トレードあたりの net
        年利 = (1 + 日次リターン) ** (12 × 20) - 1

    を net について解き、コストを足す。**建玉率が効く**——資金の半分しか
    建玉になっていなければ、1トレードあたり2倍の優位が要る
    （`scripts/measure_gap_fade.py` の `capacity_stats` が実測する値）。

    **保有期間を延ばすと要求は比例して上がる。** ``horizon_days`` 日持つなら
    回転は 1/N になるので、同じ日次リターンを出すには1トレードあたり N倍の
    net が要る。一方コストは往復1回ぶん（＋金利×日数）しか増えない。

    **つまり「保有期間を延ばせばコストの重みが下がる」は、"損を薄める"
    という意味では正しいが、"目標に届く"という意味では逆**——
    目標に届くには **gross が N倍より速く伸びる**必要がある。
    多くのシグナルは √N 程度でしか伸びないので、この差は効く。

    Args:
        annual_target: 目標年利（0.25 = 25%）。
        cost_bps: 1往復ぶんの総コスト（bps）。スプレッドに加え、
            持ち越すなら金利・貸株料も含める。TOPIX100 のスプレッドは
            約4.8、通常銘柄は約21〜30（`docs/00-overview.md` 意思決定ログ67）。
        deployment: 資金のうち建玉になる割合。1.0 なら常に満玉。
        horizon_days: 保有営業日数。1 なら当日決済（安全装置#2）。

    Returns:
        1トレードあたりに要る gross（bps）。

    Raises:
        ValueError: ``deployment`` が0以下、または ``horizon_days`` が1未満のとき。

    実測との比較（意思決定ログ88）::

        必要（TOPIX100・建玉率100%）  約14bps
        必要（TOPIX100・建玉率62%）   約20bps
        実測で見つかった gross        0〜5bps（市場要因を除いた後）
    """
    if deployment <= 0:
        raise ValueError("deployment は0より大きい")
    if horizon_days < 1:
        raise ValueError("horizon_days は1以上")
    days = TRADING_DAYS_PER_MONTH * MONTHS_PER_YEAR
    daily = float((1.0 + annual_target) ** (1.0 / days)) - 1.0
    net_bps = daily * horizon_days / deployment * 10_000.0
    return net_bps + cost_bps


def non_overlapping_days(
    days: Iterable[date], horizon: int, phase: int = 0
) -> frozenset[date]:
    """``horizon`` 営業日保有するときに、**窓が重ならない**エントリー日だけを返す。

    **なぜ要るのか。** 3日保有を毎日エントリーして測ると、D日に建てた玉と
    D+1・D+2 に建てた玉は**同じ日を共有する**。日クラスタは*エントリー日*で
    まとめているので、この重なりは打ち消せない——**独立な観測は日数ではなく
    日数/N ぶんしかない**。

    重なったまま t値を出すと、およそ **√N 倍** 過大に出る。実測では
    3日保有で t=2.7 と出たが、独立な窓で数えると 1.6 相当だった
    （`docs/00-overview.md` 意思決定ログ90）。

    **これは意思決定ログ72で直したのと同じ構造の欠陥が、横断方向ではなく
    時間方向で起きたもの。** 同じ日の銘柄が独立でないのと同様に、
    重なる保有期間も独立ではない。

    **点推定にはこれを使わないこと。** 間引くとデータの (N-1)/N を捨てるうえ、
    ``phase`` の選び方に結果が依存する——実測では5日保有の gross が
    位相を変えるだけで +31.5 → +10.0bps と3倍動いた（意思決定ログ91）。
    **gross と net は全観測から求め、重なりの補正は t値にだけ効かせる。**

    Args:
        days: 観測のある営業日（重複可）。
        horizon: 保有営業日数。1 なら全日を返す（重なりが無い）。
        phase: 何日目から数え始めるか（``0 <= phase < horizon``）。
            **位相を振ると推定がどれだけ動くかが、そのまま推定の不安定さ。**

    Returns:
        重ならないエントリー日の集合。``phase`` 日目から ``horizon`` 日おきに採る。

    Raises:
        ValueError: ``horizon`` が1未満、または ``phase`` が範囲外のとき。
    """
    if horizon < 1:
        raise ValueError("horizon は1以上")
    if not 0 <= phase < horizon:
        raise ValueError("phase は 0 以上 horizon 未満")
    ordered = sorted(set(days))
    return frozenset(ordered[phase::horizon])


@dataclass(frozen=True)
class PriceDiscontinuity:
    """隣接する日足のあいだで価格水準が跳んだ箇所。"""

    symbol: str
    day: date
    prev_close: float
    close: float

    @property
    def ratio(self) -> float:
        return self.close / self.prev_close


def price_discontinuities(
    daily_bars: dict[str, tuple[Bar, ...]],
    *,
    max_ratio: float = MAX_DAILY_CLOSE_RATIO,
) -> tuple[PriceDiscontinuity, ...]:
    """隣接する日足のあいだで価格水準が跳んだ箇所を洗い出す。

    制限値幅のある市場で終値が ``max_ratio`` 倍になることは起こらないので、
    見つかったら**値動きではなく調整の継ぎ目**を疑う
    （`MAX_DAILY_CLOSE_RATIO` の説明）。

    **ここでは値を丸めない・切らない。** 外れ値を刈るのは、このプロジェクトが
    2度踏んだ切り捨てバイアス（意思決定ログ59・71）そのもの。壊れた系列は
    **銘柄ごと除外する**のが正しく、値をいじるのは誤り。
    """
    found: list[PriceDiscontinuity] = []
    for symbol in sorted(daily_bars):
        ordered = sorted(daily_bars[symbol], key=lambda b: b.timestamp)
        for prev, today in zip(ordered, ordered[1:], strict=False):
            if prev.close <= 0 or today.close <= 0:
                continue
            ratio = today.close / prev.close
            if ratio > max_ratio or ratio < 1.0 / max_ratio:
                found.append(
                    PriceDiscontinuity(
                        symbol=symbol,
                        day=today.timestamp.date(),
                        prev_close=prev.close,
                        close=today.close,
                    )
                )
    return tuple(found)


def drop_discontinuous_symbols(
    daily_bars: dict[str, tuple[Bar, ...]],
    *,
    max_ratio: float = MAX_DAILY_CLOSE_RATIO,
) -> dict[str, tuple[Bar, ...]]:
    """継ぎ目のあった銘柄を除いた日足を返し、**除いた分を名指しで印字する。**

    どちらの区間が正しく調整されているか判別できない以上、値をいじるのでは
    なく銘柄ごと落とす。**黙って落とさない**——件数・銘柄数・最悪ケースと、
    直し方（parquet を消して取り直す）まで出す。
    """
    breaks = price_discontinuities(daily_bars, max_ratio=max_ratio)
    if not breaks:
        print(f"  価格水準の跳び（終値比 {max_ratio:.1f}倍超）: なし")
        return daily_bars

    affected = frozenset(b.symbol for b in breaks)
    print(f"  **価格水準の跳びを {len(breaks)}件 / {len(affected)}銘柄で検出**")
    print(f"  （終値比が {1.0 / max_ratio:.2f}〜{max_ratio:.1f}倍の外）")
    print()
    print("  制限値幅のある市場でこの跳びは起こらない。**値動きではなく")
    print("  調整の継ぎ目**——`BarStore` は週次の取得をマージするが、yfinance の")
    print("  auto_adjust はその取得時点から見た調整を返すので、分割をまたいで")
    print("  別々の週に取得すると未調整と調整済みが同じ系列に混ざる。")
    print()
    print(f"  {'銘柄':<8} {'日付':<12} {'前日終値':>10} {'当日終値':>10} {'比':>8}")
    print("  " + "-" * 52)
    ranked = sorted(breaks, key=lambda x: abs(math.log(x.ratio)), reverse=True)
    for b in ranked[:MAX_DISCONTINUITY_ROWS]:
        print(
            f"  {b.symbol:<8} {b.day!s:<12} {b.prev_close:>10.1f} "
            f"{b.close:>10.1f} {b.ratio:>8.3f}"
        )
    if len(breaks) > MAX_DISCONTINUITY_ROWS:
        print(f"  ほか{len(breaks) - MAX_DISCONTINUITY_ROWS}件")
    print()
    print(f"  **該当 {len(affected)}銘柄を丸ごと除外する。** 値の刈り込みは")
    print("  意思決定ログ59・71で踏んだ切り捨てバイアスそのもの。")
    print()
    print("  **直すには日足を作り直す**: data/bars/1d/<銘柄>.parquet を消して")
    print("  python scripts/fetch_bars.py を実行する（2年ぶんを取り直す）。")

    kept = {c: b for c, b in daily_bars.items() if c not in affected}
    print()
    print(f"  残った銘柄: {len(kept)} / {len(daily_bars)}")
    return kept
