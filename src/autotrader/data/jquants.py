"""J-Quants API（V2）からの日足・銘柄一覧の取得。

日本取引所グループ（JPX）が提供する公式データ配信サービス。
**証券口座は不要**で、メールアドレスでのアカウント登録のみで使える。

【プラン】

- Free: 過去2年分。**ただし直近12週間を除く**。5リクエスト/分
- Light 1,650円/月〜: 遅延なし・5年分・60リクエスト/分

まず Free で始め、``scripts/verify_data_sources.py`` で yfinance との
乖離を実測してから有料化を判断する。推測で課金しない。

【認証】

V2 は API キー方式（V1 のリフレッシュトークン方式は廃止済み）。
キーは ``.env`` の ``JQUANTS_API_KEY`` から読む。コードに書かない。

【本モジュールが担う代替不可能な役割】

**日付指定の上場銘柄一覧**（``list_symbols``）。
「現在」の一覧を過去に適用すると、上場廃止・降格した銘柄が母集団から抜け落ち、
成績が構造的に過大評価される（サバイバーシップバイアス）。
yfinance はティッカー指定のAPIで市場の構成銘柄を列挙できないため、
**この機能は J-Quants でしか得られない**。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import date, datetime
from typing import Any

from autotrader.data.base import BarDataSource, DataSourceError, EmptyResponseError
from autotrader.types import Bar, Symbol

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.jquants.com/v2"

FREE_PLAN_REQUESTS_PER_MINUTE = 5
"""Free プランのレート制限。Light は 60。"""

FREE_PLAN_DELAY_DAYS = 84
"""Free プランのデータ遅延（12週間 = 84日）。

**この遅延により、Free の日足は yfinance の5分足（直近60日）と期間が重ならない。**
直近84日の日足は yfinance で補完する必要がある（docs/09-data-sources.md）。
"""


class RateLimiter:
    """リクエスト数を毎分の上限内に収める。

    API 側の制限に当てて弾かれるのではなく、**こちらで待つ**。
    弾かれた場合のリトライは、成功したのか失敗したのか判断が難しくなる。
    """

    def __init__(self, per_minute: int) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute は1以上")
        self._per_minute = per_minute
        self._timestamps: deque[float] = deque()

    def acquire(self) -> None:
        """上限を超えないよう、必要なら待つ。"""
        now = time.monotonic()
        while self._timestamps and now - self._timestamps[0] >= 60.0:
            self._timestamps.popleft()

        if len(self._timestamps) >= self._per_minute:
            wait = 60.0 - (now - self._timestamps[0])
            if wait > 0:
                logger.debug("レート制限のため %.1f 秒待機する", wait)
                time.sleep(wait)
            now = time.monotonic()
            while self._timestamps and now - self._timestamps[0] >= 60.0:
                self._timestamps.popleft()

        self._timestamps.append(time.monotonic())


class JQuantsDataSource(BarDataSource):
    """J-Quants API クライアント。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        requests_per_minute: int = FREE_PLAN_REQUESTS_PER_MINUTE,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key が空")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._limiter = RateLimiter(requests_per_minute)
        self._timeout = timeout_seconds

    @property
    def name(self) -> str:
        return "jquants"

    def supports_interval(self, interval: str) -> bool:
        """日足のみ。**J-Quants に分足は存在しない。**"""
        return interval == "1d"

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        import httpx

        self._limiter.acquire()
        url = f"{self._base_url}/{path.lstrip('/')}"
        try:
            response = httpx.get(
                url,
                params=params,
                headers={"x-api-key": self._api_key},
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise DataSourceError(f"J-Quants への接続に失敗した: {exc}") from exc

        if response.status_code == 401:
            raise DataSourceError(
                "J-Quants の認証に失敗した（401）。APIキーを確認すること。"
                "ユーザー登録だけでは使えず、Freeプランへの登録が別途必要"
            )
        if response.status_code == 429:
            raise DataSourceError(
                "J-Quants のレート制限に達した（429）。"
                "requests_per_minute の設定を確認すること"
            )
        if response.status_code >= 400:
            raise DataSourceError(
                f"J-Quants が {response.status_code} を返した: {response.text[:200]}"
            )

        data: dict[str, Any] = response.json()
        return data

    def _get_paginated(self, path: str, params: dict[str, str], key: str) -> list[Any]:
        """``pagination_key`` を辿って全件を取得する。

        1営業日分の全銘柄（約4,000件）は複数ページに分かれる。
        """
        items: list[Any] = []
        page_params = dict(params)
        pages = 0

        while True:
            payload = self._get(path, page_params)
            items.extend(payload.get(key, []))
            pages += 1

            next_key = payload.get("pagination_key")
            if not next_key:
                break
            page_params["pagination_key"] = str(next_key)

        logger.debug("J-Quants %s: %d件 (%dページ)", path, len(items), pages)
        return items

    # ------------------------------------------------------------------
    # 銘柄一覧
    # ------------------------------------------------------------------

    def list_symbols(self, as_of: date) -> tuple[Symbol, ...] | None:
        """指定日時点の上場銘柄一覧を取得する。

        **``as_of`` を必ず渡す。** 現在の一覧を過去に適用してはならない
        （サバイバーシップバイアス。docs/03-universe.md §4.2）。
        """
        items = self._get_paginated(
            "listed/info", {"date": as_of.isoformat()}, "info"
        )
        if not items:
            raise EmptyResponseError(
                f"J-Quants が {as_of} の銘柄一覧について空の応答を返した。"
                f"Freeプランは直近{FREE_PLAN_DELAY_DAYS}日分を取得できない点に注意"
            )

        symbols: list[Symbol] = []
        for item in items:
            code = str(item.get("Code", "")).strip()
            if not code:
                continue
            # J-Quants の Code は5桁（末尾0）で返ることがある
            if len(code) == 5 and code.endswith("0"):
                code = code[:4]
            symbols.append(
                Symbol(
                    code=code,
                    name=str(item.get("CompanyName", "")),
                    lot_size=100,
                )
            )
        return tuple(symbols)

    # ------------------------------------------------------------------
    # 日足
    # ------------------------------------------------------------------

    def get_bars(
        self,
        symbol: str,
        interval: str,
        start: date,
        end: date,
    ) -> tuple[Bar, ...]:
        """1銘柄の日足を取得する。

        **全銘柄をまとめて取るときは ``get_bars_for_date`` を使うこと。**
        銘柄ごとにループすると、``date=`` での一括取得に比べて約4倍のリクエストが
        必要になる（2年分で 6,000 vs 1,470 リクエスト）。
        """
        if interval != "1d":
            raise DataSourceError(
                f"J-Quants は日足のみ対応（指定: {interval}）。分足は存在しない"
            )

        items = self._get_paginated(
            "prices/daily_quotes",
            {"code": symbol, "from": start.isoformat(), "to": end.isoformat()},
            "daily_quotes",
        )
        if not items:
            raise EmptyResponseError(
                f"J-Quants が {symbol} ({start}〜{end}) について空の応答を返した"
            )

        bars = [b for b in (_to_bar(i) for i in items) if b is not None]
        if not bars:
            raise EmptyResponseError(
                f"J-Quants の {symbol} の応答に有効なバーが含まれていない"
            )
        return tuple(sorted(bars, key=lambda b: b.timestamp))

    def get_bars_for_date(self, trade_date: date) -> dict[str, tuple[Bar, ...]]:
        """指定営業日の**全上場銘柄**の日足を1回の呼び出し系列で取得する。

        **これが日足の一括取得の推奨経路。** ``date=`` を指定すると全銘柄が返る。

        2年分を集めるなら約490営業日 × 数ページ ≒ 1,470リクエスト。
        Free の5リクエスト/分でも約5時間で、一晩のバッチで完了する。

        Returns:
            銘柄コード → その日のバー（1本）。
        """
        items = self._get_paginated(
            "prices/daily_quotes", {"date": trade_date.isoformat()}, "daily_quotes"
        )
        if not items:
            raise EmptyResponseError(
                f"J-Quants が {trade_date} について空の応答を返した。"
                f"非営業日か、Freeプランの遅延（{FREE_PLAN_DELAY_DAYS}日）の範囲内の可能性"
            )

        out: dict[str, tuple[Bar, ...]] = {}
        for item in items:
            bar = _to_bar(item)
            if bar is not None:
                out[bar.symbol] = (bar,)
        return out


def _to_bar(item: dict[str, Any]) -> Bar | None:
    """J-Quants の1レコードを ``Bar`` に変換する。欠損なら ``None``。

    調整済みの値（``AdjustmentOpen`` など）を優先する。
    未調整の値を使うと、分割の前後で価格が不連続になる。
    """
    code = str(item.get("Code", "")).strip()
    if len(code) == 5 and code.endswith("0"):
        code = code[:4]

    raw_date = item.get("Date")
    if not code or not raw_date:
        return None

    def pick(adjusted: str, plain: str) -> Any:
        value = item.get(adjusted)
        return value if value is not None else item.get(plain)

    o = pick("AdjustmentOpen", "Open")
    h = pick("AdjustmentHigh", "High")
    low = pick("AdjustmentLow", "Low")
    c = pick("AdjustmentClose", "Close")
    v = pick("AdjustmentVolume", "Volume")

    if any(x is None for x in (o, h, low, c, v)):
        return None

    try:
        return Bar(
            symbol=code,
            timestamp=datetime.fromisoformat(str(raw_date)),
            open=float(o),
            high=float(h),
            low=float(low),
            close=float(c),
            volume=int(float(v)),
        )
    except (TypeError, ValueError):
        return None
