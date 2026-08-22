"""Layer 1（ユニバース構築）のテスト。

50万円という資金量から来る制約が正しく効いているかを重点的に確認する。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from autotrader.data.base import BarDataSource
from autotrader.types import Bar, PriceTier, Symbol
from autotrader.universe.builder import bars_lookback_start, build
from autotrader.universe.filters import (
    FilterConfig,
    RejectReason,
    average_turnover,
    classify_price_tier,
    is_loanable,
    passes_liquidity,
    passes_market,
    screen,
)


def _symbol(
    code: str = "7203",
    market: str | None = "プライム",
    margin: str | None = "貸借",
) -> Symbol:
    return Symbol(code=code, name="テスト", market=market, margin_type=margin)


def _bars(
    close: float = 1500.0,
    turnover: float | None = 2_000_000_000.0,
    n: int = 25,
    limit_up: bool = False,
    limit_down: bool = False,
    volume: int = 100_000,
) -> tuple[Bar, ...]:
    return tuple(
        Bar(
            symbol="7203",
            timestamp=datetime(2026, 5, 1) + timedelta(days=i),
            open=close,
            high=close * 1.01,
            low=close * 0.99,
            close=close,
            volume=volume,
            turnover=turnover,
            limit_up=limit_up,
            limit_down=limit_down,
        )
        for i in range(n)
    )


class TestPriceTier:
    """**50万円という資金では、単元100株が最も厳しい制約になる。**

    株価P円の1単元 = 100P円。3,000円の銘柄は1単元30万円で資金の60%を占める。
    """

    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            (299.0, None),  # 1ティック(1円)が0.33%以上でコスト過大
            (300.0, PriceTier.NORMAL),
            (1500.0, PriceTier.NORMAL),  # 1単元15万円。理想的
            (2000.0, PriceTier.NORMAL),
            (2000.5, PriceTier.PREMIUM),
            (3000.0, PriceTier.PREMIUM),  # 1単元30万円。資金の60%
            (3000.5, None),  # 分散が成立しない
            (10000.0, None),
        ],
    )
    def test_境界値で正しく枠を判定する(
        self, price: float, expected: PriceTier | None
    ) -> None:
        assert classify_price_tier(price) == expected

    def test_有名な高株価銘柄は対象外になる(self) -> None:
        """50万円ではソニーやキーエンスのような銘柄は扱えない。

        「有名企業を売買する」のではなく「株価帯が資金に合う銘柄から統計的に選ぶ」
        のが本質（docs/03-universe.md §2）。
        """
        assert classify_price_tier(4000.0) is None
        assert classify_price_tier(60000.0) is None

    def test_資金が増えれば上限を緩められる(self) -> None:
        """株価レンジは資金量の関数。"""
        assert classify_price_tier(5000.0) is None
        assert classify_price_tier(5000.0, premium_max=8000) == PriceTier.PREMIUM


class TestLiquidity:
    def test_売買代金が閾値以上なら通る(self) -> None:
        assert passes_liquidity(Decimal(1_000_000_000))
        assert passes_liquidity(Decimal(5_000_000_000))

    def test_薄い銘柄は落とす(self) -> None:
        """自分の注文が板を動かすと約定モデルが成立しない。"""
        assert not passes_liquidity(Decimal(999_999_999))

    def test_実売買代金があればそれを使う(self) -> None:
        """J-Quants の Va。終値×出来高の近似より正確。"""
        bars = _bars(close=1000.0, turnover=3_000_000_000.0, n=20)
        assert average_turnover(bars) == Decimal(3_000_000_000)

    def test_売買代金がなければ終値かける出来高で近似する(self) -> None:
        """yfinance 由来のバーには売買代金がない。"""
        bars = _bars(close=1000.0, turnover=None, volume=5000, n=20)
        assert average_turnover(bars) == Decimal(5_000_000)

    def test_日数が足りなければNoneを返す(self) -> None:
        """少ない日数の平均は流動性の判定として信用できない。

        足りないのに計算して返すと、上場直後の銘柄が誤って通る。
        """
        assert average_turnover(_bars(n=19), lookback_days=20) is None
        assert average_turnover(_bars(n=20), lookback_days=20) is not None


class TestMarketAndMargin:
    def test_プライムのみ通す(self) -> None:
        assert passes_market(_symbol(market="プライム"))
        assert not passes_market(_symbol(market="スタンダード"))

    def test_ETFは市場区分その他なので落ちる(self) -> None:
        """実測で確認: ETF は MktNm が「その他」になる。"""
        assert not passes_market(_symbol(code="1306", market="その他"))

    def test_貸借銘柄なら売建できる(self) -> None:
        """流動性による代理は不要。MrgnNm で直接判定できる。"""
        assert is_loanable(_symbol(margin="貸借"))
        assert not is_loanable(_symbol(margin="信用"))
        assert not is_loanable(_symbol(margin=None))


class TestScreen:
    def test_条件を満たせば通過する(self) -> None:
        result = screen(_symbol(), _bars())
        assert result.passed
        assert result.reason is None
        assert result.tier is PriceTier.NORMAL

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"market": "スタンダード"}, RejectReason.MARKET),
            ({"margin": "信用"}, RejectReason.NOT_LOANABLE),
        ],
    )
    def test_銘柄属性で落ちる(self, kwargs: dict[str, str], expected: RejectReason) -> None:
        result = screen(_symbol(**kwargs), _bars())
        assert not result.passed
        assert result.reason is expected

    def test_株価が低すぎれば落ちる(self) -> None:
        result = screen(_symbol(), _bars(close=250.0))
        assert result.reason is RejectReason.PRICE_TOO_LOW

    def test_株価が高すぎれば落ちる(self) -> None:
        result = screen(_symbol(), _bars(close=5000.0))
        assert result.reason is RejectReason.PRICE_TOO_HIGH

    def test_流動性が足りなければ落ちる(self) -> None:
        result = screen(_symbol(), _bars(turnover=100_000_000.0))
        assert result.reason is RejectReason.ILLIQUID

    def test_ストップ高安は落ちる(self) -> None:
        """約定できないため。J-Quants の UL/LL で判定する。"""
        assert screen(_symbol(), _bars(limit_up=True)).reason is RejectReason.LIMIT_HIT
        assert screen(_symbol(), _bars(limit_down=True)).reason is RejectReason.LIMIT_HIT

    def test_出来高ゼロは売買停止とみなす(self) -> None:
        assert screen(_symbol(), _bars(volume=0)).reason is RejectReason.HALTED

    def test_日足がなければ落ちる(self) -> None:
        assert screen(_symbol(), ()).reason is RejectReason.NO_DATA

    def test_上場後3ヶ月未満は落ちる(self) -> None:
        """値動きの統計が取れない。"""
        result = screen(_symbol(), _bars(), days_since_listing=30)
        assert result.reason is RejectReason.TOO_NEW

    def test_決算前後は落ちる(self) -> None:
        """ギャップリスクが予測不能。前後どちらも除外する。"""
        assert screen(_symbol(), _bars(), days_to_earnings=1).reason is RejectReason.EARNINGS
        assert screen(_symbol(), _bars(), days_to_earnings=-1).reason is RejectReason.EARNINGS
        assert screen(_symbol(), _bars(), days_to_earnings=10).passed

    def test_空売り規制中は落ちる(self) -> None:
        result = screen(_symbol(), _bars(), short_restricted=True)
        assert result.reason is RejectReason.SHORT_RESTRICTED

    def test_安い判定を先に行う(self) -> None:
        """市場区分は銘柄一覧だけで分かる。売買代金の計算より先に落とす。

        日足が無くても市場区分で落ちるなら NO_DATA にはならない。
        """
        result = screen(_symbol(market="グロース"), ())
        assert result.reason is RejectReason.MARKET


class TestFilterConfig:
    def test_株価レンジの大小関係を検証する(self) -> None:
        with pytest.raises(ValueError):
            FilterConfig(price_hard_min=3000, price_normal_max=2000)

    def test_遡及日数はゼロを許さない(self) -> None:
        with pytest.raises(ValueError):
            FilterConfig(turnover_lookback_days=0)


class _StubSource(BarDataSource):
    """銘柄一覧を返すだけのデータソース。"""

    def __init__(self, symbols: tuple[Symbol, ...] | None) -> None:
        self._symbols = symbols

    @property
    def name(self) -> str:
        return "stub"

    def supports_interval(self, interval: str) -> bool:
        return interval == "1d"

    def get_bars(
        self, symbol: str, interval: str, start: date, end: date
    ) -> tuple[Bar, ...]:
        return ()

    def list_symbols(self, as_of: date) -> tuple[Symbol, ...] | None:
        return self._symbols


class TestBuild:
    def test_通過と除外を内訳つきで返す(self) -> None:
        symbols = (
            _symbol("7203"),
            _symbol("1306", market="その他"),
            _symbol("9999", margin="信用"),
        )
        bars = {"7203": _bars(), "1306": _bars(), "9999": _bars()}

        snapshot = build(date(2026, 5, 29), _StubSource(symbols), bars_by_symbol=bars)

        assert snapshot.symbols == ("7203",)
        assert snapshot.total_listed == 3
        assert snapshot.reject_counts[RejectReason.MARKET] == 1
        assert snapshot.reject_counts[RejectReason.NOT_LOANABLE] == 1

    def test_銘柄一覧を提供しないソースは拒否する(self) -> None:
        """サバイバーシップ回避には日付指定の一覧が必須。"""
        with pytest.raises(ValueError, match="銘柄一覧"):
            build(date(2026, 5, 29), _StubSource(None))

    def test_内訳を人が読める形にできる(self) -> None:
        symbols = (_symbol("7203"), _symbol("1306", market="その他"))
        snapshot = build(
            date(2026, 5, 29),
            _StubSource(symbols),
            bars_by_symbol={"7203": _bars(), "1306": _bars()},
        )
        text = snapshot.summary()
        assert "全上場 2" in text
        assert "market" in text

    def test_日足がなければ市場と信用区分だけで絞る(self) -> None:
        """一括取得の前に、軽い条件で母集団を減らすために使う。"""
        symbols = (_symbol("7203"), _symbol("1306", market="その他"))
        snapshot = build(date(2026, 5, 29), _StubSource(symbols))

        # 日足がないので 7203 も NO_DATA で落ちる
        assert snapshot.size == 0
        assert snapshot.reject_counts[RejectReason.MARKET] == 1
        assert snapshot.reject_counts[RejectReason.NO_DATA] == 1


class TestLookbackStart:
    def test_20営業日ぶんの暦日を確保する(self) -> None:
        """土日祝があるので営業日数より広く取る。"""
        as_of = date(2026, 5, 29)
        start = bars_lookback_start(as_of)
        assert (as_of - start).days >= 30
