"""海外指数・為替など、日本株以外の日次系列。**寄り付き前に確定している情報。**

【なぜこれが要るのか】

7家族すべてが「優位 0〜5bps < コスト 5〜13bps」で死んだ（意思決定ログ88・101）。
手がかりはすべて**日本株の価格そのもの**から作っていた。

**外部指数には、これまでの手がかりに無い性質が1つある——確定時刻。**

米国市場の引けは 16:00 ET ＝ **翌 05:00 JST**。東京の寄り付き 9:00 より
4時間前に確定している。つまり `docs/04-strategies.md` の執行可能性ゲート
質問1（シグナルは何時に確定するか）を**構造的に通る**——ギャップ・フェードが
死んだ「始値が要るので板寄せに参加できない」という循環が起きない。

【だが最大の反論を先に潰すこと】

**日経平均先物は CME・SGX・大阪の夜間セッションでほぼ24時間動いている。**
東京が寄り付く時点で、夜間の海外の値動きは**既に始値に織り込まれている**。
だから「ギャップ」が存在する。

「米国が下げた → 翌日の日本も下げる」は**予測ではなく、始値がもう下がって
いるだけ**。`scripts/measure_global_lead.py` のセクション1が、まずこれを
確かめる。**織り込み率を測ってからでないと、残りを解釈できない。**

【ルックアヘッドはここが一番危ない】

外部系列の日付は**その市場のカレンダー**で付く。日本の日付と素朴に
突き合わせると、**まだ起きていない海外の終値を使ってしまう**。

    米国 date D の終値 = 翌 05:00 JST      → 日本 date D+1 の寄り付きに間に合う
    米国 date T の終値 = 翌 05:00 JST      → 日本 date T の大引けより**後**

`align_prior_session` は **``外部日付 < 日本の営業日``** を厳格に要求する。
同日を許すと5分足のラベル解釈で踏んだのと同じ形の先読みになる
（意思決定ログ81）。**「注意して書く」では防げないので関数にした**（規約7）。

【出来高が無い系列がある】

指数（``^VIX`` ``^GSPC``）と為替（``JPY=X``）は出来高を持たない。
`yahoo.py` の `_parse` は OHLCV すべてに `pd.notna()` を要求するので、
**そのまま通すと全行が欠損扱いで捨てられ、エラーなく空になる**——
列構造の仮定で実際に踏んだのと同じ壊れ方（`docs/09-data-sources.md` §2.5）。
ここでは出来高を**任意**として扱い、無ければ0を入れる。

**ただし価格4本値の欠損は捨てる。** そこは緩めない。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from autotrader.data.base import DataSourceError, EmptyResponseError
from autotrader.data.yahoo import _init_tz_cache, _select_ticker, _to_datetime
from autotrader.types import Bar

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SERIES",
    "TRAILING_WINDOW",
    "ExternalSeries",
    "align_prior_session",
    "fetch_external",
    "log_returns",
    "trailing_rank",
]


@dataclass(frozen=True)
class ExternalSeries:
    """取得する外部系列1本。

    ``key`` は保存時のファイル名にも使うので、``^`` や ``=`` を含まない
    英数字だけにしてある（Windows でも安全）。
    """

    key: str
    ticker: str
    """Yahoo のティッカー。**``.T`` を付けない**（`yahoo.to_ticker` を通さない）。"""
    note: str


DEFAULT_SERIES: tuple[ExternalSeries, ...] = (
    ExternalSeries("VIX", "^VIX", "恐怖指数。流動性供給の対価の代理（Nagel 2012）"),
    ExternalSeries("SP500", "^GSPC", "S&P500。夜間の米国株の方向"),
    ExternalSeries("NASDAQ", "^IXIC", "ナスダック総合。ハイテク寄りの夜間入力"),
    ExternalSeries("USDJPY", "JPY=X", "ドル円。日本の輸出セクターに直結する"),
    ExternalSeries("US10Y", "^TNX", "米10年金利。グロース/バリューの綱引き"),
)
"""既定で取る系列。**増やすと多重比較の分母が増える。**

5本に絞ってある。**どれも「夜間に確定して寄り付き前に読める」ものだけ**で、
日本の取引時間中に動くもの（日経先物の日中セッション等）は入れない
——それを入れると確定時刻の議論が崩れる。
"""


def fetch_external(
    series: Sequence[ExternalSeries],
    start: date,
    end: date,
) -> dict[str, tuple[Bar, ...]]:
    """外部系列の日足を取る。**空なら例外にする**（規約6）。

    `yahoo.py` の落とし穴対策（tzキャッシュの隔離・MultiIndex の解決・
    `pd.notna()` での欠損判定）は**そちらから import して使い回す**。
    同じ対策を二度書かない（規約「同じことをする関数を二つ作らない」）。

    Raises:
        EmptyResponseError: 1本も取れなかった場合（ブロックの可能性）。
        DataSourceError: yfinance の呼び出しが失敗した場合。
    """
    if not series:
        return {}

    import pandas as pd
    import yfinance as yf

    _init_tz_cache()
    tickers = [s.ticker for s in series]
    try:
        frame = yf.download(
            tickers,
            interval="1d",
            start=start.isoformat(),
            end=end.isoformat(),
            auto_adjust=True,
            progress=False,
            group_by="ticker",
            threads=False,
        )
    except Exception as exc:  # noqa: BLE001 - 非公式APIは何を投げるか不定
        raise DataSourceError(f"yfinance の外部系列取得に失敗した: {exc}") from exc

    if frame is None or frame.empty:
        raise EmptyResponseError(
            f"yfinance が外部系列 {','.join(tickers)} について空の応答を返した。"
            "レート制限またはティッカー変更の可能性"
        )

    out: dict[str, tuple[Bar, ...]] = {}
    for spec in series:
        sub = _select_ticker(frame, spec.ticker)
        if sub is None or sub.empty:
            logger.warning("外部系列が取れなかった: %s (%s)", spec.key, spec.ticker)
            continue
        bars = tuple(_to_bars(spec, sub, pd))
        if bars:
            out[spec.key] = bars

    if not out:
        raise EmptyResponseError(
            f"外部系列 {','.join(tickers)} のどれも解釈できなかった。"
            "ティッカー変更か列構造の変更を疑う"
        )
    missing = [s.key for s in series if s.key not in out]
    if missing:
        # **黙って縮めない。** 欠けた系列があることを呼び出し側が知る必要がある
        logger.warning("取得できなかった外部系列: %s", ",".join(missing))
    return out


def _to_bars(spec: ExternalSeries, sub: Any, pd: Any) -> Iterable[Bar]:
    """1系列ぶんの DataFrame を ``Bar`` 列にする。

    **出来高は任意**（指数・為替は持たない）。欠けていれば0を入れる。
    **価格4本値の欠損は捨てる**——そこは緩めない。
    """
    for ts, row in sub.iterrows():
        prices = [row.get(c) for c in ("Open", "High", "Low", "Close")]
        if not all(pd.notna(v) for v in prices):
            continue
        o, h, low, c = (float(v) for v in prices)
        raw_volume = row.get("Volume")
        volume = int(raw_volume) if pd.notna(raw_volume) else 0
        yield Bar(
            symbol=spec.key,
            timestamp=_to_datetime(ts),
            open=o,
            high=h,
            low=low,
            close=c,
            volume=volume,
        )


def align_prior_session(
    external_days: Iterable[date], jp_days: Iterable[date]
) -> dict[date, date]:
    """日本の営業日ごとに、**その日より前に確定した**直近の外部日付を返す。

    ::

        米国 date D の終値 = 翌 05:00 JST  → 東京 date D+1 の 9:00 に間に合う
        米国 date T の終値 = 翌 05:00 JST  → 東京 date T の大引けより**後**

    したがって条件は **``外部日付 < 日本の営業日``**（厳密に小さい）。
    等号を許すと、まだ出ていない米国終値で東京の寄り付きを予測することになる
    ——5分足のラベル解釈で踏んだのと同じ形の先読み（意思決定ログ81）。

    Returns:
        ``{日本の営業日: 使ってよい外部日付}``。対応する外部日付が
        1つも無い日（系列の先頭より前）は**含めない**。
    """
    ordered = sorted(set(external_days))
    out: dict[date, date] = {}
    if not ordered:
        return out

    index = 0
    for jp_day in sorted(set(jp_days)):
        # ordered[index] が jp_day 未満である間だけ進める（等号では進めない）
        while index < len(ordered) and ordered[index] < jp_day:
            index += 1
        if index == 0:
            continue  # この日より前の外部終値が無い
        out[jp_day] = ordered[index - 1]
    return out


def log_returns(bars: Sequence[Bar]) -> dict[date, float]:
    """終値の対数リターン。**水準ではなく変化を見るときに使う。**

    対数にするのは、`^VIX` のように水準が数倍動く系列と `^GSPC` のように
    ほとんど動かない系列を同じ土俵で扱うため。

    Returns:
        ``{日付: 前営業日終値からの対数リターン}``。初日は含めない。
        終値が0以下の日はその前後とも飛ばす（0除算・対数の定義域）。
    """
    import math

    ordered = sorted(bars, key=lambda b: b.timestamp)
    out: dict[date, float] = {}
    for prev, today in zip(ordered, ordered[1:], strict=False):
        if prev.close <= 0 or today.close <= 0:
            continue
        out[today.timestamp.date()] = math.log(today.close / prev.close)
    return out


TRAILING_WINDOW = 250
"""順位付けに使う過去の観測数（約1年）。

**固定閾値を置かないため。** VIX の「高い」は水準で決まらない——2024年の20と
2026年の20は市場にとって意味が違う。過去1年での相対位置で見る。
"""


def trailing_rank(
    values: dict[date, float], window: int = TRAILING_WINDOW
) -> dict[date, float]:
    """各日の値が、**それ以前の** ``window`` 日の中で何パーセンタイルかを返す。

    【なぜ全期間の分位点を使わないのか】

    全期間の中央値で二分するのは**事後診断**にしかならない。レジーム診断で
    その二段構え（事後診断 → 先読み版フィルタの設計）に入り、持続性が
    測れずに打ち止めになった（意思決定ログ47〜50）。**同じ回り道をしない。**

    比較対象を「その日より前の観測だけ」に限れば、**最初から実装可能**な
    条件づけになる。当日の値そのものは使ってよい——VIX の終値は
    05:00 JST に確定していて、東京の寄り付き前に読める。

    Returns:
        ``{日付: 0.0〜1.0 のパーセンタイル}``。比較対象が ``window`` 本に
        満たない日は**含めない**（少ない標本で順位を出すと粗くなる）。
    """
    ordered = sorted(values)
    out: dict[date, float] = {}
    for i, day in enumerate(ordered):
        if i < window:
            continue  # 過去が足りない日は黙って埋めず、落とす
        prior = [values[d] for d in ordered[i - window : i]]
        today = values[day]
        below = sum(1 for v in prior if v < today)
        out[day] = below / len(prior)
    return out
