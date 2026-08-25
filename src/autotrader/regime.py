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

【最初の実装が踏んだ落とし穴（意思決定ログ48）】

最初は「その日の銘柄群を横断した中央値」を境界に、**銘柄ごと**に
calm/wild を割り当てていた。実データで走らせたところ、221件中220件が
wild、calm はわずか1件という結果になった。

**これは分類基準がエントリー条件とほぼ同義になっていたための
見かけ上の結果だった。** VWAP乖離のエントリー条件は「その日その銘柄が
VWAP から1.5%以上乖離する」——これは「その銘柄がその日、他の銘柄より
大きく動いた」とほぼ同じことを言っている。一方、旧「wild」の定義も
「その日の値幅が仲間内の中央値を上回る銘柄」だった。**同じものを
測っていたので、トレードのほぼ全部が機械的に wild に分類されるのは
当然だった。** これでは「相場の局面（数か月単位で良し悪しが入れ替わる）
によって成績が変わるか」という元の問いには何も答えられない。

**そこで、分類の単位を「銘柄ごと」から「日ごと」に作り替えた。**
対象日ごとに、その日値動きを観測できた全銘柄の実現レンジの中央値を
「その日1つぶんの市場全体の荒さ」とし、日をまたいだ中央値で
calm/wild を二分する。個別銘柄が仲間より動いたかではなく、
その日全体として市場が荒れていたかを測るので、個別銘柄のエントリー
条件とは独立した軸になる。

【事後診断であって、実戦のフィルタではない】

``daily_range_pct`` は**その日の高値・安値**を使う。取引判断の時点では
その日の高値・安値はまだ確定していないので、**これは取引の可否を
決めるのに使ってはならない**（先読みになる）。

ここでの目的は「VWAP乖離の勝ちが、結果的に市場全体が穏やかだった日に
偏っているか」という事後的な診断だけ。偏りが確認できて初めて、
「前日までの情報だけでその日の傾向を予測する」という、
``PointInTimeView`` の規律に従った先読み版フィルタの設計に進む
（`docs/00-overview.md` 参照）。2段階に分ける理由は、事後診断と
先読み判定を混同すると、``engine/backtest.py`` の ``PointInTimeView``
が防いでいるルックアヘッドバイアスを、この診断の中で作り込んでしまうため。

【閾値を固定しない】

「穏やか」「荒れ」の境界は固定値で持たず、**日をまたいだ中央値**で
二分する。固定閾値を新しいチューニング対象にすると、「境界をどこに
置けば良い結果が出るか」を後から探る余地ができ、これまで避けてきた
過学習の入口になる。
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
    bars_by_symbol: dict[str, tuple[Bar, ...]], days: tuple[date, ...]
) -> dict[date, RegimeLabel]:
    """指定した営業日群それぞれについて、市場全体が calm/wild かを判定する。

    **銘柄ごとではなく、日ごとに1つのラベルを付ける。** 各日の値は、
    その日値動きを観測できた全銘柄の ``daily_range_pct`` の中央値
    （＝その日1つぶんの「市場全体の荒さ」）。そのうえで、**日をまたいだ
    中央値**を境界に calm/wild を二分する。

    銘柄単位で分類すると、VWAP乖離のようなエントリー条件（その日その
    銘柄が仲間より大きく動いたか）とほぼ同じものを測ってしまい、
    トレードのほぼ全部が機械的に同じラベルに集まる（意思決定ログ48）。
    日単位にすることで、個別銘柄のエントリー条件とは独立した
    「その日全体として市場が荒れていたか」という軸になる。

    Args:
        bars_by_symbol: 銘柄コード → バー列（複数日ぶんでよい。
            この関数が対象日に該当する分だけを取り出す）。
        days: 分類する営業日群。

    Returns:
        営業日 → ``"calm"`` または ``"wild"``。値動きを観測できた
        銘柄が1つもない日は含まない。

    Note:
        **事後診断専用。** 当日の高値・安値を使うため、取引前には
        計算できない（`daily_range_pct` の docstring 参照）。
    """
    market_range_by_day: dict[date, float] = {}
    for day in days:
        symbol_ranges: list[float] = []
        for series in bars_by_symbol.values():
            today = tuple(b for b in series if b.timestamp.date() == day)
            value = daily_range_pct(today)
            if value is not None:
                symbol_ranges.append(value)
        if symbol_ranges:
            market_range_by_day[day] = statistics.median(symbol_ranges)

    if not market_range_by_day:
        return {}

    threshold = statistics.median(market_range_by_day.values())
    return {
        day: "wild" if value >= threshold else "calm"
        for day, value in market_range_by_day.items()
    }
