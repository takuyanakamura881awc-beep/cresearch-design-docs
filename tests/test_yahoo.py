"""yfinance データソースのテスト。

**yfinance 自体はモックする。** 外部APIを叩くテストは、ネットワークの都合で
落ちたりレート制限にかかったりして、テストの信頼性を損なう。

ただし記事の指摘通り「**モックだけでは、いつの間にか壊れていたを検出できない**」。
実データでの確認は ``scripts/verify_data_sources.py`` が担う。
この役割分担でカバーする。
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from autotrader.data.base import EmptyResponseError, LookbackExceededError
from autotrader.data.yahoo import (
    MAX_LOOKBACK_DAYS,
    YahooDataSource,
    check_lookback,
    from_ticker,
    to_ticker,
)


def _frame(rows: int = 3, start_hour: int = 9) -> pd.DataFrame:
    """yfinance が返す形の DataFrame（単一銘柄）。"""
    return pd.DataFrame(
        {
            "Open": [100.0 + i for i in range(rows)],
            "High": [101.0 + i for i in range(rows)],
            "Low": [99.0 + i for i in range(rows)],
            "Close": [100.5 + i for i in range(rows)],
            "Volume": [1000 + i for i in range(rows)],
        },
        index=pd.to_datetime(
            [datetime(2026, 6, 1, start_hour + i) for i in range(rows)]
        ),
    )


class _FakeYFinance(SimpleNamespace):
    """yfinance モジュールの差し替え。呼び出し引数を記録する。"""

    def __init__(self, frame: Any) -> None:
        super().__init__()
        self.frame = frame
        self.calls: list[dict[str, Any]] = []
        self.tz_cache_location: str | None = None

    def download(self, tickers: Any, **kwargs: Any) -> Any:
        self.calls.append({"tickers": tickers, **kwargs})
        return self.frame

    def set_tz_cache_location(self, path: str) -> None:
        self.tz_cache_location = path


@pytest.fixture
def fake_yf(monkeypatch: pytest.MonkeyPatch) -> _FakeYFinance:
    fake = _FakeYFinance(_frame())
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    # tzキャッシュ初期化フラグをリセットして、毎テストで初期化を通す
    monkeypatch.setattr("autotrader.data.yahoo._tz_cache_initialized", False)
    return fake


class TestTicker:
    def test_日本株はサフィックスTを付ける(self) -> None:
        assert to_ticker("7203") == "7203.T"

    def test_既にサフィックスがあれば二重に付けない(self) -> None:
        assert to_ticker("7203.T") == "7203.T"

    def test_サフィックスを外して銘柄コードに戻せる(self) -> None:
        assert from_ticker("7203.T") == "7203"

    def test_空文字は拒否する(self) -> None:
        with pytest.raises(ValueError):
            to_ticker("  ")


class TestLookback:
    """取得可能期間の超過を黙って切り詰めないこと。

    切り詰めると「取れたつもりで欠損している」状態になり、
    バックテストの前提が静かに壊れる。
    """

    def test_1分足は7日を超えるとエラー(self) -> None:
        today = date(2026, 8, 21)
        with pytest.raises(LookbackExceededError):
            check_lookback("1m", date(2026, 8, 1), today=today)

    def test_1分足は7日以内なら通る(self) -> None:
        today = date(2026, 8, 21)
        check_lookback("1m", date(2026, 8, 18), today=today)

    def test_5分足は60日を超えるとエラー(self) -> None:
        today = date(2026, 8, 21)
        with pytest.raises(LookbackExceededError):
            check_lookback("5m", date(2026, 5, 1), today=today)

    def test_日足は制限がないので何年前でも通る(self) -> None:
        check_lookback("1d", date(2010, 1, 1), today=date(2026, 8, 21))

    def test_定数が公開仕様と一致している(self) -> None:
        """実機で異なった場合はこの定数を修正する（実測は verify スクリプト）。"""
        assert MAX_LOOKBACK_DAYS["1m"] == 7
        assert MAX_LOOKBACK_DAYS["5m"] == 60


class TestAutoAdjust:
    def test_auto_adjustが必ずTrueで渡される(self, fake_yf: _FakeYFinance) -> None:
        """分割調整。無効化すると分割前後で価格が不連続になり、
        ATR%や売買代金の計算が壊れて架空の急騰を誤検出する。
        """
        source = YahooDataSource()
        source.get_bars_batch(("7203",), "1d", date(2026, 6, 1), date(2026, 6, 10))

        assert fake_yf.calls
        assert all(c["auto_adjust"] is True for c in fake_yf.calls)


class TestEmptyResponse:
    """空の DataFrame を握り潰さないこと。

    yfinance はブロックされても例外を投げず空を返す。
    「データなし」と「ブロック」が区別できない以上、安全側（例外）に倒す。
    """

    def test_全バッチが空なら例外を投げる(self, fake_yf: _FakeYFinance) -> None:
        fake_yf.frame = pd.DataFrame()
        source = YahooDataSource()
        with pytest.raises(EmptyResponseError):
            source.get_bars_batch(("7203",), "1d", date(2026, 6, 1), date(2026, 6, 10))

    def test_Noneが返っても例外を投げる(self, fake_yf: _FakeYFinance) -> None:
        fake_yf.frame = None
        source = YahooDataSource()
        with pytest.raises(EmptyResponseError):
            source.get_bars_batch(("7203",), "1d", date(2026, 6, 1), date(2026, 6, 10))

    def test_単一銘柄取得でも空なら例外(self, fake_yf: _FakeYFinance) -> None:
        fake_yf.frame = pd.DataFrame()
        source = YahooDataSource()
        with pytest.raises(EmptyResponseError):
            source.get_bars("7203", "1d", date(2026, 6, 1), date(2026, 6, 10))


class TestBatching:
    def test_バッチサイズごとに分割して呼ぶ(self, fake_yf: _FakeYFinance) -> None:
        """短時間に大量のリクエストを投げるとIPブロックされるため。"""
        source = YahooDataSource(batch_size=2, batch_interval_seconds=0.0)
        symbols = tuple(str(7200 + i) for i in range(5))
        source.get_bars_batch(symbols, "1d", date(2026, 6, 1), date(2026, 6, 10))

        # 5銘柄をバッチ2で分けるので3回
        assert len(fake_yf.calls) == 3

    def test_銘柄が空なら呼ばない(self, fake_yf: _FakeYFinance) -> None:
        source = YahooDataSource()
        assert source.get_bars_batch((), "1d", date(2026, 6, 1), date(2026, 6, 10)) == {}
        assert not fake_yf.calls


class TestMissingValues:
    def test_NaNを含む行はスキップする(self, fake_yf: _FakeYFinance) -> None:
        """欠損は None ではなく NaN。``is None`` では検出できない。"""
        frame = _frame(rows=3)
        frame.loc[frame.index[1], "Close"] = float("nan")
        fake_yf.frame = frame

        source = YahooDataSource()
        result = source.get_bars_batch(
            ("7203",), "1d", date(2026, 6, 1), date(2026, 6, 10)
        )
        assert len(result["7203"]) == 2  # NaN の行が落ちる


class TestTzCache:
    def test_tzキャッシュをプロセス固有の場所に隔離する(
        self, fake_yf: _FakeYFinance
    ) -> None:
        """複数プロセスが同じ SQLite を触ると OperationalError で落ちるため。"""
        source = YahooDataSource()
        source.get_bars_batch(("7203",), "1d", date(2026, 6, 1), date(2026, 6, 10))

        assert fake_yf.tz_cache_location is not None
        assert "py-yfinance-" in fake_yf.tz_cache_location


def _multiindex_frame(
    tickers: tuple[str, ...], rows: int = 3, ticker_level: int = 0
) -> pd.DataFrame:
    """yfinance が返す MultiIndex 列の DataFrame。

    Args:
        ticker_level: ティッカーを置く階層。0 なら (ticker, field)、1 なら (field, ticker)。
            ``group_by`` の指定や yfinance のバージョンで順序が変わるため両方を再現する。
    """
    data: dict[tuple[str, str], list[float]] = {}
    for t in tickers:
        for field, base in (
            ("Open", 100.0),
            ("High", 101.0),
            ("Low", 99.0),
            ("Close", 100.5),
            ("Volume", 1000.0),
        ):
            key = (t, field) if ticker_level == 0 else (field, t)
            data[key] = [base + i for i in range(rows)]

    index = pd.to_datetime([datetime(2026, 6, 1, 9 + i) for i in range(rows)])
    return pd.DataFrame(data, index=index)


class TestSingleTickerMultiIndex:
    """**単一銘柄でも MultiIndex が返る**ケースの回帰テスト。

    Phase 1 の実測で、1銘柄の取得だけが全滅する不具合を踏んだ。
    原因は `_parse` が銘柄数で分岐し、単一時にフラットな列を前提していたこと。
    yfinance は単一ティッカーでも MultiIndex を返すのが既定に変わっている。

    それまでのテストが3銘柄のケースしか見ていなかったため素通りした。
    """

    def test_単一銘柄でMultiIndexが返っても解釈できる(
        self, fake_yf: _FakeYFinance
    ) -> None:
        fake_yf.frame = _multiindex_frame(("7203.T",))
        source = YahooDataSource()
        result = source.get_bars_batch(
            ("7203",), "1d", date(2026, 6, 1), date(2026, 6, 10)
        )
        assert len(result["7203"]) == 3
        assert result["7203"][0].close == 100.5

    def test_ティッカーが第2階層にあっても解釈できる(
        self, fake_yf: _FakeYFinance
    ) -> None:
        """group_by の指定やバージョンで (field, ticker) の順になる場合。"""
        fake_yf.frame = _multiindex_frame(("7203.T",), ticker_level=1)
        source = YahooDataSource()
        result = source.get_bars_batch(
            ("7203",), "1d", date(2026, 6, 1), date(2026, 6, 10)
        )
        assert len(result["7203"]) == 3

    def test_複数銘柄のMultiIndexも解釈できる(self, fake_yf: _FakeYFinance) -> None:
        fake_yf.frame = _multiindex_frame(("7203.T", "8306.T"))
        source = YahooDataSource()
        result = source.get_bars_batch(
            ("7203", "8306"), "1d", date(2026, 6, 1), date(2026, 6, 10)
        )
        assert set(result) == {"7203", "8306"}

    def test_単一銘柄取得の経路でも空にならない(self, fake_yf: _FakeYFinance) -> None:
        """get_bars（単一銘柄用）が EmptyResponseError を誤って投げないこと。"""
        fake_yf.frame = _multiindex_frame(("7203.T",))
        bars = YahooDataSource().get_bars(
            "7203", "1d", date(2026, 6, 1), date(2026, 6, 10)
        )
        assert len(bars) == 3

    def test_含まれない銘柄は取得できない扱いになる(
        self, fake_yf: _FakeYFinance
    ) -> None:
        fake_yf.frame = _multiindex_frame(("7203.T",))
        source = YahooDataSource()
        result = source.get_bars_batch(
            ("7203", "9999"), "1d", date(2026, 6, 1), date(2026, 6, 10)
        )
        assert "7203" in result
        assert "9999" not in result


class TestInterval:
    def test_対応する足を正しく判定する(self) -> None:
        source = YahooDataSource()
        assert source.supports_interval("1d")
        assert source.supports_interval("5m")
        assert not source.supports_interval("tick")

    def test_ソース名を持つ(self) -> None:
        assert YahooDataSource().name == "yahoo"
