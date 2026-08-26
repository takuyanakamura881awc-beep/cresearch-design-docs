"""J-Quants API（V2）からの日足・銘柄一覧の取得。

日本取引所グループ（JPX）が提供する公式データ配信サービス。
**証券口座は不要**で、メールアドレスでのアカウント登録のみで使える。

【プラン】

- Free: 過去2年分。**ただし直近12週間を除く**。5リクエスト/分
- Light 1,650円/月〜: 遅延なし・5年分・60リクエスト/分

まず Free で始め、``scripts/verify_data_sources.py`` で yfinance との
乖離を実測してから有料化を判断する。推測で課金しない。

【V2 仕様（V1 は 2026年6月1日に終了済み）】

============  ==============================  ==============================
項目          V1（廃止）                      V2（現行）
============  ==============================  ==============================
銘柄一覧      ``/listed/info``                ``/equities/master``
日足          ``/prices/daily_quotes``        ``/equities/bars/daily``
レスポンス    ``{"daily_quotes": [...]}``     ``{"data": [...]}``
項目名        ``Open`` ``AdjustmentClose``    短縮形 ``O`` ``AdjC`` など
============  ==============================  ==============================

認証は API キー方式（``x-api-key`` ヘッダ）。V1 のリフレッシュトークン方式は廃止。
キーは ``.env`` の ``JQUANTS_API_KEY`` から読む。コードに書かない。

【項目名は候補リストで解決する】

V2 の短縮形の正確な綴りは、公開情報からの推定を含む。
そのため ``_pick`` で**候補キーを順に試す**方式にしてある。
実際の項目名は ``scripts/verify_data_sources.py`` が実データから出力するので、
確定したら ``_FIELD_CANDIDATES`` を整理する。

【本モジュールが担う代替不可能な役割】

**日付指定の上場銘柄一覧**（``list_symbols``）。
「現在」の一覧を過去に適用すると、上場廃止・降格した銘柄が母集団から抜け落ち、
成績が構造的に過大評価される（サバイバーシップバイアス）。
yfinance はティッカー指定のAPIで市場の構成銘柄を列挙できないため、
**この機能は J-Quants でしか得られない**。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime
from typing import Any

from autotrader.data.base import (
    BarDataSource,
    DataSourceError,
    EmptyResponseError,
    RateLimitError,
    SubscriptionRangeError,
)
from autotrader.types import Bar, Symbol

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.jquants.com/v2"

ENDPOINT_MASTER = "equities/master"
"""上場銘柄一覧（V1 の ``listed/info``）。"""

ENDPOINT_DAILY_BARS = "equities/bars/daily"
"""日足四本値（V1 の ``prices/daily_quotes``）。"""

RESPONSE_DATA_KEYS = ("data", "daily_quotes", "info")
"""レスポンスの配列を包むキーの候補。V2 は ``data``。

V1 形式が返ってきても動くよう、旧キーも候補に残す。
"""

FREE_PLAN_REQUESTS_PER_MINUTE = 5
"""Free プランのレート制限。Light は 60。"""

FREE_PLAN_DELAY_DAYS = 84
"""Free プランのデータ遅延（12週間 = 84日）。

**この遅延により、Free の日足は yfinance の5分足（直近60日）と期間が重ならない。**
直近84日の日足は yfinance で補完する必要がある（docs/09-data-sources.md）。
"""

_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "code": ("Code", "code"),
    "date": ("Date", "date"),
    # 調整済みの値を優先する。未調整だと分割の前後で価格が不連続になり、
    # ATR% や売買代金の計算が壊れて架空の急騰を誤検出する。
    "open": ("AdjO", "AdjustmentOpen", "O", "Open", "open"),
    "high": ("AdjH", "AdjustmentHigh", "H", "High", "high"),
    "low": ("AdjL", "AdjustmentLow", "L", "Low", "low"),
    "close": ("AdjC", "AdjustmentClose", "C", "Close", "close"),
    "volume": ("AdjVo", "AdjustmentVolume", "Vo", "Volume", "volume"),
    "turnover": ("Va", "TurnoverValue", "turnover"),
    "limit_up": ("UL", "UpperLimit"),
    "limit_down": ("LL", "LowerLimit"),
    # 会社名は CoName。CompanyName と推測して外し、銘柄名が空になった実績がある。
    "name": ("CoName", "CompanyName", "Name", "name"),
    "market": ("MktNm", "MarketCodeName", "MarketName"),
    "margin": ("MrgnNm", "MarginCodeName"),
    "sector": ("S33Nm", "Sector33CodeName"),
    "scale": ("ScaleCat", "ScaleCategory"),
}
"""レスポンス項目名の候補。左から順に試し、最初に見つかった値を使う。

**先頭の候補は実データで確認済み**（scripts/verify_data_sources.py の出力）。
後続は V1 形式などの保険で、非公式APIで項目名が揺れても壊れないようにしてある。

実測で確認した項目（2026-08-21）::

    日足      AdjC AdjFactor AdjH AdjL AdjO AdjVo C Code Date
              ExRT H L LL MktCap O UL Va Vo
    銘柄一覧  CoName CoNameEn Code Date Mkt MktNm Mrgn MrgnNm
              ProdCat S17 S17Nm S33 S33Nm ScaleCat
"""


_SUBSCRIPTION_RANGE_RE = re.compile(
    r"subscription covers the following dates:\s*"
    r"(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
"""400 のメッセージから購読範囲を抜き出す。

実際に返ってきた文字列（2026-08-23 に確認）::

    Your subscription covers the following dates: 2024-05-31 ~ 2026-05-31.
    If you want more data, please check other plans:https://jpx-jquants.com/#dataset
"""


def parse_subscription_range(message: str) -> tuple[date, date] | None:
    """購読範囲を抽出する。抽出できなければ ``None``。

    形式が変わっても壊れないよう、**抽出できないことを許容する**
    （範囲が分からなければ従来どおり都度リクエストするだけ）。
    """
    match = _SUBSCRIPTION_RANGE_RE.search(message)
    if match is None:
        return None
    try:
        return (
            date.fromisoformat(match.group(1)),
            date.fromisoformat(match.group(2)),
        )
    except ValueError:
        return None


def _pick(record: dict[str, Any], field: str) -> Any:
    """レコードから項目を取り出す。候補キーを順に試す。

    Returns:
        最初に見つかった非 ``None`` の値。どれも無ければ ``None``。
    """
    for key in _FIELD_CANDIDATES[field]:
        value = record.get(key)
        if value is not None:
            return value
    return None


class RateLimiter:
    """リクエストを**均等な間隔**で送る。

    API 側の制限に当てて弾かれるのではなく、**こちらで待つ**。

    【なぜスライディングウィンドウではなく均等ペースなのか】

    「直近60秒で5件まで」というスライディングウィンドウは、
    **5件を一気に送って58秒待ち、また5件を一気に送る**挙動になる。
    サーバ側のウィンドウが少しでもずれていると超過する::

        自分の窓 [t=0,  t=60]  : 5件（自分の判定では OK）
        自分の窓 [t=58, t=118] : 5件（自分の判定では OK）
        サーバの窓 [t=1, t=61] : 1回目の残り4件 + 2回目の5件 = 9件 → 429

    実際にこれで 429 を踏んだ。**バーストが原因。**

    ``60 / per_minute`` 秒ごとに1件だけ送れば、どんなウィンドウの切り方でも
    超過しない。最も保守的で、サーバ側の実装（固定窓・トークンバケット等）に
    依存しない。
    """

    def __init__(self, per_minute: int) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute は1以上")
        self._interval = 60.0 / per_minute
        self._last: float | None = None

    @property
    def interval_seconds(self) -> float:
        """リクエスト間隔（秒）。"""
        return self._interval

    def acquire(self) -> None:
        """前回から ``interval_seconds`` 経つまで待つ。"""
        if self._last is not None:
            elapsed = time.monotonic() - self._last
            wait = self._interval - elapsed
            if wait > 0:
                logger.debug("レート制限のため %.1f 秒待機する", wait)
                time.sleep(wait)
        self._last = time.monotonic()


class JQuantsDataSource(BarDataSource):
    """J-Quants API（V2）クライアント。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        requests_per_minute: int = FREE_PLAN_REQUESTS_PER_MINUTE,
        timeout_seconds: float = 30.0,
        max_rate_limit_retries: int = 2,
    ) -> None:
        if not api_key:
            raise ValueError("api_key が空")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._limiter = RateLimiter(requests_per_minute)
        self._timeout = timeout_seconds
        self._max_rate_limit_retries = max_rate_limit_retries
        self._subscription_range: tuple[date, date] | None = None

    @property
    def name(self) -> str:
        return "jquants"

    @property
    def limiter_interval_seconds(self) -> float:
        """リクエスト間隔（秒）。診断スクリプトが所要時間を見積もるのに使う。"""
        return self._limiter.interval_seconds

    @property
    def subscription_range(self) -> tuple[date, date] | None:
        """判明している購読範囲。まだ範囲外を叩いていなければ ``None``。

        400 応答から自動で学習する。推測した定数ではなく**実際の範囲**なので、
        プランを変更しても追随する。
        """
        return self._subscription_range

    def covers(self, day: date) -> bool:
        """その日付が購読範囲内か。

        範囲が未判明なら ``True``（叩いてみないと分からない）。
        """
        if self._subscription_range is None:
            return True
        start, end = self._subscription_range
        return start <= day <= end

    def _ensure_covered(self, *days: date) -> None:
        """範囲外なら**リクエストを送らずに**例外にする。

        5件/分の制約下では、範囲外を叩き続けるのは予算の無駄。
        Phase 2 の一括収集（2年分の営業日ループ）では特に効く。
        """
        if self._subscription_range is None:
            return
        start, end = self._subscription_range
        for day in days:
            if not (start <= day <= end):
                raise SubscriptionRangeError(
                    f"契約プランの範囲外（{start} 〜 {end}）: {day} は範囲外のため照会しない",
                    covered_from=start,
                    covered_to=end,
                )

    def supports_interval(self, interval: str) -> bool:
        """日足のみ。**J-Quants に分足は存在しない。**"""
        return interval == "1d"

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def get_raw(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """生のレスポンスを返す。

        検証スクリプトが**実際の項目名を確認する**ために使う公開経路。
        推測でコードを書かず、実データで確かめるための入口。
        """
        return self._get(path, params)

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """レート制限に当たったら待って再試行する。

        サーバ側の制限方式は公開されていないため、こちらのペース制御だけでは
        取りこぼしうる。**予測しきれない前提で回復可能にしておく。**

        Raises:
            RateLimitError: 再試行しても解消しなかった場合。
                **呼び出し側はこれを「データなし」と混同してはならない。**
        """
        delay = self._limiter.interval_seconds
        last: RateLimitError | None = None

        for attempt in range(self._max_rate_limit_retries + 1):
            try:
                return self._request(path, params)
            except RateLimitError as exc:
                last = exc
                if attempt >= self._max_rate_limit_retries:
                    break
                logger.warning(
                    "レート制限に達した。%.0f秒待って再試行する (%d/%d)",
                    delay,
                    attempt + 1,
                    self._max_rate_limit_retries,
                )
                time.sleep(delay)
                delay *= 2  # 指数バックオフ

        assert last is not None
        raise last

    def _request(self, path: str, params: dict[str, str]) -> dict[str, Any]:
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
            raise DataSourceError(f"J-Quants への接続に失敗した ({url}): {exc}") from exc

        if response.status_code == 401:
            raise DataSourceError(
                f"J-Quants の認証に失敗した（401 / {url}）。APIキーを確認すること。"
                "ユーザー登録だけでは使えず、Freeプランへの登録が別途必要"
            )
        if response.status_code == 403:
            raise DataSourceError(
                f"J-Quants にアクセス権がない（403 / {url}）。"
                "プランで利用できないエンドポイントの可能性"
            )
        if response.status_code == 404:
            raise DataSourceError(
                f"J-Quants のエンドポイントが見つからない（404 / {url}）。"
                "V2 でパスが変更されている可能性（V1 は2026年6月1日に終了）"
            )
        if response.status_code == 429:
            raise RateLimitError(
                f"J-Quants のレート制限に達した（429 / {url}）。"
                f"現在の間隔: {self._limiter.interval_seconds:.1f}秒/件"
            )
        if response.status_code == 400:
            covered = parse_subscription_range(response.text)
            if covered is not None:
                self._subscription_range = covered
                raise SubscriptionRangeError(
                    f"契約プランの範囲外（{covered[0]} 〜 {covered[1]}）: {url}",
                    covered_from=covered[0],
                    covered_to=covered[1],
                )
            raise DataSourceError(
                f"J-Quants が 400 を返した（{url}）: {response.text[:300]}"
            )
        if response.status_code >= 400:
            raise DataSourceError(
                f"J-Quants が {response.status_code} を返した（{url}）: "
                f"{response.text[:300]}"
            )

        data: dict[str, Any] = response.json()
        return data

    @staticmethod
    def _extract(payload: dict[str, Any]) -> list[Any]:
        """レスポンスから配列を取り出す。包むキーは V2 で ``data`` に変わった。"""
        for key in RESPONSE_DATA_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []

    def _get_paginated(self, path: str, params: dict[str, str]) -> list[Any]:
        """``pagination_key`` を辿って全件を取得する。

        1営業日分の全銘柄（約4,000件）は複数ページに分かれる。
        """
        items: list[Any] = []
        page_params = dict(params)
        pages = 0

        while True:
            payload = self._get(path, page_params)
            items.extend(self._extract(payload))
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
        self._ensure_covered(as_of)
        items = self._get_paginated(ENDPOINT_MASTER, {"date": as_of.isoformat()})
        if not items:
            raise EmptyResponseError(
                f"J-Quants が {as_of} の銘柄一覧について空の応答を返した。"
                f"Freeプランは直近{FREE_PLAN_DELAY_DAYS}日分を取得できない点に注意"
            )

        symbols: list[Symbol] = []
        for item in items:
            code = _normalize_code(_pick(item, "code"))
            if not code:
                continue
            symbols.append(
                Symbol(
                    code=code,
                    name=str(_pick(item, "name") or ""),
                    lot_size=100,
                    market=_as_text(_pick(item, "market")),
                    margin_type=_as_text(_pick(item, "margin")),
                    sector=_as_text(_pick(item, "sector")),
                    scale_category=_as_text(_pick(item, "scale")),
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

        self._ensure_covered(start, end)
        items = self._get_paginated(
            ENDPOINT_DAILY_BARS,
            {"code": symbol, "from": start.isoformat(), "to": end.isoformat()},
        )
        if not items:
            raise EmptyResponseError(
                f"J-Quants が {symbol} ({start}〜{end}) について空の応答を返した"
            )

        bars = [b for b in (_to_bar(i) for i in items) if b is not None]
        if not bars:
            raise EmptyResponseError(
                f"J-Quants の {symbol} の応答に有効なバーが含まれていない。"
                f"項目名が想定と違う可能性がある（先頭レコードのキー: "
                f"{sorted(items[0].keys()) if isinstance(items[0], dict) else '不明'}）"
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
        self._ensure_covered(trade_date)
        items = self._get_paginated(
            ENDPOINT_DAILY_BARS, {"date": trade_date.isoformat()}
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

        if not out:
            raise EmptyResponseError(
                f"J-Quants の {trade_date} の応答に有効なバーが含まれていない。"
                f"項目名が想定と違う可能性がある（先頭レコードのキー: "
                f"{sorted(items[0].keys()) if isinstance(items[0], dict) else '不明'}）"
            )
        return out


def _as_text(value: Any) -> str | None:
    """文字列に変換する。空なら ``None``。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_code(raw: Any) -> str:
    """銘柄コードを4桁に正規化する。

    J-Quants は5桁（末尾0）で返すことがある（``72030`` → ``7203``）。
    """
    code = str(raw or "").strip()
    if len(code) == 5 and code.endswith("0"):
        return code[:4]
    return code


def _to_bar(item: Any) -> Bar | None:
    """J-Quants の1レコードを ``Bar`` に変換する。欠損なら ``None``。

    項目名は ``_FIELD_CANDIDATES`` の候補を順に試す（V2 の短縮形・V1 形式の両対応）。
    """
    if not isinstance(item, dict):
        return None

    code = _normalize_code(_pick(item, "code"))
    raw_date = _pick(item, "date")
    if not code or not raw_date:
        return None

    values = [_pick(item, f) for f in ("open", "high", "low", "close", "volume")]
    if any(v is None for v in values):
        return None

    o, h, low, c, v = values
    try:
        return Bar(
            symbol=code,
            timestamp=datetime.fromisoformat(str(raw_date)),
            open=float(o),
            high=float(h),
            low=float(low),
            close=float(c),
            volume=int(float(v)),
            turnover=_as_float(_pick(item, "turnover")),
            limit_up=_as_flag(_pick(item, "limit_up")),
            limit_down=_as_flag(_pick(item, "limit_down")),
        )
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    """数値に変換する。変換できなければ ``None``（欠損として扱う）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_flag(value: Any) -> bool | None:
    """値幅制限フラグを解釈する。

    J-Quants は ``UL`` / ``LL`` を文字列の ``"0"`` / ``"1"`` で返す（実測で確認）。
    ``bool("0")`` は True になってしまうため、**文字列をそのまま真偽値にしない**。
    """
    if value is None:
        return None
    text = str(value).strip()
    if text in ("0", "", "-"):
        return False
    if text == "1":
        return True
    return None
