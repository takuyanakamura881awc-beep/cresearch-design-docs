#!/usr/bin/env python3
"""ギャップ（前日終値と当日始値の差）がその日のうちに埋まる傾向があるか、安く診断する。

    python scripts/measure_gap_fade.py

事前に ``python scripts/fetch_bars.py`` などで ``data/`` に日足を
蓄積しておく（新規のネットワーク取得は行わない。ローカルの
``BarStore`` キャッシュだけを読む）。

【なぜこの診断なのか】

竹（ORB+VWAP混合）、VWAP乖離の2方向拡張（乖離2.0%・出来高確認）は
いずれも棄却が確定した（`docs/00-overview.md` 意思決定ログ46・50・52）。
ORB・VWAP乖離という手元の手がかりは使い切ったので、新しいシグナル発想が要る。

板情報は Stage A にないため使えない。**ギャップはまだ検証していない**うえ、
**日足だけで安く予備検証できる**という利点がある——5分足はまだ39〜80営業日
しかないが、日足は J-Quants 無料で最大2年（意思決定ログ53）。5分足の
検証環境（Layer2選定・約定モデル・リスクチェック）を一切構築せずに、
「そもそもこの銘柄群でギャップはフェードする傾向があるか」を桁違いに
大きい母数で先に見られる。

**Layer2 選定で使う `gap_pct`（寄り前気配 vs 前日終値）とは別物。**
選定用の寄り前気配は Stage A では取得できない（`docs/03-universe.md`）。
ここで見るのは**当日の実際の始値**——寄り付いた瞬間には確定している
情報で、先読みにならない。

【まだ合否判定はしない】

`--stress-test` のような多重比較補正した棄却判定はまだ行わない。
バケット間で符号・大きさが一貫してフェード側に振れているかを
目視で確認する診断であり、仮説を戦略化する段階で初めて検定が要る。
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from autotrader.data.store import BarStore
from autotrader.diagnostics import (
    ClusteredStats,
    drop_discontinuous_symbols,
    split_days,
)
from autotrader.diagnostics import clustered_stats as clustered_from_samples
from autotrader.provenance import banner
from autotrader.tick import DEFAULT_SPREAD_TICKS, spread_yen
from autotrader.types import Bar, Symbol

DATA_ROOT = Path("data")

GAP_BUCKETS_PCT: tuple[float, ...] = (0.005, 0.010, 0.015, 0.020)
"""ギャップの下限バケット。VWAP乖離のスイープ（0.7/1.0/1.5/2.0%）と同じ刻み方。"""

CAPITAL_CANDIDATES_YEN: tuple[int, ...] = (
    500_000,
    800_000,
    1_200_000,
    2_000_000,
    3_000_000,
)
"""資金スイープの候補。`scripts/measure_topix100.py` の刻みと揃えてある。"""

MAX_CONCURRENT = 5
"""同時保有の上限（安全装置#7）。"""

MAX_POSITION_PCT = 0.25
"""1銘柄あたりの建玉上限（安全装置#7）。**動かさない**（意思決定ログ21）。"""

LOT_SIZE = 100
"""単元株数。"""

TRADING_DAYS_PER_MONTH = 20
"""月利への換算に使う営業日数。"""

TRADING_DAYS_PER_YEAR = 245
"""年利・シャープレシオへの換算に使う営業日数。"""

DAILY_BREAKER_PCT = -0.02
"""日次ブレーカー（安全装置#4）。この損失に達した日は当日の取引を止める。"""

CAPACITY_THRESHOLD_PCT = 0.020
"""建玉シミュレーションで使うギャップ下限。**最も net の良いバケット**を使う。"""


@dataclass(frozen=True)
class GapFadePair:
    """1銘柄・1営業日ぶんのギャップとその日の値動き。"""

    symbol: str
    day: date
    """当日の日付。**同じ日のシグナルをまとめる**ために要る。

    1日に何件シグナルが出るか・同時保有5枠（安全装置#7）が埋まるかは、
    日をまたいで平均した bps からは分からない。
    """
    gap_pct: float
    """(当日始値 - 前日終値) / 前日終値。"""
    intraday_return_pct: float
    """(当日終値 - 当日始値) / 当日始値。"""
    open_price: float
    """当日始値。**コストは株価で決まる**ので必要（`autotrader.tick`）。

    ギャップ・フェード戦略として実装するなら始値付近で建てることになるので、
    往復コストの見積りもこの価格を基準にする。
    """
    topix100: bool = False
    """TOPIX100 構成銘柄か。**呼値が1桁違うのでコストに直接効く**
    （`autotrader.types.Symbol.is_topix100`・意思決定ログ61）。"""


def gap_fade_pairs(
    daily_bars: dict[str, tuple[Bar, ...]],
    topix100_codes: frozenset[str] = frozenset(),
) -> tuple[GapFadePair, ...]:
    """銘柄ごとの日足から、ギャップと当日の値動きのペアを作る。

    **銘柄の初日（前日終値がない）は除外する。** 始値・前日終値が0以下の
    日も除外する（0除算対策。`autotrader.regime.daily_range_pct` と同じ規律）。

    Args:
        daily_bars: 銘柄コード → 日足。
        topix100_codes: TOPIX100 構成銘柄のコード。**呼値が1桁違う**ので
            コスト計算で区別する（意思決定ログ61・64）。
    """
    pairs: list[GapFadePair] = []
    for symbol, series in daily_bars.items():
        ordered = sorted(series, key=lambda b: b.timestamp)
        for prev, today in zip(ordered, ordered[1:], strict=False):
            if prev.close <= 0 or today.open <= 0:
                continue
            gap_pct = (today.open - prev.close) / prev.close
            intraday_return_pct = (today.close - today.open) / today.open
            pairs.append(
                GapFadePair(
                    symbol=symbol,
                    day=today.timestamp.date(),
                    gap_pct=gap_pct,
                    intraday_return_pct=intraday_return_pct,
                    open_price=today.open,
                    topix100=symbol in topix100_codes,
                )
            )
    return tuple(pairs)


def fade_score(pair: GapFadePair) -> float:
    """ギャップ方向と逆に動いたら正（フェード）、伸びたら負（ギャップ&ゴー）。

    ギャップがゼロの日は符号がないので0を返す（フェードもギャップ&ゴーもない）。
    """
    if pair.gap_pct > 0:
        return -pair.intraday_return_pct
    if pair.gap_pct < 0:
        return pair.intraday_return_pct
    return 0.0


def round_trip_cost_bps(
    pair: GapFadePair, n_ticks: float = DEFAULT_SPREAD_TICKS
) -> float:
    """この銘柄・この日に建てたときの往復コスト（bps）。

    `autotrader.tick.spread_yen` をそのまま使う——**約定コストの
    モデルを診断ごとに作り直さない**（`docs/00` 意思決定ログ33以降で
    呼値ベースに統一済み）。往復でスプレッド1本ぶんを払う。

    ``n_ticks`` を変えられるのは**感度を見るため**。スプレッドは
    Stage A では実測できず（意思決定ログ57〜60）、この1つの数値が
    net を線形に動かすので、「想定を変えたら結論が変わるのか」を
    確かめられるようにしてある。**既定値は動かさない。**
    """
    spread = spread_yen(pair.open_price, n_ticks, topix100=pair.topix100)
    return float(spread) / pair.open_price * 10_000.0


@dataclass(frozen=True)
class BucketStats:
    """1バケット（|ギャップ| がある下限以上の日）の集計。"""

    n: int
    gross_bps: float
    """fade_score の平均を bps にしたもの。**コスト前**。"""
    cost_bps: float
    """往復コストの平均（bps）。"""
    stderr_bps: float
    """gross の標準誤差（bps）。件数が増えるほど小さくなる。"""

    @property
    def net_bps(self) -> float:
        """コストを引いた後。**これが正でなければ取引する意味がない。**"""
        return self.gross_bps - self.cost_bps

    @property
    def t_stat(self) -> float:
        """gross がゼロと区別できるか。標準誤差が0なら0を返す。

        **統計的な有意性と、コスト後に残るかは別の話。** 有意でも
        コストに負けることはある（このプロジェクトでは実際にそうなった
        ——`docs/00` 意思決定ログ36の「損益分岐の勝率」と同じ構造）。
        """
        if self.stderr_bps <= 0:
            return 0.0
        return self.gross_bps / self.stderr_bps


def bucket_stats(
    pairs: tuple[GapFadePair, ...],
    threshold: float,
    n_ticks: float = DEFAULT_SPREAD_TICKS,
) -> BucketStats | None:
    """``|gap_pct| >= threshold`` の日だけを集計する。

    Returns:
        該当が2件未満なら ``None``（標準偏差が計算できない）。
    """
    bucket = [p for p in pairs if abs(p.gap_pct) >= threshold]
    if len(bucket) < 2:
        return None

    scores_bps = [fade_score(p) * 10_000.0 for p in bucket]
    costs_bps = [round_trip_cost_bps(p, n_ticks) for p in bucket]
    mean = statistics.fmean(scores_bps)
    stderr = statistics.stdev(scores_bps) / math.sqrt(len(scores_bps))
    return BucketStats(
        n=len(bucket),
        gross_bps=mean,
        cost_bps=statistics.fmean(costs_bps),
        stderr_bps=stderr,
    )


def trade_side(pair: GapFadePair) -> str:
    """このギャップをフェードするなら、買建と売建のどちらになるか。

    **この区別は集計の都合ではなく、安全装置の適用範囲が変わる。**
    ギャップアップをフェードする＝**売建**であり、安全装置#3
    （ショート建玉はストップ注文なしで作らない）と、一般信用デイトレの
    売建可能銘柄リスト（Stage A では取得できない・`docs/09` §2.5）の
    両方に縛られる。買建だけで成立するなら、その両方を回避できる。
    """
    return "売建" if pair.gap_pct > 0 else "買建"


def market_drift_bps(pairs: tuple[GapFadePair, ...]) -> float:
    """全ペアの平均 ``始値→終値``（bps）。**日中の相場ドリフト。**

    これが正だと、**ギャップダウンをフェードする側（買建）だけが
    自動的に得をする**——`fade_score` はギャップダウン日には
    `始値→終値` そのものだから。フェードの効果ではなく相場の上昇を
    測っているだけかもしれない、という疑いの出発点。
    """
    if not pairs:
        return 0.0
    return statistics.fmean(p.intraday_return_pct for p in pairs) * 10_000.0


def demean_by_day(pairs: tuple[GapFadePair, ...]) -> tuple[GapFadePair, ...]:
    """その日の全銘柄の平均 ``始値→終値`` を各銘柄から差し引く。

    **市場全体がその日どちらに動いたかを取り除き、銘柄固有の動きだけを残す。**
    上昇局面を切り取っただけの見かけの優位と、本物の平均回帰を分けるための操作。

    **事後診断であって、実戦の変種ではない。** その日の市場平均は
    引けまで分からないので、この控除を実際の売買で行うことはできない
    （`autotrader.regime` の calm/wild 分類と同じ位置づけ——
    「なぜ効いたか」の説明には使えるが、エントリー判断には使えない）。
    先物やETFでヘッジすれば近似はできるが、資金50万円・Stage A では
    別の話なのでここでは踏み込まない。
    """
    by_day: dict[date, list[GapFadePair]] = defaultdict(list)
    for pair in pairs:
        by_day[pair.day].append(pair)

    out: list[GapFadePair] = []
    for group in by_day.values():
        mean_move = statistics.fmean(p.intraday_return_pct for p in group)
        out.extend(
            replace(p, intraday_return_pct=p.intraday_return_pct - mean_move) for p in group
        )
    return tuple(out)


@dataclass(frozen=True)
class CapacityStats:
    """ある資金額で、この戦略が実際に何をどれだけ建てられるか。

    **bps は「1回の取引でいくら取れるか」しか言わない。** 月利は
    「1日に何回建てられるか」「資金のうち何割が建玉になるか」で決まり、
    それは資金額（＝買える銘柄の株価上限）に強く依存する。
    """

    capital_yen: int
    symbols_used: int
    """実際に1回以上建てられた銘柄数。**分散が効くかの目安**（安全装置#7）。"""
    days: int
    """対象営業日数。**シグナルが1件も出ない日も分母に入れる。**"""
    mean_slots_filled: float
    """1日あたりに埋まった枠数（上限は ``MAX_CONCURRENT``）。"""
    mean_deployed_pct: float
    """1日あたり、資金のうち建玉になった割合。レバ1倍なので100%が上限。"""
    monthly_return_pct: float
    """net の月利（%）。**ストップもブレーカーも含まない上限見積り。**"""
    daily_stdev_pct: float
    """日次リターンの標準偏差（%）。**リターンだけ見ても運か実力か分からない。**"""
    worst_day_pct: float
    """最悪の1日（%）。"""
    breaker_days: int
    """日次ブレーカー（-2%・安全装置#4）に達した日数。

    **シミュレーションはブレーカーを適用していない。** ここで数えているのは
    「もし動かしていたら何日で強制停止していたか」であり、多いほど
    **戦略が安全装置の設計前提と衝突している**ことを意味する。
    """
    max_drawdown_pct: float
    """日次リターンを積み上げたときの最大下落幅（%）。累積DD(-15%・安全装置#6)と比べる。"""

    @property
    def annual_return_pct(self) -> float:
        """年利（%）。**目標は年利25〜35%**（意思決定ログ73）。"""
        monthly = self.monthly_return_pct / 100.0
        return (float((1.0 + monthly) ** 12.0) - 1.0) * 100.0

    @property
    def sharpe(self) -> float:
        """年率シャープレシオ。**このプロジェクトの評価軸**（意思決定ログ9）。

        無リスク金利は0とみなす（`report/metrics.py` と同じ扱い）。
        リターンだけを追うと、たまたま勝った高リスク手法を選んでしまう。
        """
        if self.daily_stdev_pct <= 0:
            return 0.0
        daily_mean = self.monthly_return_pct / TRADING_DAYS_PER_MONTH
        return daily_mean / self.daily_stdev_pct * math.sqrt(TRADING_DAYS_PER_YEAR)


def capacity_stats(
    pairs: tuple[GapFadePair, ...],
    capital_yen: int,
    threshold: float = CAPACITY_THRESHOLD_PCT,
    n_ticks: float = DEFAULT_SPREAD_TICKS,
    universe_days: frozenset[date] | None = None,
) -> CapacityStats | None:
    """資金額を固定して、日ごとに枠を埋めていく建玉シミュレーション。

    守るもの（**安全装置をバイパスしない**）:

    - 1銘柄の建玉 ≤ ``capital_yen × MAX_POSITION_PCT``（安全装置#7）
    - 同時保有 ≤ ``MAX_CONCURRENT``（安全装置#7）
    - 建玉総額 ≤ 現金残高（レバレッジ1倍の不変条件）
    - 単元株（100株）単位でしか建てられない

    シグナルが多い日は**ギャップの大きい順**に埋める。乖離が大きいほど
    成績が良いという観測（意思決定ログ56・66）に従った並びで、
    ここで新しいパラメータを作らない。

    ``universe_days`` は営業日の分母を外から与える。**片方の方向だけを
    渡すとき（買建のみなど）に要る**——その方向のシグナルが1件も出ない日が
    黙って分母から消えると、建てなかった日を「なかったこと」にしてしまい、
    月利が過大に出る。

    **これは上限見積りであって、バックテストではない。**

    - 損切り・利確を含まない（始値で建てて引けで手仕舞うだけ）
    - ブレーカー三層（安全装置#4〜6）を含まない
    - 5分足の約定モデルを通していない（日足の始値＝約定価格と仮定）
    - 売建の可否（一般信用の在庫）を確認していない

    どれも**成績を良い側に倒す**方向の単純化なので、ここで出る月利は
    実際に得られる値の上限として読む（規約「検証できないものは
    保守的な側に倒す」の裏返し——楽観側の数字はそう明示する）。

    Returns:
        対象日が1日もなければ ``None``。
    """
    per_position_cap = capital_yen * MAX_POSITION_PCT

    by_day: dict[date, list[GapFadePair]] = defaultdict(list)
    for pair in pairs:
        by_day[pair.day].append(pair)
    days = sorted(universe_days) if universe_days is not None else sorted(by_day)
    if not days:
        return None

    used_symbols: set[str] = set()
    daily_returns: list[float] = []
    slots: list[int] = []
    deployed: list[float] = []

    for day in days:
        candidates = sorted(
            (p for p in by_day[day] if abs(p.gap_pct) >= threshold),
            key=lambda p: abs(p.gap_pct),
            reverse=True,
        )
        cash = float(capital_yen)
        filled = 0
        pnl_yen = 0.0
        exposure = 0.0
        for pair in candidates:
            if filled >= MAX_CONCURRENT:
                break
            budget = min(per_position_cap, cash)
            lots = int(budget // (pair.open_price * LOT_SIZE))
            if lots < 1:
                continue
            value = lots * LOT_SIZE * pair.open_price
            net_pct = fade_score(pair) - round_trip_cost_bps(pair, n_ticks) / 10_000.0
            pnl_yen += value * net_pct
            cash -= value
            exposure += value
            filled += 1
            used_symbols.add(pair.symbol)
        daily_returns.append(pnl_yen / capital_yen)
        slots.append(filled)
        deployed.append(exposure / capital_yen)

    equity = 1.0
    peak = 1.0
    drawdown = 0.0
    for daily in daily_returns:
        equity *= 1.0 + daily
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1.0)

    return CapacityStats(
        capital_yen=capital_yen,
        symbols_used=len(used_symbols),
        days=len(daily_returns),
        mean_slots_filled=statistics.fmean(slots),
        mean_deployed_pct=statistics.fmean(deployed) * 100.0,
        monthly_return_pct=statistics.fmean(daily_returns) * TRADING_DAYS_PER_MONTH * 100.0,
        daily_stdev_pct=(
            statistics.stdev(daily_returns) * 100.0 if len(daily_returns) > 1 else 0.0
        ),
        worst_day_pct=min(daily_returns) * 100.0,
        breaker_days=sum(1 for d in daily_returns if d <= DAILY_BREAKER_PCT),
        max_drawdown_pct=drawdown * 100.0,
    )


def clustered_stats(
    pairs: tuple[GapFadePair, ...], threshold: float
) -> ClusteredStats | None:
    """t値を日クラスタで出し直す。

    **なぜ要るのか。** バケットの t値を件数（5,786件など）から計算すると、
    **同じ日の銘柄同士が市場要因で強く相関している**ことを無視することになる。
    ある日に市場全体が上げれば、その日のギャップダウン銘柄はまとめて
    フェード側に振れる——独立な観測が5,786個あるのではなく、
    **せいぜい日数ぶんしかない**。

    実際、市場ドリフトを控除すると買建の優位は +35.95 → +4.94bps に落ちた
    （意思決定ログ71）。**消えたぶんは日単位で共通の動き**であり、
    そこに件数ぶんの信頼を置いてはいけない。

    日ごとに平均してから日をまたいで t値を出せば、実質的な標本数が
    日数になる。**点推定もわずかに変わる**（日ごとに等ウェイトになるため）が、
    それは意図した挙動——銘柄が多く該当した日を過大に扱わない。

    Returns:
        該当日が2日未満なら ``None``。
    """
    return clustered_from_samples(
        (p.day, fade_score(p) * 10_000.0)
        for p in pairs
        if abs(p.gap_pct) >= threshold
    )


def load_symbols() -> tuple[Symbol, ...]:
    """`scripts/backtest_take.py` の同名関数と同じ読み込み。

    **重複させている。** スクリプトファイルは pythonpath に乗らないため
    （`tests/test_backtest_take_script.py` の docstring 参照）、
    スクリプト間で import せず、それぞれ自己完結させる。
    """
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
            scale_category=r.get("scale_category"),
        )
        for r in payload["symbols"]
    )


def hr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def report(pairs: tuple[GapFadePair, ...]) -> None:
    if not pairs:
        print("  データがない。先に python scripts/fetch_bars.py を実行する")
        return

    baseline = bucket_stats(pairs, 0.0)
    if baseline is not None:
        print(
            f"  全日ベースライン: 件数 {baseline.n:>6} / "
            f"gross {baseline.gross_bps:>+7.2f}bps"
        )
    print()
    print(
        f"  {'|ギャップ|下限':<12} {'件数':>7} {'gross':>9} {'コスト':>9} "
        f"{'net':>9} {'t値(件)':>8} {'日数':>6} {'t値(日)':>8}"
    )
    print("  " + "-" * 74)
    for threshold in GAP_BUCKETS_PCT:
        stats = bucket_stats(pairs, threshold)
        if stats is None:
            print(f"  {threshold:>10.1%}  {0:>7}  —（該当なし）")
            continue
        clustered = clustered_stats(pairs, threshold)
        cluster_txt = (
            f"{clustered.days:>6} {clustered.t_stat:>7.1f}"
            if clustered
            else f"{'—':>6} {'—':>8}"
        )
        print(
            f"  {threshold:>10.1%}  {stats.n:>7} "
            f"{stats.gross_bps:>+8.2f}b {stats.cost_bps:>8.2f}b "
            f"{stats.net_bps:>+8.2f}b {stats.t_stat:>7.1f} {cluster_txt}"
        )

    print()
    print("  **gross はコスト前、net はコストを引いた後。** t値は gross が")
    print("  ゼロと区別できるかで、**コスト後に残るかとは別の話**。")
    print("  net が負なら、傾向が統計的に本物でも取引する意味はない。")
    print()
    print("  **見るべきは t値(日) のほう。** 同じ日の銘柄は市場要因で強く")
    print("  相関しているので、件数ぶんの独立した観測があるわけではない。")
    print("  実質的な標本数は日数であり、t値(件) はそのぶん過大に出る。")
    print()
    print("  往復コストは呼値2tick想定（`autotrader.tick`）。始値で建てて")
    print("  引けで手仕舞う前提の、最も楽観的な見積り——実際は寄り付き直後の")
    print("  スプレッドはこれより広い。**net が僅差で正でも安心できない。**")

    _report_by_side(pairs)
    _report_period_split(pairs)
    _report_drift_adjusted(pairs)
    _report_spread_sensitivity(pairs)


def _report_by_side(pairs: tuple[GapFadePair, ...]) -> None:
    """買建（ギャップダウンのフェード）と売建（ギャップアップのフェード）に分ける。

    **売建には買建にない制約が2つ乗る**（`trade_side` の docstring 参照）。
    買建だけで成立するなら、安全装置#3 と売建可能銘柄リストの両方を
    回避できるので、実装の難易度が大きく下がる。

    **これは診断であって、片方を選ぶ根拠にはまだしない。** 方向で分けた
    時点で比較の数が増えており、片方が良く見えるのは偶然でも起きる
    （意思決定ログ45の多重比較補正と同じ問題）。
    """
    longs = tuple(p for p in pairs if p.gap_pct < 0)
    shorts = tuple(p for p in pairs if p.gap_pct > 0)
    print()
    print("  【方向別】買建=ギャップダウンのフェード / 売建=ギャップアップのフェード")
    print("  売建は安全装置#3（ストップ必須）と売建可能銘柄リストに縛られる。")
    print()
    print(f"  {'|ギャップ|下限':<12} {'買建 件数':>9} {'net':>9}   {'売建 件数':>9} {'net':>9}")
    print("  " + "-" * 60)
    for threshold in GAP_BUCKETS_PCT:
        lo = bucket_stats(longs, threshold)
        sh = bucket_stats(shorts, threshold)
        lo_txt = f"{lo.n:>9} {lo.net_bps:>+8.2f}b" if lo else f"{'—':>9} {'—':>9}"
        sh_txt = f"{sh.n:>9} {sh.net_bps:>+8.2f}b" if sh else f"{'—':>9} {'—':>9}"
        print(f"  {threshold:>10.1%}  {lo_txt}   {sh_txt}")
    print()
    print("  **片方だけが良くても、それだけで方向を絞らない。** 方向で分けた")
    print("  分だけ比較の数が増えており、偶然に片寄る余地が生まれている。")


def split_by_period(
    pairs: tuple[GapFadePair, ...],
) -> tuple[tuple[GapFadePair, ...], tuple[GapFadePair, ...]]:
    """営業日の中央で前半・後半に二分する。

    **`--stress-test` で竹にかけた規律を、ギャップにも適用する**
    （意思決定ログ46）。片方の期間だけで勝っていれば偶然を疑う。

    **銘柄ではなく日で切る。** 日数の中央値で分けるので、
    片方に日が偏らない（該当件数は偏りうる——それ自体が情報になる）。
    """
    first_days, second_days = split_days(p.day for p in pairs)
    if not second_days:
        return (pairs, ())
    return (
        tuple(p for p in pairs if p.day in first_days),
        tuple(p for p in pairs if p.day in second_days),
    )


def _report_period_split(pairs: tuple[GapFadePair, ...]) -> None:
    """前半・後半で効果が安定しているかを見る。

    **判定基準（結果を見る前に固定した）**:

    - **両期間とも net が正で、かつ閾値との単調性が両方で保たれる**
      → 期間に依存しない効果。次の検証に進む価値がある
    - **片方の期間だけが正、または符号が入れ替わる**
      → 特定の局面を切り取っただけ。棄却

    竹の `--stress-test` では前半66%・後半87%と両方が正だったが、
    それでも全期間の閾値（99%）に届かず棄却した（意思決定ログ46）。
    **期間分割は「通れば合格」ではなく「落とすための試験」。**
    """
    if not pairs:
        return
    first, second = split_by_period(pairs)
    if not first or not second:
        print()
        print("  【期間分割】営業日が足りず判定不能")
        return

    first_days = sorted({p.day for p in first})
    second_days = sorted({p.day for p in second})
    print()
    print("  【期間分割】前半・後半で効果が安定しているか")
    print(f"  前半 {first_days[0]}〜{first_days[-1]}（{len(first_days)}営業日）")
    print(f"  後半 {second_days[0]}〜{second_days[-1]}（{len(second_days)}営業日）")
    print()
    print(
        f"  {'|ギャップ|下限':<12} {'前半 件数':>9} {'net':>9} {'t値(日)':>8}   "
        f"{'後半 件数':>9} {'net':>9} {'t値(日)':>8}"
    )
    print("  " + "-" * 76)
    for threshold in GAP_BUCKETS_PCT:
        cells: list[str] = []
        for subset in (first, second):
            stats = bucket_stats(subset, threshold)
            clustered = clustered_stats(subset, threshold)
            if stats is None:
                cells.append(f"{'—':>9} {'—':>9} {'—':>8}")
                continue
            t_txt = f"{clustered.t_stat:>7.1f}" if clustered else f"{'—':>8}"
            cells.append(f"{stats.n:>9} {stats.net_bps:>+8.2f}b {t_txt}")
        print(f"  {threshold:>10.1%}  {cells[0]}   {cells[1]}")
    print()
    print("  **片方の期間だけが正、または符号が入れ替わるなら棄却。**")
    print("  期間分割は「通れば合格」ではなく**落とすための試験**（意思決定ログ46）。")


def _report_drift_adjusted(pairs: tuple[GapFadePair, ...]) -> None:
    """買建の優位が銘柄固有なのか、相場の上昇ドリフトなのかを切り分ける。

    **疑う理由**: 方向別に見ると買建だけが閾値とともに単調に伸びた。
    だが `fade_score` はギャップダウン日には `始値→終値` そのものなので、
    **日中に平均して上昇するドリフトがあれば買建だけが自動的に得をする**。
    検証期間（2年）が上昇局面なら、フェードではなく相場を測っただけになる。

    **判定基準（結果を見る前に固定した）**:

    - 控除しても買建の優位がおおむね残り、閾値との単調性も保たれる
      → 銘柄固有の効果。ギャップは本物の手がかり
    - 控除で買建が売建の水準まで落ちる
      → 非対称の正体は相場ドリフト。「ギャップ・フェード」という
        枠組み自体が誤りで、上昇局面を測っていただけ
    """
    if not pairs:
        return
    adjusted = demean_by_day(pairs)
    print()
    print("  【市場ドリフトの控除】その日の全銘柄の平均 始値→終値 を差し引く")
    print(f"  全ペアの平均 始値→終値: {market_drift_bps(pairs):+.2f}bps")
    print("  正なら、ギャップダウンをフェードする側（買建）だけが自動的に得をする。")
    print()
    print(
        f"  {'|ギャップ|下限':<12} {'買建(素)':>10} {'買建(控除)':>11} "
        f"{'売建(素)':>10} {'売建(控除)':>11}"
    )
    print("  " + "-" * 62)
    for threshold in GAP_BUCKETS_PCT:
        cells: list[str] = []
        for subset in (pairs, adjusted):
            for sign in (-1, 1):
                side = tuple(p for p in subset if p.gap_pct * sign > 0)
                stats = bucket_stats(side, threshold)
                cells.append(f"{stats.net_bps:>+9.2f}b" if stats else f"{'—':>10}")
        # cells は [買建(素), 売建(素), 買建(控除), 売建(控除)] の順
        print(f"  {threshold:>10.1%}  {cells[0]} {cells[2]}  {cells[1]} {cells[3]}")
    print()
    print("  **これは事後診断であって、実戦の変種ではない。** その日の市場平均は")
    print("  引けまで分からないので、この控除を実際の売買では行えない。")
    print("  「なぜ効いたか」の説明に使うだけで、エントリー判断には使えない。")


def _report_capacity(pairs: tuple[GapFadePair, ...]) -> None:
    """資金額 → 実際に建てられる量 → 月利、に変換する。

    **これが資金判断に必要だった数字。** これまでの表は bps（1回の
    取引の取り分）までしか出しておらず、「1日に何回建てられるか」
    「資金のうち何割が建玉になるか」が抜けていた。月利はその積で決まる。
    """
    if not pairs:
        return
    print()
    print(f"  【資金 → 月利】ギャップ下限 {CAPACITY_THRESHOLD_PCT:.1%}・"
          f"同時{MAX_CONCURRENT}銘柄・1銘柄{MAX_POSITION_PCT:.0%}上限・レバ1倍")
    print()
    print(
        f"  **この {CAPACITY_THRESHOLD_PCT:.1%} という閾値は net bps が最大だったから"
        "選んだもので、"
    )
    print("  結果を見てから選んだ基準**（意思決定ログ75）。日クラスタの t値では")
    print("  最も弱いバケットでもある。**下の頑健性の格子と併せて読むこと。**")
    print()
    print(
        f"  {'資金':>10} {'銘柄':>7} {'枠/日':>6} {'建玉率':>7} "
        f"{'月利':>8} {'年利':>8} {'Sharpe':>7} {'最悪日':>7} {'BRK':>5} {'最大DD':>8}"
    )
    print("  " + "-" * 86)
    for capital in CAPITAL_CANDIDATES_YEN:
        stats = capacity_stats(pairs, capital)
        if stats is None:
            continue
        print(
            f"  {capital:>9,}円 {stats.symbols_used:>6} "
            f"{stats.mean_slots_filled:>6.2f} {stats.mean_deployed_pct:>6.1f}% "
            f"{stats.monthly_return_pct:>+7.2f}% {stats.annual_return_pct:>+7.1f}% "
            f"{stats.sharpe:>7.2f} {stats.worst_day_pct:>+6.2f}% "
            f"{stats.breaker_days:>5} {stats.max_drawdown_pct:>+7.1f}%"
        )

    longs = tuple(p for p in pairs if p.gap_pct < 0)
    all_days = frozenset(p.day for p in pairs)
    print()
    print("  【買建のみ】ギャップダウンのフェードだけを建てる")
    print("  **安全装置#3（ショートのストップ必須）と売建可能銘柄リストを回避できる。**")
    print("  そのぶんシグナルが半減するので、枠が埋まらず建玉率が落ちる。")
    print()
    print(
        f"  {'資金':>10} {'銘柄':>7} {'枠/日':>6} {'建玉率':>7} "
        f"{'月利':>8} {'年利':>8} {'Sharpe':>7} {'最悪日':>7} {'BRK':>5} {'最大DD':>8}"
    )
    print("  " + "-" * 86)
    for capital in CAPITAL_CANDIDATES_YEN:
        stats = capacity_stats(longs, capital, universe_days=all_days)
        if stats is None:
            continue
        print(
            f"  {capital:>9,}円 {stats.symbols_used:>6} "
            f"{stats.mean_slots_filled:>6.2f} {stats.mean_deployed_pct:>6.1f}% "
            f"{stats.monthly_return_pct:>+7.2f}% {stats.annual_return_pct:>+7.1f}% "
            f"{stats.sharpe:>7.2f} {stats.worst_day_pct:>+6.2f}% "
            f"{stats.breaker_days:>5} {stats.max_drawdown_pct:>+7.1f}%"
        )
    print()
    print("  BRK = 日次ブレーカー(-2%・安全装置#4)に達した日数。**シミュレーションは")
    print("  ブレーカーを適用していない**ので、これは「動かしていたら何日で強制停止")
    print("  していたか」。多いほど**戦略が安全装置の設計前提と衝突している**。")
    print()
    print("  **目標は年利25〜35%**（意思決定ログ73）。ただし合格の門はリターンではなく")
    print("  **シャープレシオ**（`docs/07` §2・意思決定ログ9）——リターンだけを追うと、")
    print("  たまたま勝った高リスク手法を選んでしまう。")
    print()
    print("  **これは上限見積りであって、バックテストではない。**")
    print("  損切り・ブレーカー三層・5分足の約定モデル・売建の在庫確認を")
    print("  どれも含んでおらず、すべて成績を良い側に倒す単純化になっている。")
    print("  **ここで目標に届かないなら、実装すれば必ず下回る。**")

    _report_threshold_robustness(pairs)


def _capacity_grid(
    pairs: tuple[GapFadePair, ...],
    label: str,
    universe_days: frozenset[date] | None = None,
) -> None:
    """閾値 × 資金 の格子で、年利とシャープを並べる。

    **閾値を1つに固定しない理由。** 建玉シミュレーションは当初
    2.0%固定で測っていたが、それは「net bps が最大のバケット」という
    **結果を見てから選んだ基準**だった。日クラスタの t値を出したところ
    2.0%は t=0.6（ゼロと区別できない）で、最も強いのは 1.0%（t=3.3）だった
    （意思決定ログ75）。**閾値の選び方で結論が変わるなら、その結論は
    採用できない**ので、全バケットを並べて頑健性を見る。
    """
    print()
    print(f"  【{label}】年利 / シャープレシオ")
    print()
    header = "  ".join(f"{t:>13.1%}" for t in GAP_BUCKETS_PCT)
    print(f"  {'資金':>10}  {header}")
    print("  " + "-" * (12 + 15 * len(GAP_BUCKETS_PCT)))
    for capital in CAPITAL_CANDIDATES_YEN:
        cells: list[str] = []
        for threshold in GAP_BUCKETS_PCT:
            stats = capacity_stats(
                pairs, capital, threshold=threshold, universe_days=universe_days
            )
            cells.append(
                f"{stats.annual_return_pct:>+7.1f}%/{stats.sharpe:>4.1f}"
                if stats
                else f"{'—':>13}"
            )
        print(f"  {capital:>9,}円  " + "  ".join(cells))


def _report_threshold_robustness(pairs: tuple[GapFadePair, ...]) -> None:
    """閾値の選び方と期間の切り方に、結論が耐えるかを見る。

    **これは新しい探索ではなく、既に出した数字の頑健性チェック。**
    新しい変種を足していないので多重比較の分母は増えない
    （同じ1つの仮説を、選択の自由度を変えて測り直しているだけ）。
    """
    if not pairs:
        return
    all_days = frozenset(p.day for p in pairs)
    _capacity_grid(pairs, "全期間", universe_days=all_days)

    first, second = split_by_period(pairs)
    if first and second:
        _capacity_grid(first, "前半", universe_days=frozenset(p.day for p in first))
        _capacity_grid(second, "後半", universe_days=frozenset(p.day for p in second))

    print()
    print("  **見るのは「後半」の行。** 前半は事実上 in-sample——閾値も資金も")
    print("  全期間の数字を見てから決めている。後半で目標（年利25〜35%）に")
    print("  届かないなら、届いていたのは過去の一区間だけだったということ。")
    print()
    print("  **閾値によって結論が変わるなら、その結論は採用しない。**")
    print("  2.0%固定で測っていたのは net bps が最大だったからで、")
    print("  それは結果を見てから選んだ基準だった（意思決定ログ75）。")


def _report_spread_sensitivity(pairs: tuple[GapFadePair, ...]) -> None:
    """スプレッド想定を変えたときに net の符号が変わるかを見る。

    **スプレッドは Stage A では実測できない**（意思決定ログ57〜60で
    Corwin-Schultz を試したが、この銘柄群では推定がノイズ床に埋もれた）。
    だとしても「想定を変えたら結論が変わるのか」は測れる。
    **変わらないなら、スプレッドの精密化に投資する意味はない。**
    """
    print()
    print("  【スプレッド想定に対する感度】")
    print("  **gross はコストモデルに依存しない**ので、この表は確実。")
    print()
    candidates = (2.0, 1.5, 1.0, 0.5)
    header = "  ".join(f"{t:.1f}本" for t in candidates)
    print(f"  {'|ギャップ|下限':<12} {'gross':>8}   net: {header}")
    print("  " + "-" * 62)
    for threshold in GAP_BUCKETS_PCT:
        base = bucket_stats(pairs, threshold)
        if base is None:
            continue
        nets = "  ".join(
            f"{s.net_bps:>+7.1f}b" if (s := bucket_stats(pairs, threshold, t)) else "    —"
            for t in candidates
        )
        print(f"  {threshold:>10.1%}  {base.gross_bps:>+7.2f}b        {nets}")
    print()
    print("  **どの想定でも net が負なら、スプレッドの精密化は不要。**")
    print("  優位そのものが足りていないので、コストではなく手法側の問題。")


def load_topix100(*, historical: bool) -> tuple[tuple[Symbol, ...], str]:
    """TOPIX100 構成銘柄。`scripts/measure_topix100.py` が作るキャッシュを読む。

    Args:
        historical: ``True`` なら**検証期間の開始時点**の一覧を使う。

            **既定でこちらを使うべき。** 現在の一覧は「2年間 大型で
            居続けた勝ち組」なので、それで過去を測ると成績が構造的に
            過大評価される（`docs/03-universe.md` §4.2 が明示的に禁じている）。
            `False` は「今の一覧で測るとどれだけ甘くなるか」を
            比較するためだけに使う。

    Returns:
        ``(TOPIX100 の銘柄, 基準日の説明)``。
    """
    import json

    name = "master_scale_historical.json" if historical else "master_scale.json"
    path = DATA_ROOT / name
    if not path.is_file():
        raise SystemExit(
            f"{path} がない。先に python scripts/measure_topix100.py --refresh を実行する"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    symbols = tuple(
        Symbol(
            code=r["code"],
            name=r["name"],
            market=r.get("market"),
            margin_type=r.get("margin_type"),
            sector=r.get("sector"),
            scale_category=r.get("scale_category"),
        )
        for r in payload["symbols"]
    )
    as_of = payload.get("as_of", "不明")
    return tuple(s for s in symbols if s.is_topix100), as_of


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--topix100",
        action="store_true",
        help=(
            "TOPIX100 構成銘柄だけで測る（呼値0.1〜0.5円）。"
            "**大型株でも優位が残るか**を、資金の判断より先に確かめる"
        ),
    )
    parser.add_argument(
        "--survivorship",
        action="store_true",
        help=(
            "**現在**の TOPIX100 一覧で過去を測る（既定は検証期間の開始時点）。"
            "サバイバーシップバイアスがどれだけ効くかの比較用。"
            "**この結果を採用してはならない**（docs/03 §4.2）"
        ),
    )
    args = parser.parse_args()

    print("ギャップ・フェード診断（日足のみ・安価な予備検証）")
    print(banner())

    store = BarStore(DATA_ROOT)
    if args.topix100:
        symbols, as_of = load_topix100(historical=not args.survivorship)
        topix100_codes = frozenset(s.code for s in symbols)
        print(f"  **TOPIX100 のみ**（呼値0.1〜0.5円）: {len(symbols)}銘柄")
        if args.survivorship:
            print(f"  **現在の一覧（{as_of}）で過去を測っている。**")
            print("  → サバイバーシップバイアスあり。**採用してはならない**")
        else:
            print(f"  構成銘柄は検証期間の開始時点（{as_of}）のものを使う")
            print("  → 今の勝ち組で過去を測らない（docs/03 §4.2）")
    else:
        symbols = load_symbols()
        topix100_codes = frozenset()

    daily = {s.code: store.read(s.code, "1d") for s in symbols}
    daily = {c: b for c, b in daily.items() if b}
    print(f"  日足あり: {len(daily)}銘柄")

    # **gap_pct は日をまたぐ値**（前日終値 → 当日始値）なので、
    # 価格水準の継ぎ目があるとそこだけ嘘になる（意思決定ログ97）
    hr("0. データの健全性（集計より先に確かめる）")
    daily = drop_discontinuous_symbols(daily)
    if not daily:
        print("  健全な日足が残らなかった。日足を取り直す")
        return 1

    pairs = gap_fade_pairs(daily, topix100_codes)
    hr("結果")
    report(pairs)

    if args.topix100:
        _report_tick_decomposition(pairs)

    hr("資金はいくら要るのか")
    _report_capacity(pairs)
    if args.topix100:
        print()
        print("  資金50万円では TOPIX100 99銘柄中92銘柄が株価上限を超える")
        print("  （意思決定ログ64）。上の表の「使えた銘柄」がそれを反映する。")
    return 0


def _report_tick_decomposition(pairs: tuple[GapFadePair, ...]) -> None:
    """コストが下がった理由を「細かい呼値」と「株価が高い」に分解する。

    **この2つは別物。** TOPIX100 の呼値が細かいのは事実だが、
    TOPIX100 は株価も高い。呼値が絶対額である以上、**株価が高いだけでも
    bps 換算のコストは下がる**（3,000円の通常銘柄なら呼値1円で6.7bps、
    600円なら33bps）。

    どちらが効いているかを分けないと、「TOPIX100 だから安い」と
    誤解して、通常銘柄の高株価帯という選択肢を見落とす。
    """
    if not pairs:
        return
    # 同じ銘柄・同じ日を、通常銘柄の呼値で評価し直す
    as_regular = tuple(
        GapFadePair(
            symbol=p.symbol,
            day=p.day,
            gap_pct=p.gap_pct,
            intraday_return_pct=p.intraday_return_pct,
            open_price=p.open_price,
            topix100=False,
        )
        for p in pairs
    )
    print()
    print("  【コスト低下の内訳: 呼値が細かい / 株価が高い】")
    print("  同じ銘柄・同じ日を、通常銘柄の呼値で評価し直す。")
    print()
    print(
        f"  {'|ギャップ|下限':<12} {'gross':>9} "
        f"{'TOPIX100呼値':>13} {'通常呼値':>11} {'net(通常)':>11}"
    )
    print("  " + "-" * 60)
    for threshold in GAP_BUCKETS_PCT:
        fine = bucket_stats(pairs, threshold)
        plain = bucket_stats(as_regular, threshold)
        if fine is None or plain is None:
            continue
        print(
            f"  {threshold:>10.1%}  {fine.gross_bps:>+8.2f}b "
            f"{fine.cost_bps:>12.2f}b {plain.cost_bps:>10.2f}b "
            f"{plain.net_bps:>+10.2f}b"
        )
    print()
    print("  **右端が正なら、細かい呼値がなくても成立する。**")
    print("  その場合の本質は「株価の高い銘柄を扱えるか」＝資金の問題。")


if __name__ == "__main__":
    sys.exit(main())
