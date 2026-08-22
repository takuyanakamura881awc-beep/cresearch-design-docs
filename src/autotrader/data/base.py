"""データソースの抽象インターフェースとフォールバック。

`broker/base.py` の ``Broker`` ABC と同じ設計思想で、データ取得も抽象化する。

【なぜ抽象化するのか】

yfinance は Yahoo Finance の**非公式API**で、エンドポイントやHTML構造の変更により
**ある日突然データが取れなくなる**ことがある。実際「昨日まで動いていたのに今日から
全銘柄 empty DataFrame」という報告が定期的に上がっており、ライブラリが対応するまで
数日間データが取れない状態が続くこともある。

日次でデータを回すシステムにとって「数日間データが取れない」は致命的。
**単一のデータソースに依存しない構成**にすることで、片方が死んでも走り続けられる。

【Stage ごとの構成】

===========  ===========================================  ==========================
データ       構成                                         フォールバック
===========  ===========================================  ==========================
日足         ``FallbackDataSource([JQuants, Yahoo])``     あり
5分足        ``YahooDataSource`` のみ                     **なし（単一障害点）**
銘柄一覧     ``JQuantsDataSource`` のみ                   **なし（代替不可）**
===========  ===========================================  ==========================

5分足と銘柄一覧にフォールバック先が存在しないことは、設計上の既知のリスク。
5分足は J-Quants に存在せず、日付指定の銘柄一覧は yfinance に機能がない。
蓄積したデータをローカルに確実に残すこと（``store.py`` のキャッシュ）で緩和する。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date

from autotrader.types import Bar, Symbol

logger = logging.getLogger(__name__)


class DataSourceError(Exception):
    """データ取得に失敗した。

    握り潰してはならない。呼び出し側はフォールバックするか、処理を止める。
    """


class EmptyResponseError(DataSourceError):
    """データソースが空の応答を返した。

    **「データがない銘柄」なのか「ブロックされた」のかを区別できない**ため、
    正常系として扱わず必ず例外にする。

    yfinance はレート制限でブロックされても例外を投げず、空の DataFrame を返す。
    これを「データなし」として黙って受け入れると、銘柄が静かにユニバースから
    脱落し、成績が歪んだことに誰も気づけない。**安全側（例外）に倒す。**
    """


class RateLimitError(DataSourceError):
    """データソースのレート制限に達した。

    **「データがない」と混同してはならない。**

    測定や探索のループでこれを「その日はデータなし」として飛ばすと、
    **測定結果そのものが嘘になる**。実際に、データ終端日の実測が
    84日から88日にずれる事故を起こした（429で失敗した日を「データなし」と誤判定した）。

    呼び出し側は、待って再試行するか、**測定を中断して「測定不能」と報告する**。
    誤った値を返すより、測定できなかったと言う方が安全。
    """


class SubscriptionRangeError(DataSourceError):
    """契約プランがカバーしていない期間を要求した。

    J-Quants は範囲外の日付に対し 400 とともに**実際の購読範囲を返す**::

        {"message": "Your subscription covers the following dates:
                     2024-05-31 ~ 2026-05-31. ..."}

    この範囲を保持しておけば、以降は**リクエストを送る前に範囲外を弾ける**。
    5件/分の制約下では、範囲外を叩き続けるのは予算の無駄。

    推測した定数（`FREE_PLAN_DELAY_DAYS`）ではなく **API が返す実際の範囲**を
    使えるため、プランを変更しても自動で追随する。
    """

    def __init__(
        self,
        message: str,
        covered_from: date | None = None,
        covered_to: date | None = None,
    ) -> None:
        super().__init__(message)
        self.covered_from = covered_from
        self.covered_to = covered_to

    @property
    def has_range(self) -> bool:
        """範囲を抽出できたか。"""
        return self.covered_from is not None and self.covered_to is not None


class LookbackExceededError(DataSourceError):
    """データソースが遡れる期間を超えた指定がされた。

    黙って切り詰めない。**取れたつもりで欠損している**のが最悪のケースで、
    バックテストの前提が静かに壊れる。
    """


class BarDataSource(ABC):
    """OHLCV バーを提供するデータソース。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """ソース名。ログとフォールバックの記録に使う。"""

    @abstractmethod
    def get_bars(
        self,
        symbol: str,
        interval: str,
        start: date,
        end: date,
    ) -> tuple[Bar, ...]:
        """バーを取得する。

        Args:
            symbol: 銘柄コード（``7203`` のような、取引所サフィックスなしの形式）。
            interval: ``1m`` / ``5m`` / ``1d`` など。
            start, end: 取得期間。

        Returns:
            バー列。時刻の昇順。

        Raises:
            EmptyResponseError: 空の応答が返った場合。
            LookbackExceededError: 遡れる期間を超えた場合。
            DataSourceError: その他の取得失敗。
        """

    @abstractmethod
    def supports_interval(self, interval: str) -> bool:
        """その足を提供できるか。

        フォールバック時に、そもそも対応していないソースを飛ばすために使う。
        """

    def list_symbols(self, as_of: date) -> tuple[Symbol, ...] | None:
        """指定日時点の上場銘柄一覧を返す。

        **日付指定であることが重要。** 「現在」の一覧を過去に適用すると、
        上場廃止・降格した銘柄が母集団から抜け落ち、成績が構造的に過大評価される
        （サバイバーシップバイアス。docs/03-universe.md §4.2）。

        Returns:
            銘柄一覧。**この機能を持たないソースは ``None`` を返す**
            （yfinance はティッカー指定のAPIで、市場の構成銘柄を列挙できない）。
        """
        return None


class FallbackDataSource(BarDataSource):
    """複数のデータソースを順に試す。

    前段が失敗したら次段へ進む。全て失敗したら最後の例外を送出する。

    【フォールバックの発動は必ずログに残す】

    黙って切り替わると、データの品質差に気づけない。
    J-Quants（JPX公式）と yfinance（非公式）では分割調整の扱いが異なりうるため、
    「いつ・どの銘柄で・どちらのソースが使われたか」が追えないと、
    バックテスト結果の異常を後から診断できなくなる。
    """

    def __init__(self, sources: list[BarDataSource]) -> None:
        if not sources:
            raise ValueError("sources は1つ以上必要")
        self._sources = sources

    @property
    def name(self) -> str:
        return "fallback(" + ",".join(s.name for s in self._sources) + ")"

    @property
    def sources(self) -> tuple[BarDataSource, ...]:
        return tuple(self._sources)

    def supports_interval(self, interval: str) -> bool:
        return any(s.supports_interval(interval) for s in self._sources)

    def get_bars(
        self,
        symbol: str,
        interval: str,
        start: date,
        end: date,
    ) -> tuple[Bar, ...]:
        candidates = [s for s in self._sources if s.supports_interval(interval)]
        if not candidates:
            raise DataSourceError(
                f"interval={interval} に対応するデータソースがない: {self.name}"
            )

        last_error: Exception | None = None
        for i, source in enumerate(candidates):
            try:
                bars = source.get_bars(symbol, interval, start, end)
            except DataSourceError as exc:
                last_error = exc
                remaining = candidates[i + 1 :]
                if remaining:
                    logger.warning(
                        "データソース %s が失敗したため %s にフォールバックする "
                        "(symbol=%s interval=%s): %s",
                        source.name,
                        remaining[0].name,
                        symbol,
                        interval,
                        exc,
                    )
                continue

            if i > 0:
                # 最優先のソースではない = フォールバックが発動した
                logger.warning(
                    "フォールバックしたデータソース %s を使用した "
                    "(symbol=%s interval=%s bars=%d)。"
                    "品質差の可能性があるため記録する",
                    source.name,
                    symbol,
                    interval,
                    len(bars),
                )
            return bars

        assert last_error is not None
        raise DataSourceError(
            f"全データソースが失敗した (symbol={symbol} interval={interval}): {last_error}"
        ) from last_error

    def list_symbols(self, as_of: date) -> tuple[Symbol, ...] | None:
        """銘柄一覧を提供できる最初のソースに委譲する。"""
        for source in self._sources:
            symbols = source.list_symbols(as_of)
            if symbols is not None:
                return symbols
        return None
