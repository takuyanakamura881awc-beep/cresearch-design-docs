"""J-Quants データソースのテスト。HTTP はモックする。"""

from __future__ import annotations

import sys
import time
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from autotrader.data.base import DataSourceError, EmptyResponseError
from autotrader.data.jquants import (
    FREE_PLAN_DELAY_DAYS,
    JQuantsDataSource,
    RateLimiter,
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


class TestRateLimiter:
    """API側の制限に当てず、こちらで待つこと。"""

    def test_上限内なら待たない(self) -> None:
        limiter = RateLimiter(per_minute=5)
        started = time.monotonic()
        for _ in range(5):
            limiter.acquire()
        assert time.monotonic() - started < 0.5

    def test_ゼロ以下の上限は拒否する(self) -> None:
        with pytest.raises(ValueError):
            RateLimiter(per_minute=0)


class TestInterval:
    def test_日足のみ対応する(self) -> None:
        """J-Quants に分足は存在しない。"""
        source = JQuantsDataSource("key")
        assert source.supports_interval("1d")
        assert not source.supports_interval("5m")
        assert not source.supports_interval("1m")

    def test_分足を要求されたらエラーにする(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, [])
        source = JQuantsDataSource("key")
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
                        "daily_quotes": [_quote("7203", "2026-06-01")],
                        "pagination_key": "p2",
                    }
                ),
                _Response({"daily_quotes": [_quote("8306", "2026-06-01")]}),
            ],
        )
        source = JQuantsDataSource("key")
        bars = source.get_bars_for_date(date(2026, 6, 1))

        assert set(bars) == {"7203", "8306"}
        assert len(fake.calls) == 2
        assert fake.calls[1]["params"]["pagination_key"] == "p2"

    def test_日付指定で全銘柄を取る(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """code= の銘柄ループより約4倍少ないリクエストで済む経路。"""
        fake = _install(
            monkeypatch,
            [_Response({"daily_quotes": [_quote("7203", "2026-06-01")]})],
        )
        source = JQuantsDataSource("key")
        source.get_bars_for_date(date(2026, 6, 1))

        assert fake.calls[0]["params"]["date"] == "2026-06-01"
        assert "code" not in fake.calls[0]["params"]


class TestAuth:
    def test_APIキーをヘッダに載せる(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _install(
            monkeypatch,
            [_Response({"daily_quotes": [_quote("7203", "2026-06-01")]})],
        )
        JQuantsDataSource("secret-key").get_bars_for_date(date(2026, 6, 1))
        assert fake.calls[0]["headers"]["x-api-key"] == "secret-key"

    def test_401はプラン登録の案内つきでエラーにする(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ユーザー登録だけでは使えず、Freeプランへの登録が別途必要。"""
        _install(monkeypatch, [_Response({}, status_code=401)])
        source = JQuantsDataSource("key")
        with pytest.raises(DataSourceError, match="Freeプラン"):
            source.get_bars_for_date(date(2026, 6, 1))

    def test_429はレート制限として区別する(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, [_Response({}, status_code=429)])
        source = JQuantsDataSource("key")
        with pytest.raises(DataSourceError, match="レート制限"):
            source.get_bars_for_date(date(2026, 6, 1))


class TestEmptyResponse:
    def test_空応答は遅延の可能性を示して例外にする(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, [_Response({"daily_quotes": []})])
        source = JQuantsDataSource("key")
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
                        "info": [
                            {"Code": "72030", "CompanyName": "トヨタ自動車"},
                            {"Code": "8306", "CompanyName": "三菱UFJ"},
                        ]
                    }
                )
            ],
        )
        source = JQuantsDataSource("key")
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
                        "daily_quotes": [
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
        bars = JQuantsDataSource("key").get_bars_for_date(date(2026, 6, 1))
        assert bars["7203"][0].close == 100.5  # 調整済みの値

    def test_欠損を含むレコードは落とす(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            [
                _Response(
                    {
                        "daily_quotes": [
                            {"Code": "7203", "Date": "2026-06-01", "Close": None},
                            _quote("8306", "2026-06-01"),
                        ]
                    }
                )
            ],
        )
        bars = JQuantsDataSource("key").get_bars_for_date(date(2026, 6, 1))
        assert set(bars) == {"8306"}
