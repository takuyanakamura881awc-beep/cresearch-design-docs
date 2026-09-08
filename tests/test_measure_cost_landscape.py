"""scripts/measure_cost_landscape.py のテスト。

**この地図は仮説検定ではなく算術**なので、テストの役割も「偶然を弾く」
ではなく「算術が正しいこと」を固定することにある。

重点は3つ:

1. **往復コストが `autotrader.tick` と同じ値になること**
   （コストモデルを診断ごとに作り直していない）
2. **のこぎり波になること**——呼値の境界の直下が最安で、
   境界を超えると跳ねる。ここを取り違えると株価帯の判断が逆になる
3. **絞り込みが構造的な基準だけを見ること**（成績を見ない）
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from autotrader.tick import spread_yen

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "measure_cost_landscape.py"
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "measure_cost_landscape_script", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mcl() -> ModuleType:
    return _load_script()


class TestRoundTripBps:
    def test_tickモジュールと同じ値になる(self, mcl: ModuleType) -> None:
        """**コストモデルを診断ごとに作り直していない**ことの確認。"""
        for price in (500.0, 1_250.0, 3_000.0, 5_000.0):
            assert mcl.round_trip_bps(price) == pytest.approx(
                float(spread_yen(price)) / price * 10_000.0
            )

    def test_TOPIX100は呼値が細かいぶん安い(self, mcl: ModuleType) -> None:
        assert mcl.round_trip_bps(1_000.0, topix100=True) < mcl.round_trip_bps(1_000.0)

    def test_同じ呼値の中では株価が高いほど安い(self, mcl: ModuleType) -> None:
        """呼値は絶対額なので、比率としては株価に反比例する。"""
        assert mcl.round_trip_bps(3_000.0) < mcl.round_trip_bps(1_000.0)
        assert mcl.round_trip_bps(1_000.0) < mcl.round_trip_bps(500.0)

    def test_呼値の境界を超えると跳ねる(self, mcl: ModuleType) -> None:
        """**のこぎり波の核心。** ここを取り違えると株価帯の判断が逆になる。"""
        assert mcl.round_trip_bps(3_001.0) > mcl.round_trip_bps(2_999.0)
        assert mcl.round_trip_bps(5_001.0) > mcl.round_trip_bps(4_999.0)

    def test_株価が0以下ならエラー(self, mcl: ModuleType) -> None:
        with pytest.raises(ValueError, match="price"):
            mcl.round_trip_bps(0.0)


class TestPriceCeiling:
    def test_資金の25パーセントを100株で割る(self, mcl: ModuleType) -> None:
        """**安全装置#7 の逆算値**（意思決定ログ21）。"""
        assert mcl.price_ceiling_yen(500_000) == Decimal("1250")
        assert mcl.price_ceiling_yen(1_200_000) == Decimal("3000")

    def test_資金に比例する(self, mcl: ModuleType) -> None:
        assert mcl.price_ceiling_yen(1_000_000) == mcl.price_ceiling_yen(500_000) * 2


class TestCheapestPrice:
    def test_呼値の境界の直下を選ぶ(self, mcl: ModuleType) -> None:
        """**上限いっぱいが最安とは限らない。** 3,000円を超えると呼値が5円になる。"""
        assert mcl.cheapest_price_at_or_below(Decimal("5000")) == Decimal("3000")
        assert mcl.cheapest_price_at_or_below(Decimal("12500")) == Decimal("3000")

    def test_境界に届かなければ上限そのもの(self, mcl: ModuleType) -> None:
        assert mcl.cheapest_price_at_or_below(Decimal("1250")) == Decimal("1250")

    def test_TOPIX100は別の境界になる(self, mcl: ModuleType) -> None:
        """TOPIX100 は1,000円以下が0.1円なので、そこが最安。"""
        assert mcl.cheapest_price_at_or_below(
            Decimal("5000"), topix100=True
        ) == Decimal("1000")

    def test_選ばれた株価が実際に最安(self, mcl: ModuleType) -> None:
        """**探索の結果が本当に最小か**を、総当たりで突き合わせる。"""
        ceiling = Decimal("5000")
        best = mcl.cheapest_price_at_or_below(ceiling)
        best_bps = mcl.round_trip_bps(float(best))
        for price in range(300, int(ceiling) + 1, 7):
            assert mcl.round_trip_bps(float(price)) >= best_bps - 1e-9

    def test_上限が0以下ならエラー(self, mcl: ModuleType) -> None:
        with pytest.raises(ValueError, match="ceiling"):
            mcl.cheapest_price_at_or_below(Decimal("0"))


class TestTradable:
    def _row(
        self,
        mcl: ModuleType,
        code: str,
        *,
        price: float,
        turnover: float,
        market: str | None = "プライム",
        margin_type: str | None = "貸借",
    ) -> Any:
        return mcl.MarketRow(
            code=code,
            name=code,
            price=price,
            avg_turnover_yen=turnover,
            topix100=False,
            scale_category=None,
            market=market,
            margin_type=margin_type,
        )

    def test_株価上限を超える銘柄は外す(self, mcl: ModuleType) -> None:
        rows = (
            self._row(mcl, "CHEAP", price=1_000.0, turnover=1e9),
            self._row(mcl, "PRICEY", price=3_000.0, turnover=1e9),
        )
        assert [r.code for r in mcl.tradable(rows, 500_000)] == ["CHEAP"]
        assert len(mcl.tradable(rows, 1_200_000)) == 2

    def test_流動性が足りない銘柄は外す(self, mcl: ModuleType) -> None:
        rows = (
            self._row(mcl, "LIQUID", price=1_000.0, turnover=1e9),
            self._row(mcl, "THIN", price=1_000.0, turnover=1e6),
        )
        assert [r.code for r in mcl.tradable(rows, 500_000)] == ["LIQUID"]

    def test_コスト上限で絞れる(self, mcl: ModuleType) -> None:
        rows = (
            self._row(mcl, "LOW", price=2_000.0, turnover=1e9),   # 10bps
            self._row(mcl, "HIGH", price=500.0, turnover=1e9),    # 40bps
        )
        assert [
            r.code for r in mcl.tradable(rows, 1_200_000, max_cost_bps=20.0)
        ] == ["LOW"]

    def test_成績を一切見ない(self, mcl: ModuleType) -> None:
        """**構造的な基準だけで選ぶ**（意思決定ログ69・94）。

        `MarketRow` は成績を持たない——持たせないことで、
        成績で選ぶ経路を構造的に塞いでいる。
        """
        row = self._row(mcl, "A", price=1_000.0, turnover=1e9)
        assert not hasattr(row, "gross_bps")
        assert not hasattr(row, "annual_return_pct")

    def test_買える株価かどうかは安全装置7で決まる(self, mcl: ModuleType) -> None:
        row = self._row(mcl, "A", price=1_250.0, turnover=1e9)
        assert row.affordable(500_000)
        assert not self._row(mcl, "B", price=1_251.0, turnover=1e9).affordable(500_000)


class TestStructuralGates:
    """市場区分・信用区分のゲート。**ETF・REIT を入れない。**

    実測で `1357`（日経平均ダブルインバースETF）などが287銘柄に紛れ込んで
    いた（意思決定ログ99）。ETF は**市場全体への方向性の賭けそのもの**で、
    意思決定ログ71 で「別の商品」として棄却した性質を持つ。

    しかも**対象市場を変えるのは人間が判断すること**（`CLAUDE.md`）なので、
    ゲートが無いこと自体が規約違反だった。
    """

    def _row(
        self,
        mcl: ModuleType,
        code: str,
        *,
        market: str | None,
        margin_type: str | None,
    ) -> Any:
        return mcl.MarketRow(
            code=code,
            name=code,
            price=1_000.0,
            avg_turnover_yen=1e9,
            topix100=False,
            scale_category=None,
            market=market,
            margin_type=margin_type,
        )

    def test_プライム以外は外す(self, mcl: ModuleType) -> None:
        rows = (
            self._row(mcl, "PRIME", market="プライム", margin_type="貸借"),
            self._row(mcl, "STD", market="スタンダード", margin_type="貸借"),
            self._row(mcl, "ETF", market="ETF・ETN", margin_type="-"),
        )
        assert [r.code for r in mcl.tradable(rows, 1_200_000)] == ["PRIME"]

    def test_貸借以外は外す(self, mcl: ModuleType) -> None:
        """安全装置#12（売建可否）。制度信用で売建できない銘柄は入れない。"""
        rows = (
            self._row(mcl, "LOAN", market="プライム", margin_type="貸借"),
            self._row(mcl, "MARGIN", market="プライム", margin_type="信用"),
        )
        assert [r.code for r in mcl.tradable(rows, 1_200_000)] == ["LOAN"]

    def test_市場区分が不明な銘柄は外す(self, mcl: ModuleType) -> None:
        """**不明を通さない。** 検証できないものは保守的な側に倒す（規約5）。"""
        rows = (self._row(mcl, "UNKNOWN", market=None, margin_type="貸借"),)
        assert mcl.tradable(rows, 1_200_000) == ()

    def test_緩めるときは引数に書く(self, mcl: ModuleType) -> None:
        """**対象市場を広げたことがコードに残る形にする。**"""
        rows = (self._row(mcl, "STD", market="スタンダード", margin_type="貸借"),)
        widened = mcl.tradable(rows, 1_200_000, markets=("プライム", "スタンダード"))
        assert [r.code for r in widened] == ["STD"]

    def test_選定にもゲートが効く(self, mcl: ModuleType) -> None:
        rows = (
            self._row(mcl, "PRIME", market="プライム", margin_type="貸借"),
            self._row(mcl, "ETF", market="ETF・ETN", margin_type="-"),
        )
        selected = mcl.select_universe(rows, max_cost_bps=20.0)
        assert [r.code for r in selected] == ["PRIME"]


class TestSelectUniverse:
    """**構造的な基準だけで切り出す。** 成績は一切見ない（意思決定ログ94・95）。"""

    def _row(
        self,
        mcl: ModuleType,
        code: str,
        *,
        price: float,
        turnover: float,
        market: str | None = "プライム",
        margin_type: str | None = "貸借",
    ) -> Any:
        return mcl.MarketRow(
            code=code,
            name=code,
            price=price,
            avg_turnover_yen=turnover,
            topix100=False,
            scale_category="TOPIX Mid400",
            market=market,
            margin_type=margin_type,
        )

    def test_コストの安い順に並ぶ(self, mcl: ModuleType) -> None:
        rows = (
            self._row(mcl, "MID", price=2_500.0, turnover=1e9),   # 8bps
            self._row(mcl, "CHEAP", price=3_000.0, turnover=1e9),  # 6.7bps
            self._row(mcl, "PRICY", price=2_000.0, turnover=1e9),  # 10bps
        )
        assert [r.code for r in mcl.select_universe(rows)] == ["CHEAP", "MID", "PRICY"]

    def test_同じコストなら売買代金の大きい順(self, mcl: ModuleType) -> None:
        """**どちらも成績とは無関係な構造的な量。**"""
        rows = (
            self._row(mcl, "THIN", price=3_000.0, turnover=5e8),
            self._row(mcl, "DEEP", price=3_000.0, turnover=9e9),
        )
        assert [r.code for r in mcl.select_universe(rows)] == ["DEEP", "THIN"]

    def test_コスト上限を超える銘柄は入らない(self, mcl: ModuleType) -> None:
        rows = (
            self._row(mcl, "OK", price=2_000.0, turnover=1e9),     # 10bps
            self._row(mcl, "EXPENSIVE", price=1_000.0, turnover=1e9),  # 20bps
        )
        assert [r.code for r in mcl.select_universe(rows)] == ["OK"]

    def test_株価上限を超える銘柄は入らない(self, mcl: ModuleType) -> None:
        """資金120万円なら株価上限3,000円（安全装置#7）。"""
        rows = (self._row(mcl, "TOO_HIGH", price=3_500.0, turnover=1e9),)
        assert mcl.select_universe(rows) == ()

    def test_流動性が足りない銘柄は入らない(self, mcl: ModuleType) -> None:
        rows = (self._row(mcl, "THIN", price=3_000.0, turnover=1e6),)
        assert mcl.select_universe(rows) == ()

    def test_資金とコスト上限を上書きできる(self, mcl: ModuleType) -> None:
        rows = (self._row(mcl, "A", price=1_000.0, turnover=1e9),)  # 20bps
        assert mcl.select_universe(rows) == ()
        assert len(mcl.select_universe(rows, max_cost_bps=20.0)) == 1
