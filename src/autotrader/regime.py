"""日次の値動きの荒さを、事後的に分類する。**実戦の判断には使わない。**

【なぜ要るのか】

ランダム検定で「竹はランダムと区別できない」ことが確定した中、
VWAP乖離（逆張り）単独だけが、事前登録した合格基準には僅差で届かなかった
ものの、前半・後半どちらの期間でもランダムより上という、他の棄却変種には
ない特徴を持っていた（`docs/00-overview.md` 意思決定ログ46）。

ユーザーから「相場には局面があり、1つの手法が全期間に適するとは限らない」
という指摘があった。この発想自体は妥当だが、**過去10年分のような
大規模な局面判定システムをいきなり作るのは時期尚早**と判断した
（意思決定ログ47）:

- J-Quants 無料プランは日足でも過去2年、5分足は yfinance で過去58日までで、
  10年分のデータが物理的に手に入らない
- 局面×手法の組合せは検定すべき数を掛け算的に増やし、5変種の比較ですら
  99%補正が要った現状のデータ量では検証しきれない
- どの局面でも通用する土台がまだ1つもない段階で使い分けを作るのは早い

そこで、**外部データを増やさず、手元の5分足だけで**「VWAP乖離の成績が
値動きの荒さで偏っているか」を診断する縮小版だけをここに置く。

【事後診断であって、実戦のフィルタではない】

``daily_range_pct`` は**その日の高値・安値**を使う。取引判断の時点では
その日の高値・安値はまだ確定していないので、**これは取引の可否を
決めるのに使ってはならない**（先読みになる）。

ここでの目的は「VWAP乖離の勝ちが、結果的に穏やかだった日に偏っているか」
という事後的な診断だけ。偏りが確認できて初めて、「前日までの情報だけで
その日の傾向を予測する」という、``PointInTimeView`` の規律に従った
先読み版フィルタの設計に進む（`docs/00-overview.md` 参照）。
2段階に分ける理由は、事後診断と先読み判定を混同すると、
``engine/backtest.py`` の ``PointInTimeView`` が防いでいる
ルックアヘッドバイアスを、この診断の中で作り込んでしまうため。

【閾値を固定しない】

「穏やか」「荒れ」の境界は固定値で持たず、**対象日に値動きが観測できた
銘柄群の中央値**で二分する。固定閾値を新しいチューニング対象にすると、
「境界をどこに置けば良い結果が出るか」を後から探る余地ができ、
これまで避けてきた過学習の入口になる。
"""

from __future__ import annotations

import statistics
from datetime import date
from typing import Literal

from autotrader.types import Bar

RegimeLabel = Literal["calm", "wild"]

__all__ = ["RegimeLabel", "classify_days", "daily_range_pct"]


def daily_range_pct(bars: tuple[Bar, ...]) -> float | None:
    """その日1銘柄ぶんの実現レンジ（高値−安値）÷ 終値。

    ``universe/selector.py`` の ``prev_range_pct`` と同じ計算式。
    **新しい指標を発明しない** — 既存の式を日中バーに適用するだけにして、
    過学習の余地を増やさない。

    Args:
        bars: その日・その銘柄ぶんの5分足（時刻の昇順）。

    Returns:
        バーが1本もない、または終値が0以下なら ``None``。
    """
    if not bars:
        return None
    close = bars[-1].close
    if close <= 0:
        return None
    high = max(b.high for b in bars)
    low = min(b.low for b in bars)
    return (high - low) / close


def classify_days(
    bars_by_symbol: dict[str, tuple[Bar, ...]], day: date
) -> dict[str, RegimeLabel]:
    """指定した1営業日について、銘柄ごとに calm/wild を割り当てる。

    **その日に値動きが観測できた銘柄群の中央値**を境界にする。
    中央値以上なら wild、未満なら calm。

    Args:
        bars_by_symbol: 銘柄コード → バー列（複数日ぶんでよい。
            この関数が ``day`` に該当する分だけを取り出す）。
        day: 分類する営業日。

    Returns:
        銘柄コード → ``"calm"`` または ``"wild"``。
        その日にバーがない、または実現レンジを計算できない銘柄は含まない。

    Note:
        **事後診断専用。** 当日の高値・安値を使うため、取引前には
        計算できない（`daily_range_pct` の docstring 参照）。
    """
    ranges: dict[str, float] = {}
    for symbol, series in bars_by_symbol.items():
        today = tuple(b for b in series if b.timestamp.date() == day)
        value = daily_range_pct(today)
        if value is not None:
            ranges[symbol] = value

    if not ranges:
        return {}

    threshold = statistics.median(ranges.values())
    return {
        symbol: "wild" if value >= threshold else "calm"
        for symbol, value in ranges.items()
    }
