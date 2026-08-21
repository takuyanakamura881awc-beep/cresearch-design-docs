"""フォールバック構成とストアのテスト。"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import pytest

from autotrader.data.base import (
    BarDataSource,
    DataSourceError,
    EmptyResponseError,
    FallbackDataSource,
)
from autotrader.data.store import BarStore
from autotrader.types import Bar, Symbol


class _StubSource(BarDataSource):
    """テスト用のデータソース。成功/失敗を指定できる。"""

    def __init__(
        self,
        name: str,
        *,
        bars: tuple[Bar, ...] | None = None,
        error: Exception | None = None,
        intervals: tuple[str, ...] = ("1d",),
        symbols: tuple[Symbol, ...] | None = None,
    ) -> None:
        self._name = name
        self._bars = bars or ()
        self._error = error
        self._intervals = intervals
        self._symbols = symbols

    @property
    def name(self) -> str:
        return self._name

    def supports_interval(self, interval: str) -> bool:
        return interval in self._intervals

    def get_bars(
        self, symbol: str, interval: str, start: date, end: date
    ) -> tuple[Bar, ...]:
        if self._error is not None:
            raise self._error
        return self._bars

    def list_symbols(self, as_of: date) -> tuple[Symbol, ...] | None:
        return self._symbols


def _bar(day: int = 1, close: float = 100.0) -> Bar:
    return Bar(
        symbol="7203",
        timestamp=datetime(2026, 6, day, 15, 0),
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        volume=10000,
    )


class TestFallback:
    def test_前段が成功すればそれを返す(self) -> None:
        primary = _StubSource("jquants", bars=(_bar(close=100.0),))
        secondary = _StubSource("yahoo", bars=(_bar(close=200.0),))
        source = FallbackDataSource([primary, secondary])

        bars = source.get_bars("7203", "1d", date(2026, 6, 1), date(2026, 6, 2))
        assert bars[0].close == 100.0

    def test_前段が失敗したら次段に切り替える(self) -> None:
        primary = _StubSource("jquants", error=EmptyResponseError("空"))
        secondary = _StubSource("yahoo", bars=(_bar(close=200.0),))
        source = FallbackDataSource([primary, secondary])

        bars = source.get_bars("7203", "1d", date(2026, 6, 1), date(2026, 6, 2))
        assert bars[0].close == 200.0

    def test_フォールバックの発動をログに残す(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """黙って切り替わると、データの品質差に気づけない。

        J-Quants（JPX公式）と yfinance（非公式）では分割調整の扱いが異なりうるため、
        どちらが使われたかが追えないと、後から異常を診断できなくなる。
        """
        primary = _StubSource("jquants", error=EmptyResponseError("空"))
        secondary = _StubSource("yahoo", bars=(_bar(),))
        source = FallbackDataSource([primary, secondary])

        with caplog.at_level(logging.WARNING):
            source.get_bars("7203", "1d", date(2026, 6, 1), date(2026, 6, 2))

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "jquants" in messages
        assert "yahoo" in messages

    def test_全て失敗したら例外を投げる(self) -> None:
        source = FallbackDataSource(
            [
                _StubSource("a", error=EmptyResponseError("空a")),
                _StubSource("b", error=EmptyResponseError("空b")),
            ]
        )
        with pytest.raises(DataSourceError, match="全データソースが失敗"):
            source.get_bars("7203", "1d", date(2026, 6, 1), date(2026, 6, 2))

    def test_対応しない足のソースは飛ばす(self) -> None:
        """5分足に対応しない J-Quants を飛ばして yahoo に行く。"""
        jq = _StubSource("jquants", intervals=("1d",), bars=(_bar(close=100.0),))
        yh = _StubSource("yahoo", intervals=("1d", "5m"), bars=(_bar(close=200.0),))
        source = FallbackDataSource([jq, yh])

        bars = source.get_bars("7203", "5m", date(2026, 6, 1), date(2026, 6, 2))
        assert bars[0].close == 200.0

    def test_誰も対応しない足はエラー(self) -> None:
        source = FallbackDataSource([_StubSource("jquants", intervals=("1d",))])
        with pytest.raises(DataSourceError, match="対応するデータソースがない"):
            source.get_bars("7203", "1m", date(2026, 6, 1), date(2026, 6, 2))

    def test_空のソース列は拒否する(self) -> None:
        with pytest.raises(ValueError):
            FallbackDataSource([])

    def test_銘柄一覧は提供できる最初のソースに委譲する(self) -> None:
        """yfinance は銘柄一覧を持たないので None を返し、J-Quants に回る。"""
        yh = _StubSource("yahoo", symbols=None)
        jq = _StubSource("jquants", symbols=(Symbol(code="7203", name="トヨタ"),))
        source = FallbackDataSource([yh, jq])

        symbols = source.list_symbols(date(2026, 6, 1))
        assert symbols is not None
        assert symbols[0].code == "7203"

    def test_誰も銘柄一覧を持たなければNone(self) -> None:
        source = FallbackDataSource([_StubSource("yahoo")])
        assert source.list_symbols(date(2026, 6, 1)) is None


class TestBarStore:
    def test_書き込んだバーを読み出せる(self, tmp_path: Path) -> None:
        store = BarStore(tmp_path)
        store.write("7203", "1d", (_bar(1), _bar(2)))

        bars = store.read("7203", "1d")
        assert len(bars) == 2
        assert bars[0].symbol == "7203"

    def test_同じ期間を再取得しても壊れない(self, tmp_path: Path) -> None:
        """冪等。再実行が安全でないと、失敗したバッチをやり直せない。"""
        store = BarStore(tmp_path)
        store.write("7203", "1d", (_bar(1), _bar(2)))
        total = store.write("7203", "1d", (_bar(1), _bar(2)))

        assert total == 2  # 重複しない

    def test_同じ時刻は新しい値で上書きする(self, tmp_path: Path) -> None:
        store = BarStore(tmp_path)
        store.write("7203", "1d", (_bar(1, close=100.0),))
        store.write("7203", "1d", (_bar(1, close=150.0),))

        bars = store.read("7203", "1d")
        assert len(bars) == 1
        assert bars[0].close == 150.0

    def test_期間を指定して読み出せる(self, tmp_path: Path) -> None:
        store = BarStore(tmp_path)
        store.write("7203", "1d", (_bar(1), _bar(5), _bar(10)))

        bars = store.read("7203", "1d", date(2026, 6, 4), date(2026, 6, 6))
        assert len(bars) == 1
        assert bars[0].timestamp.day == 5

    def test_データがなければ空を返す(self, tmp_path: Path) -> None:
        assert BarStore(tmp_path).read("9999", "1d") == ()

    def test_空のバー列を書いても壊れない(self, tmp_path: Path) -> None:
        assert BarStore(tmp_path).write("7203", "1d", ()) == 0


class TestTTL:
    def test_記録がなければ新鮮ではない(self, tmp_path: Path) -> None:
        store = BarStore(tmp_path)
        assert not store.is_fresh("7203", "1d", date(2026, 6, 1), date(2026, 6, 2))

    def test_TTL内なら新鮮(self, tmp_path: Path) -> None:
        store = BarStore(tmp_path, ttl_days=1)
        now = datetime(2026, 6, 10, 12, 0)
        store.record_fetch(
            "7203", "1d", date(2026, 6, 1), date(2026, 6, 2), "jquants", 2, now=now
        )
        assert store.is_fresh(
            "7203", "1d", date(2026, 6, 1), date(2026, 6, 2),
            now=datetime(2026, 6, 10, 20, 0),
        )

    def test_TTLを過ぎたら新鮮ではない(self, tmp_path: Path) -> None:
        store = BarStore(tmp_path, ttl_days=1)
        now = datetime(2026, 6, 10, 12, 0)
        store.record_fetch(
            "7203", "1d", date(2026, 6, 1), date(2026, 6, 2), "jquants", 2, now=now
        )
        assert not store.is_fresh(
            "7203", "1d", date(2026, 6, 1), date(2026, 6, 2),
            now=datetime(2026, 6, 12, 12, 0),
        )

    def test_どのソースから取ったかを追跡できる(self, tmp_path: Path) -> None:
        """フォールバック発動時に、後から品質差を診断するために必要。"""
        store = BarStore(tmp_path)
        store.record_fetch(
            "7203", "1d", date(2026, 6, 1), date(2026, 6, 2), "yahoo", 2
        )
        history = store.sources_used("7203", "1d")
        assert history == [("2026-06-01", "2026-06-02", "yahoo")]


class TestCoverage:
    def test_蓄積期間を取得できる(self, tmp_path: Path) -> None:
        """5分足の蓄積量が Stage A の検証期間を律速するため、進捗の可視化に使う。"""
        store = BarStore(tmp_path)
        store.write("7203", "5m", (_bar(1), _bar(10)))

        coverage = store.coverage("7203", "5m")
        assert coverage == (date(2026, 6, 1), date(2026, 6, 10))

    def test_データがなければNone(self, tmp_path: Path) -> None:
        assert BarStore(tmp_path).coverage("9999", "5m") is None

    def test_保存済み銘柄を列挙できる(self, tmp_path: Path) -> None:
        store = BarStore(tmp_path)
        store.write("7203", "1d", (_bar(),))
        store.write("8306", "1d", (_bar(),))

        assert store.symbols("1d") == ("7203", "8306")
        assert store.symbols("5m") == ()
