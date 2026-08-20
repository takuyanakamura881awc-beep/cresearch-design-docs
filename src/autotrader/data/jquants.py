"""J-Quants API（V2）からの日足・銘柄情報の取得。

【プラン】
- 無料: 12週間遅延。バックテストには使えるが直近データは取れない
- Light 1,650円/月〜: 直近データと長い履歴

まず無料で始め、遅延がボトルネックになった時点で有料への移行を判断する。

【認証】V2 は API キー方式（V1 のリフレッシュトークン方式は廃止）。
キーは .env の JQUANTS_API_KEY から読む。コードに書かない。

【重要】``list_symbols`` は**日付指定で当時の上場銘柄一覧**を返せること。
サバイバーシップバイアスの回避に必須（docs/03-universe.md §4.2）。
"""

from __future__ import annotations

from datetime import date

from autotrader.types import Bar, Symbol


class JQuantsClient:
    """J-Quants API クライアント。"""

    def __init__(self, api_key: str) -> None:
        raise NotImplementedError("Phase 1 で実装する")

    def list_symbols(self, as_of: date) -> tuple[Symbol, ...]:
        """指定日時点の上場銘柄一覧を取得する。

        **現在の一覧を過去に適用してはならない。**
        上場廃止・降格した銘柄が抜けると成績が過大評価される。
        """
        raise NotImplementedError("Phase 1 で実装する")

    def get_daily_bars(
        self, symbol: str, start: date, end: date
    ) -> tuple[Bar, ...]:
        """日足を取得する。

        分割・併合の調整状況を必ず確認すること。
        未調整のデータをそのまま使うと価格の連続性が壊れる。
        """
        raise NotImplementedError("Phase 1 で実装する")
