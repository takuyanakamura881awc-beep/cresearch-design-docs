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

import math
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

from autotrader.data.store import BarStore
from autotrader.provenance import banner
from autotrader.tick import spread_yen
from autotrader.types import Bar, Symbol

DATA_ROOT = Path("data")

GAP_BUCKETS_PCT: tuple[float, ...] = (0.005, 0.010, 0.015, 0.020)
"""ギャップの下限バケット。VWAP乖離のスイープ（0.7/1.0/1.5/2.0%）と同じ刻み方。"""


@dataclass(frozen=True)
class GapFadePair:
    """1銘柄・1営業日ぶんのギャップとその日の値動き。"""

    symbol: str
    gap_pct: float
    """(当日始値 - 前日終値) / 前日終値。"""
    intraday_return_pct: float
    """(当日終値 - 当日始値) / 当日始値。"""
    open_price: float
    """当日始値。**コストは株価で決まる**ので必要（`autotrader.tick`）。

    ギャップ・フェード戦略として実装するなら始値付近で建てることになるので、
    往復コストの見積りもこの価格を基準にする。
    """


def gap_fade_pairs(daily_bars: dict[str, tuple[Bar, ...]]) -> tuple[GapFadePair, ...]:
    """銘柄ごとの日足から、ギャップと当日の値動きのペアを作る。

    **銘柄の初日（前日終値がない）は除外する。** 始値・前日終値が0以下の
    日も除外する（0除算対策。`autotrader.regime.daily_range_pct` と同じ規律）。
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
                    gap_pct=gap_pct,
                    intraday_return_pct=intraday_return_pct,
                    open_price=today.open,
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


def round_trip_cost_bps(pair: GapFadePair) -> float:
    """この銘柄・この日に建てたときの往復コスト（bps）。

    `autotrader.tick.spread_yen` をそのまま使う——**約定コストの
    モデルを診断ごとに作り直さない**（`docs/00` 意思決定ログ33以降で
    呼値ベースに統一済み）。往復でスプレッド1本ぶんを払う。
    """
    return float(spread_yen(pair.open_price)) / pair.open_price * 10_000.0


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


def bucket_stats(pairs: tuple[GapFadePair, ...], threshold: float) -> BucketStats | None:
    """``|gap_pct| >= threshold`` の日だけを集計する。

    Returns:
        該当が2件未満なら ``None``（標準偏差が計算できない）。
    """
    bucket = [p for p in pairs if abs(p.gap_pct) >= threshold]
    if len(bucket) < 2:
        return None

    scores_bps = [fade_score(p) * 10_000.0 for p in bucket]
    costs_bps = [round_trip_cost_bps(p) for p in bucket]
    mean = statistics.fmean(scores_bps)
    stderr = statistics.stdev(scores_bps) / math.sqrt(len(scores_bps))
    return BucketStats(
        n=len(bucket),
        gross_bps=mean,
        cost_bps=statistics.fmean(costs_bps),
        stderr_bps=stderr,
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
        f"{'net':>9} {'t値':>7}"
    )
    print("  " + "-" * 60)
    for threshold in GAP_BUCKETS_PCT:
        stats = bucket_stats(pairs, threshold)
        if stats is None:
            print(f"  {threshold:>10.1%}  {0:>7}  —（該当なし）")
            continue
        print(
            f"  {threshold:>10.1%}  {stats.n:>7} "
            f"{stats.gross_bps:>+8.2f}b {stats.cost_bps:>8.2f}b "
            f"{stats.net_bps:>+8.2f}b {stats.t_stat:>6.1f}"
        )

    print()
    print("  **gross はコスト前、net はコストを引いた後。** t値は gross が")
    print("  ゼロと区別できるかで、**コスト後に残るかとは別の話**。")
    print("  net が負なら、傾向が統計的に本物でも取引する意味はない。")
    print()
    print("  往復コストは呼値2tick想定（`autotrader.tick`）。始値で建てて")
    print("  引けで手仕舞う前提の、最も楽観的な見積り——実際は寄り付き直後の")
    print("  スプレッドはこれより広い。**net が僅差で正でも安心できない。**")


def main() -> int:
    print("ギャップ・フェード診断（日足のみ・安価な予備検証）")
    print(banner())

    symbols = load_symbols()
    store = BarStore(DATA_ROOT)
    daily = {s.code: store.read(s.code, "1d") for s in symbols}
    daily = {c: b for c, b in daily.items() if b}
    print(f"  日足あり: {len(daily)}銘柄")

    pairs = gap_fade_pairs(daily)
    hr("結果")
    report(pairs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
