"""yfinance（Yahoo Finance）からの日足・分足の取得。**Stage A の分足の入手経路。**

**証券口座もAPIキーも不要。** ライブラリを入れるだけで使える。

【当初計画のボトルネックを解いた経路】

当初は「分足はどこからも取れないので kabuステーションAPI で3ヶ月かけて自前蓄積する
しかない」という制約があった。yfinance は**5分足を過去60日分まとめて取得できる**ため、
待たずに検証を始められる（docs/09-data-sources.md）。

【本モジュールが対処している yfinance の落とし穴】

======  ==================================================  ==============================
#       落とし穴                                            本モジュールでの対処
======  ==================================================  ==============================
1       ブロックされても例外が飛ばず空 DataFrame が返る     ``EmptyResponseError`` を送出
2       分割の調整漏れで価格が不連続になる                  分割調整済みの列を使う（配当調整は除く）
2b      欠損は ``None`` ではなく ``NaN``                    ``pd.notna()`` で判定
3       tzキャッシュ(SQLite)の同時アクセスで落ちる          プロセス固有ディレクトリへ隔離
4       仕様変更で数日間データが取れない                    ``base.FallbackDataSource``
======  ==================================================  ==============================

【注意】
- **非公式API。** 規約・可用性・品質の保証がない。落ちる前提で組む
- 遅延15〜20分。リアルタイムではない（Stage A は過去データ検証なので支障なし）
- **板情報（bid/ask/厚み）は取れない** → 約定モデルを保守的に倒す
- 日本株のティッカーは ``7203.T`` のようにサフィックス ``.T``
"""

from __future__ import annotations

import atexit
import logging
import shutil
import tempfile
import time
from datetime import date, datetime, timedelta
from typing import Any

from autotrader.data.base import (
    BarDataSource,
    DataSourceError,
    EmptyResponseError,
    LookbackExceededError,
)
from autotrader.types import Bar

logger = logging.getLogger(__name__)

MAX_LOOKBACK_DAYS: dict[str, int] = {
    "1m": 7,
    "2m": 58,
    "5m": 58,
    "15m": 58,
    "30m": 58,
    "60m": 728,
    "90m": 58,
}
"""足ごとに遡れる日数。**実測値**（2026-08-21、7203.T で確認）。

日足（``1d``）は制限がないため含めない。

======  ==========  ==========  ==================================
足      公称        実測        備考
======  ==========  ==========  ==================================
1m      7日         7日成功     9日は失敗
5m      60日        58日成功    **60日は失敗**
60m     730日       728日成功   **730日は失敗**
======  ==========  ==========  ==================================

**公称値より2日少ない値を採用している。** Yahoo の「直近60日以内」という判定は
境界を含まず、``today - 60日`` を指定すると弾かれる::

    5m data not available ... The requested range must be within the last 60 days.

公称値をそのまま定数にすると、上限ぎりぎりの取得が**エラーなく空になる**。
安全側（少なめ）に倒して、確実に取れる範囲だけを許可する。
"""

DEFAULT_BATCH_SIZE = 20
"""1回の ``yf.download`` に渡す銘柄数。

レート制限を避けるための実績値。大きくするとブロックされやすくなる。
"""

DEFAULT_BATCH_INTERVAL_SECONDS = 1.0
"""バッチ間の待機秒数。レート制限対策の実績値。"""

DEFAULT_ADJUST_DIVIDENDS = False
"""配当調整を行うか。**既定は False。**

yfinance の ``auto_adjust=True`` は**分割と配当の両方**を調整する（トータルリターン基準）。
一方 J-Quants の ``AdjC`` は**分割のみ**調整する。基準が違うと2つの問題が起きる：

1. **継ぎ目の段差** — 日足は「2年前〜84日前は J-Quants、直近84日は yfinance」で繋ぐ。
   基準が違うと継ぎ目に**実在しない価格変化**が生まれ、その期間を跨ぐ
   14日ATR や 20日平均が汚染されて架空のシグナルになる。

   実測（7203、118日）で **中央値1.467%・最大1.467%** の差が出た。
   中央値と最大値が一致するのはランダムなノイズではなく**一定のオフセット**の証拠で、
   トヨタの半期配当の水準と一致する。

2. **株価レンジフィルタのずれ** — 配当調整済み価格は実際の約定価格ではないため、
   300〜3,000円の判定が実態とずれる。

**私たちに必要なのは「実際に約定する価格」**なので、分割のみ調整が正しい基準。
"""

_tz_cache_initialized = False


def _init_tz_cache() -> None:
    """yfinance のタイムゾーンキャッシュをプロセス固有のディレクトリに隔離する。

    yfinance は内部で ``~/.cache/py-yfinance/`` の SQLite をタイムゾーンキャッシュに
    使う。**複数プロセスが同時に読み書きすると ``OperationalError`` で落ちる。**

    本プロジェクトでは日次の分足回収バッチ・日足取得バッチ・手動のバックテスト実行が
    並走しうるため、この隔離が必要になる。ローカルで1プロセスしか動かさない開発中は
    踏まないので、**本番で初めて落ちる**類の問題。

    プロセス終了時に ``atexit`` で一時ディレクトリを削除する。
    """
    global _tz_cache_initialized
    if _tz_cache_initialized:
        return

    import yfinance as yf

    cache_dir = tempfile.mkdtemp(prefix="py-yfinance-")
    yf.set_tz_cache_location(cache_dir)
    atexit.register(shutil.rmtree, cache_dir, True)
    _tz_cache_initialized = True
    logger.debug("yfinance tzキャッシュを隔離した: %s", cache_dir)


def to_ticker(code: str) -> str:
    """銘柄コードを Yahoo のティッカーに変換する（``7203`` → ``7203.T``）。"""
    code = code.strip()
    if not code:
        raise ValueError("銘柄コードが空")
    if code.endswith(".T"):
        return code
    return f"{code}.T"


def from_ticker(ticker: str) -> str:
    """Yahoo のティッカーを銘柄コードに戻す（``7203.T`` → ``7203``）。"""
    return ticker[:-2] if ticker.endswith(".T") else ticker


def check_lookback(interval: str, start: date, *, today: date | None = None) -> None:
    """遡れる期間を超えていないか検査する。

    **超過を黙って切り詰めない。** 取れたつもりで欠損しているのが最悪のケースで、
    バックテストの前提が静かに壊れる。

    Raises:
        LookbackExceededError: ``MAX_LOOKBACK_DAYS`` を超える場合。
    """
    limit = MAX_LOOKBACK_DAYS.get(interval)
    if limit is None:
        return  # 日足など、制限のない足

    today = today or date.today()
    oldest = today - timedelta(days=limit)
    if start < oldest:
        raise LookbackExceededError(
            f"interval={interval} は直近{limit}日（{oldest} 以降）しか遡れない。"
            f"指定された開始日 {start} は範囲外"
        )


class YahooDataSource(BarDataSource):
    """yfinance 経由の OHLCV 取得。

    Stage A の分足の唯一の入手経路であり、日足では J-Quants のフォールバック先。
    """

    def __init__(
        self,
        batch_size: int = DEFAULT_BATCH_SIZE,
        batch_interval_seconds: float = DEFAULT_BATCH_INTERVAL_SECONDS,
        adjust_dividends: bool = DEFAULT_ADJUST_DIVIDENDS,
    ) -> None:
        """
        Args:
            adjust_dividends: 配当調整を行うか。**既定は False**（§``DEFAULT_ADJUST_DIVIDENDS``）。
                J-Quants の ``AdjC`` と基準を揃えるため。
        """
        self._batch_size = batch_size
        self._batch_interval = batch_interval_seconds
        self._adjust_dividends = adjust_dividends

    @property
    def name(self) -> str:
        return "yahoo"

    def supports_interval(self, interval: str) -> bool:
        return interval == "1d" or interval in MAX_LOOKBACK_DAYS

    def get_bars(
        self,
        symbol: str,
        interval: str,
        start: date,
        end: date,
    ) -> tuple[Bar, ...]:
        """1銘柄のバーを取得する。

        複数銘柄をまとめて取る場合は ``get_bars_batch`` を使うこと
        （リクエスト数を減らせてレート制限に当たりにくい）。
        """
        result = self.get_bars_batch((symbol,), interval, start, end)
        bars = result.get(symbol)
        if not bars:
            raise EmptyResponseError(
                f"yfinance が {symbol} ({interval}) について空の応答を返した"
            )
        return bars

    def get_bars_batch(
        self,
        symbols: tuple[str, ...],
        interval: str,
        start: date,
        end: date,
    ) -> dict[str, tuple[Bar, ...]]:
        """複数銘柄のバーをバッチで取得する。

        ``batch_size`` 銘柄ずつに分割し、バッチ間に ``batch_interval_seconds`` 待つ。
        短時間に大量のリクエストを投げると Yahoo 側から IP ブロックされるため。

        Returns:
            銘柄コード → バー列。**取得できなかった銘柄はキーごと含まれない。**
            呼び出し側は欠けた銘柄を検出して扱いを決めること
            （黙って無視すると、ユニバースが静かに縮む）。

        Raises:
            LookbackExceededError: 遡れる期間を超えた場合。
            EmptyResponseError: 全バッチが空だった場合（ブロックの可能性が高い）。
        """
        if not symbols:
            return {}

        check_lookback(interval, start)
        _init_tz_cache()

        out: dict[str, tuple[Bar, ...]] = {}
        empty_batches = 0
        total_batches = 0

        for offset in range(0, len(symbols), self._batch_size):
            batch = symbols[offset : offset + self._batch_size]
            total_batches += 1

            frame = self._download(batch, interval, start, end)
            if frame is None:
                # 空の DataFrame。ブロックの可能性があるが確定できない。
                empty_batches += 1
                logger.warning(
                    "yfinance が空の応答を返した (interval=%s batch=%s)。"
                    "レート制限の可能性あり",
                    interval,
                    ",".join(batch),
                )
            else:
                out.update(self._parse(frame, batch, interval))

            if offset + self._batch_size < len(symbols):
                time.sleep(self._batch_interval)

        if empty_batches == total_batches:
            # 全滅。個別銘柄のデータ欠損では説明がつかないのでブロックとみなす。
            raise EmptyResponseError(
                f"yfinance が全 {total_batches} バッチで空の応答を返した "
                f"(interval={interval})。レート制限またはAPI仕様変更の可能性"
            )

        missing = [s for s in symbols if s not in out]
        if missing:
            logger.warning(
                "yfinance で取得できなかった銘柄が %d 件ある: %s",
                len(missing),
                ",".join(missing[:10]) + ("..." if len(missing) > 10 else ""),
            )

        return out

    def _download(
        self,
        symbols: tuple[str, ...],
        interval: str,
        start: date,
        end: date,
    ) -> Any | None:
        """yfinance を呼ぶ。空の DataFrame なら ``None`` を返す。

        ``auto_adjust`` は**分割と配当の両方**を調整する。
        分割調整は必須（無効だと分割前後で価格が不連続になり、ATR% や売買代金が
        壊れて架空の急騰を誤検出する）が、**配当調整は入れてはならない**
        （``DEFAULT_ADJUST_DIVIDENDS`` の説明を参照）。

        ``auto_adjust=False`` のとき ``Close`` は分割調整済み・配当未調整、
        ``Adj Close`` が配当も調整した値になる。前者を使う。
        """
        import yfinance as yf

        tickers = [to_ticker(s) for s in symbols]
        try:
            frame = yf.download(
                tickers,
                interval=interval,
                start=start.isoformat(),
                end=end.isoformat(),
                auto_adjust=self._adjust_dividends,
                progress=False,
                group_by="ticker",
                threads=False,  # tzキャッシュの競合を避ける
            )
        except Exception as exc:  # noqa: BLE001 - 非公式APIは何を投げるか不定
            raise DataSourceError(f"yfinance の呼び出しに失敗した: {exc}") from exc

        if frame is None or frame.empty:
            return None
        return frame

    def _parse(
        self,
        frame: Any,
        symbols: tuple[str, ...],
        interval: str,
    ) -> dict[str, tuple[Bar, ...]]:
        """yfinance の DataFrame を ``Bar`` 列に変換する。

        欠損は ``None`` ではなく ``NaN`` で返るため ``pd.notna()`` で判定する。
        ``if value is None`` では検出できない。

        列の取り出しは ``_select_ticker`` に委ねる。**銘柄数で分岐してはならない**
        （yfinance は単一ティッカーでも MultiIndex を返す。§`_select_ticker` 参照）。
        """
        import pandas as pd

        out: dict[str, tuple[Bar, ...]] = {}

        for code in symbols:
            sub = _select_ticker(frame, to_ticker(code))
            if sub is None or sub.empty:
                continue

            bars: list[Bar] = []
            for ts, row in sub.iterrows():
                values = [row.get(c) for c in ("Open", "High", "Low", "Close", "Volume")]
                if not all(pd.notna(v) for v in values):
                    continue  # 欠損行はスキップ（NaN は None ではない）
                o, h, low, c, v = values
                bars.append(
                    Bar(
                        symbol=code,
                        timestamp=_to_datetime(ts),
                        open=float(o),
                        high=float(h),
                        low=float(low),
                        close=float(c),
                        volume=int(v),
                    )
                )

            if bars:
                out[code] = tuple(bars)

        return out


def _select_ticker(frame: Any, ticker: str) -> Any | None:
    """DataFrame から1銘柄ぶんの列を取り出す。

    **銘柄数で分岐してはならない。** yfinance は
    **単一ティッカーでも多階層インデックス（MultiIndex）を返すのが既定**に変わっており、
    「1銘柄だからフラットなはず」と仮定すると列名が `("7203.T", "Open")` のタプルのため
    `row.get("Open")` が `None` を返し、**全行が欠損として捨てられて空になる**。

    実際にこの不具合を踏んだ（Phase 1 の実測で、1銘柄の取得だけが全滅した）。
    列構造そのものを見て判定する。

    level の順序は ``group_by`` の指定や yfinance のバージョンで変わりうるため、
    ticker が level 0 にある場合と level 1 にある場合の**両方に対応する**。

    Returns:
        その銘柄の OHLCV を列に持つ DataFrame。見つからなければ ``None``。
    """
    import pandas as pd

    columns = getattr(frame, "columns", None)
    if columns is None:
        return None

    if isinstance(columns, pd.MultiIndex):
        if ticker in columns.get_level_values(0):
            return frame[ticker]
        if ticker in columns.get_level_values(1):
            return frame.xs(ticker, axis=1, level=1)
        return None

    # フラットな列（単一ティッカーで MultiIndex を返さない構成）
    return frame


def _to_datetime(value: Any) -> datetime:
    """pandas の Timestamp を datetime に変換する。"""
    if isinstance(value, datetime):
        return value
    return value.to_pydatetime()  # type: ignore[no-any-return]
