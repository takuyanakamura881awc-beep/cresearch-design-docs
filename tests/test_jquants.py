"""J-Quants データソースのテスト。HTTP はモックする。"""

from __future__ import annotations

import sys
import time
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from autotrader.data.base import (
    DataSourceError,
    EmptyResponseError,
    RateLimitError,
    SubscriptionRangeError,
)
from autotrader.data.jquants import (
    ENDPOINT_DAILY_BARS,
    ENDPOINT_MASTER,
    FREE_PLAN_DELAY_DAYS,
    JQuantsDataSource,
    RateLimiter,
    parse_subscription_range,
)


class _Response(SimpleNamespace):
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        super().__init__()
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> Any:
        return self._payload


class _FakeHttpx(SimpleNamespace):
    """httpx モジュールの差し替え。応答を順番に返す。"""

    def __init__(self, responses: list[_Response]) -> None:
        super().__init__()
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("応答が尽きた（想定より多く呼ばれた）")
        return self.responses.pop(0)


def _quote(code: str, day: str, close: float = 100.0) -> dict[str, Any]:
    """V2 形式のレコード（短縮された項目名）。"""
    return {
        "Code": code,
        "Date": day,
        "AdjO": close - 1,
        "AdjH": close + 1,
        "AdjL": close - 2,
        "AdjC": close,
        "AdjVo": 10000,
    }


def _v1_quote(code: str, day: str, close: float = 100.0) -> dict[str, Any]:
    """V1 形式のレコード。候補キー方式が旧形式でも動くことの確認用。"""
    return {
        "Code": code,
        "Date": day,
        "AdjustmentOpen": close - 1,
        "AdjustmentHigh": close + 1,
        "AdjustmentLow": close - 2,
        "AdjustmentClose": close,
        "AdjustmentVolume": 10000,
    }


def _install(monkeypatch: pytest.MonkeyPatch, responses: list[_Response]) -> _FakeHttpx:
    fake = _FakeHttpx(responses)
    monkeypatch.setitem(sys.modules, "httpx", fake)
    return fake


def _source(api_key: str = "key", **kwargs: Any) -> JQuantsDataSource:
    """テスト用のクライアント。

    **実時間で待たせない。** 本番は 5件/分（12秒間隔）だが、テストで実際に待つと
    スイート全体が分単位になる。ペース制御そのものは TestRateLimiter で検証する。
    """
    kwargs.setdefault("requests_per_minute", 60_000)  # 1ms 間隔
    return JQuantsDataSource(api_key, **kwargs)


class TestRateLimiter:
    """**バーストせず均等な間隔で送ること。**

    「直近60秒で5件まで」のスライディングウィンドウだと、5件を一気に送って
    58秒待ち、また5件を一気に送る挙動になる。サーバ側のウィンドウがずれていると
    境界で9件が観測されて 429 になる。実際にこれを踏んだ。
    """

    def test_間隔は毎分の上限から決まる(self) -> None:
        assert RateLimiter(per_minute=5).interval_seconds == 12.0
        assert RateLimiter(per_minute=60).interval_seconds == 1.0

    def test_初回は待たない(self) -> None:
        limiter = RateLimiter(per_minute=60)  # 1秒間隔
        started = time.monotonic()
        limiter.acquire()
        assert time.monotonic() - started < 0.5

    def test_連続要求はバーストせず間隔を空ける(self) -> None:
        """5件を連続要求したら 4×間隔ぶん待つこと（バーストなら一瞬で終わる）。"""
        limiter = RateLimiter(per_minute=1200)  # 0.05秒間隔
        started = time.monotonic()
        for _ in range(5):
            limiter.acquire()
        elapsed = time.monotonic() - started

        assert elapsed >= 0.05 * 4 * 0.9  # 多少の誤差を許容
        assert elapsed < 1.0  # 待ちすぎてもいない

    def test_ゼロ以下の上限は拒否する(self) -> None:
        with pytest.raises(ValueError):
            RateLimiter(per_minute=0)


class TestInterval:
    def test_日足のみ対応する(self) -> None:
        """J-Quants に分足は存在しない。"""
        source = _source()
        assert source.supports_interval("1d")
        assert not source.supports_interval("5m")
        assert not source.supports_interval("1m")

    def test_分足を要求されたらエラーにする(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, [])
        source = _source()
        with pytest.raises(DataSourceError, match="日足のみ"):
            source.get_bars("7203", "5m", date(2026, 1, 1), date(2026, 1, 10))

    def test_APIキーが空なら拒否する(self) -> None:
        with pytest.raises(ValueError):
            JQuantsDataSource("")


class TestPagination:
    def test_pagination_keyを辿って全件取得する(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1営業日の全銘柄（約4,000件）は複数ページに分かれる。"""
        fake = _install(
            monkeypatch,
            [
                _Response(
                    {
                        "data": [_quote("7203", "2026-06-01")],
                        "pagination_key": "p2",
                    }
                ),
                _Response({"data": [_quote("8306", "2026-06-01")]}),
            ],
        )
        source = _source()
        bars = source.get_bars_for_date(date(2026, 6, 1))

        assert set(bars) == {"7203", "8306"}
        assert len(fake.calls) == 2
        assert fake.calls[1]["params"]["pagination_key"] == "p2"

    def test_V2のエンドポイントを叩く(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """V1 のパス(prices/daily_quotes)は2026年6月1日に終了済み。

        実測で J-Quants が何も返さなかった原因がこれだった。
        """
        fake = _install(
            monkeypatch,
            [_Response({"data": [_quote("7203", "2026-06-01")]})],
        )
        _source().get_bars_for_date(date(2026, 6, 1))

        assert fake.calls[0]["url"].endswith(ENDPOINT_DAILY_BARS)
        assert "prices/daily_quotes" not in fake.calls[0]["url"]

    def test_銘柄一覧もV2のエンドポイントを叩く(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _install(
            monkeypatch,
            [_Response({"data": [{"Code": "7203", "CompanyName": "トヨタ"}]})],
        )
        _source().list_symbols(date(2026, 6, 1))

        assert fake.calls[0]["url"].endswith(ENDPOINT_MASTER)
        assert "listed/info" not in fake.calls[0]["url"]

    def test_日付指定で全銘柄を取る(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """code= の銘柄ループより約4倍少ないリクエストで済む経路。"""
        fake = _install(
            monkeypatch,
            [_Response({"data": [_quote("7203", "2026-06-01")]})],
        )
        source = _source()
        source.get_bars_for_date(date(2026, 6, 1))

        assert fake.calls[0]["params"]["date"] == "2026-06-01"
        assert "code" not in fake.calls[0]["params"]


class TestAuth:
    def test_APIキーをヘッダに載せる(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _install(
            monkeypatch,
            [_Response({"data": [_quote("7203", "2026-06-01")]})],
        )
        _source("secret-key").get_bars_for_date(date(2026, 6, 1))
        assert fake.calls[0]["headers"]["x-api-key"] == "secret-key"

    def test_401はプラン登録の案内つきでエラーにする(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ユーザー登録だけでは使えず、Freeプランへの登録が別途必要。"""
        _install(monkeypatch, [_Response({}, status_code=401)])
        source = _source()
        with pytest.raises(DataSourceError, match="Freeプラン"):
            source.get_bars_for_date(date(2026, 6, 1))

    def test_429はRateLimitErrorとして区別する(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**「データなし」と混同してはならない。**

        測定ループがこれを「その日はデータなし」として飛ばすと測定値が嘘になる。
        実際に、データ終端日の実測が 84日から 88日にずれる事故を起こした。
        """
        _install(monkeypatch, [_Response({}, status_code=429)] * 5)
        with pytest.raises(RateLimitError):
            _source(max_rate_limit_retries=0).get_bars_for_date(date(2026, 6, 1))

    def test_RateLimitErrorは空応答と別の型である(self) -> None:
        """呼び出し側が両者を区別できること。"""
        assert issubclass(RateLimitError, DataSourceError)
        assert not issubclass(RateLimitError, EmptyResponseError)
        assert not issubclass(EmptyResponseError, RateLimitError)

    def test_429は再試行してから諦める(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """サーバ側の制限方式は公開されていないため、回復可能にしておく。"""
        fake = _install(monkeypatch, [_Response({}, status_code=429)] * 5)
        with pytest.raises(RateLimitError):
            _source(max_rate_limit_retries=2).get_bars_for_date(date(2026, 6, 1))

        assert len(fake.calls) == 3  # 初回 + 再試行2回

    def test_再試行で回復すれば成功する(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            [
                _Response({}, status_code=429),
                _Response({"data": [_quote("7203", "2026-06-01")]}),
            ],
        )
        bars = _source(max_rate_limit_retries=2).get_bars_for_date(date(2026, 6, 1))
        assert "7203" in bars


class TestEmptyResponse:
    def test_空応答は遅延の可能性を示して例外にする(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, [_Response({"data": []})])
        source = _source()
        with pytest.raises(EmptyResponseError, match=str(FREE_PLAN_DELAY_DAYS)):
            source.get_bars_for_date(date(2026, 6, 1))


class TestSymbols:
    def test_日付指定で銘柄一覧を取得する(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """サバイバーシップバイアス回避の要。現在の一覧を過去に適用してはならない。"""
        fake = _install(
            monkeypatch,
            [
                _Response(
                    {
                        "data": [
                            {"Code": "72030", "CompanyName": "トヨタ自動車"},
                            {"Code": "8306", "CompanyName": "三菱UFJ"},
                        ]
                    }
                )
            ],
        )
        source = _source()
        symbols = source.list_symbols(date(2026, 3, 31))

        assert symbols is not None
        assert fake.calls[0]["params"]["date"] == "2026-03-31"
        # 5桁コード（末尾0）は4桁に正規化する
        assert symbols[0].code == "7203"
        assert symbols[1].code == "8306"
        assert all(s.lot_size == 100 for s in symbols)


class TestBarConversion:
    def test_調整済みの値を優先する(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未調整の値を使うと分割の前後で価格が不連続になる。"""
        _install(
            monkeypatch,
            [
                _Response(
                    {
                        "data": [
                            {
                                "Code": "7203",
                                "Date": "2026-06-01",
                                "Open": 999.0,
                                "Close": 999.0,
                                "High": 999.0,
                                "Low": 999.0,
                                "Volume": 1,
                                "AdjustmentOpen": 100.0,
                                "AdjustmentHigh": 101.0,
                                "AdjustmentLow": 99.0,
                                "AdjustmentClose": 100.5,
                                "AdjustmentVolume": 10000,
                            }
                        ]
                    }
                )
            ],
        )
        bars = _source().get_bars_for_date(date(2026, 6, 1))
        assert bars["7203"][0].close == 100.5  # 調整済みの値

    def test_V2の短縮項目名を解釈できる(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """V2 は項目名が短縮された（Open→O、Close→C、調整済みは AdjC 等）。"""
        _install(monkeypatch, [_Response({"data": [_quote("7203", "2026-06-01")]})])
        bars = _source().get_bars_for_date(date(2026, 6, 1))
        assert bars["7203"][0].close == 100.0

    def test_V1形式が返っても解釈できる(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """候補キー方式なので旧形式でも壊れない。

        V2 の短縮形の正確な綴りは公開情報からの推定を含むため、
        取りこぼしても動くようにしてある。
        """
        _install(monkeypatch, [_Response({"data": [_v1_quote("7203", "2026-06-01")]})])
        bars = _source().get_bars_for_date(date(2026, 6, 1))
        assert bars["7203"][0].close == 100.0

    def test_項目名が全く違えば実際のキーを添えて例外にする(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """推測が外れたときに、実データの項目名が分かるようにする。"""
        _install(
            monkeypatch,
            [_Response({"data": [{"Unknown1": 1, "Unknown2": 2}]})],
        )
        with pytest.raises(EmptyResponseError, match="Unknown1"):
            _source().get_bars_for_date(date(2026, 6, 1))

    def test_404はパス変更の可能性を示す(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """失敗理由を握り潰さず、原因の切り分けができるようにする。"""
        _install(monkeypatch, [_Response({}, status_code=404)])
        with pytest.raises(DataSourceError, match="V2"):
            _source().get_bars_for_date(date(2026, 6, 1))

    def test_欠損を含むレコードは落とす(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            [
                _Response(
                    {
                        "data": [
                            {"Code": "7203", "Date": "2026-06-01", "Close": None},
                            _quote("8306", "2026-06-01"),
                        ]
                    }
                )
            ],
        )
        bars = _source().get_bars_for_date(date(2026, 6, 1))
        assert set(bars) == {"8306"}


# 実際に返ってきた 400 のメッセージ（2026-08-23 に確認）。
# 推測した形式ではなく実物をテストデータに使う。
_REAL_400_BODY = (
    '{"message": "Your subscription covers the following dates: '
    "2024-05-31 ~ 2026-05-31. If you want more data, please check other plans:"
    'https://jpx-jquants.com/#dataset"}'
)


class TestSubscriptionRange:
    """契約範囲外の照会を減らす。

    5件/分の制約下では、範囲外を叩き続けるのは予算の無駄。
    Phase 2 の一括収集（2年分の営業日ループ）では特に効く。
    """

    def test_実際のメッセージから範囲を抽出できる(self) -> None:
        covered = parse_subscription_range(_REAL_400_BODY)
        assert covered == (date(2024, 5, 31), date(2026, 5, 31))

    def test_形式が違えばNoneを返す(self) -> None:
        """抽出できないことを許容する。分からなければ都度リクエストするだけ。"""
        assert parse_subscription_range('{"message": "Bad Request"}') is None

    def test_400で範囲を学習しSubscriptionRangeErrorになる(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, [_Response(_REAL_400_BODY, status_code=400)])
        source = _source()

        with pytest.raises(SubscriptionRangeError) as exc_info:
            source.get_bars_for_date(date(2026, 6, 3))

        assert exc_info.value.has_range
        assert exc_info.value.covered_to == date(2026, 5, 31)
        assert source.subscription_range == (date(2024, 5, 31), date(2026, 5, 31))

    def test_学習後は範囲外をリクエストせず弾く(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**送信前に弾くこと。** これが予算節約の本体。"""
        fake = _install(monkeypatch, [_Response(_REAL_400_BODY, status_code=400)])
        source = _source()

        with pytest.raises(SubscriptionRangeError):
            source.get_bars_for_date(date(2026, 6, 3))
        assert len(fake.calls) == 1

        # 2回目以降は範囲外と分かっているので送らない
        for day in (date(2026, 6, 2), date(2026, 6, 1)):
            with pytest.raises(SubscriptionRangeError):
                source.get_bars_for_date(day)
        assert len(fake.calls) == 1  # 増えていない

    def test_範囲内なら通常どおり照会する(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _install(
            monkeypatch,
            [
                _Response(_REAL_400_BODY, status_code=400),
                _Response({"data": [_quote("7203", "2026-05-29")]}),
            ],
        )
        source = _source()
        with pytest.raises(SubscriptionRangeError):
            source.get_bars_for_date(date(2026, 6, 3))

        bars = source.get_bars_for_date(date(2026, 5, 29))
        assert "7203" in bars
        assert len(fake.calls) == 2

    def test_範囲未判明なら弾かない(self) -> None:
        """叩いてみないと分からない状態では通す。"""
        source = _source()
        assert source.subscription_range is None
        assert source.covers(date(2020, 1, 1))

    def test_範囲を知らない形式の400は汎用エラーにする(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, [_Response('{"message": "Bad Request"}', status_code=400)])
        source = _source()
        with pytest.raises(DataSourceError) as exc_info:
            source.get_bars_for_date(date(2026, 6, 3))
        assert not isinstance(exc_info.value, SubscriptionRangeError)
