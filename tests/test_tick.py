"""呼値の単位と、そこから導く約定コストのテスト。

重点は3つ。

1. **境界値**（1,000円 / 3,000円ちょうど）— 表の実装は境界で間違えやすい
2. **`往復コスト = スプレッド円 ÷ ATR円` の恒等性** — 株価は式に出てこない。
   ここが崩れると「ATR% で判定してよい」という誤った近似に戻る
3. **下限が株価に追随すること** — 固定の ATR% を捨てた理由そのもの
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from autotrader.risk.limits import max_atr_pct, max_atr_yen
from autotrader.risk.sizing import max_affordable_price
from autotrader.tick import (
    DEFAULT_COST_ATR_MULTIPLE,
    DEFAULT_SPREAD_TICKS,
    half_spread_bps,
    min_atr_yen,
    round_trip_cost_atr,
    spread_yen,
    tick_size,
)

CAPITAL = Decimal(500_000)


class TestTickSize:
    """東証の呼値表。**我々の株価帯で効くのは「3,000円以下 → 1円」だけ。**"""

    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            (300.0, Decimal(1)),
            (600.0, Decimal(1)),
            (1250.0, Decimal(1)),
            (2200.0, Decimal(1)),
            (3000.0, Decimal(1)),  # 境界は「以下」に含む
            (3000.5, Decimal(5)),
            (5000.0, Decimal(5)),
            (5001.0, Decimal(10)),
        ],
    )
    def test_通常銘柄(self, price: float, expected: Decimal) -> None:
        assert tick_size(price) == expected

    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            (999.0, Decimal("0.1")),
            (1000.0, Decimal("0.1")),  # 境界は「以下」に含む
            (1000.5, Decimal("0.5")),
            (3000.0, Decimal("0.5")),
            (3001.0, Decimal(1)),
        ],
    )
    def test_TOPIX100は細かい(self, price: float, expected: Decimal) -> None:
        """同じ株価でも呼値が1/10〜1/2になる。**入ってくればコスト面で有利。**"""
        assert tick_size(price, topix100=True) == expected

    def test_TOPIX100のほうが常に細かいか同じ(self) -> None:
        for price in (500.0, 1000.0, 2200.0, 5000.0, 20_000.0):
            assert tick_size(price, topix100=True) <= tick_size(price)

    def test_表の上限を超えても値を返す(self) -> None:
        assert tick_size(100_000_000.0) > 0

    def test_ゼロと負を拒否する(self) -> None:
        with pytest.raises(ValueError, match="正の値"):
            tick_size(0.0)
        with pytest.raises(ValueError, match="正の値"):
            tick_size(-100.0)


class TestSpread:
    def test_既定は呼値2本ぶん(self) -> None:
        """**1tick に張り付く前提を置かない**（CLAUDE.md 規約5）。"""
        assert DEFAULT_SPREAD_TICKS == 2.0
        assert spread_yen(1000.0) == Decimal(2)

    def test_片道はスプレッドの半分(self) -> None:
        # 1,000円で2円のスプレッド → 片道1円 = 10bps
        assert half_spread_bps(1000.0) == pytest.approx(10.0)
        # 2,200円でも呼値は1円なので同じ2円。相対では小さくなる
        assert half_spread_bps(2200.0) == pytest.approx(10_000.0 / 2200.0)

    def test_株価が高いほど相対コストが下がる(self) -> None:
        """呼値が同じなら、株価が高いほど相対的に安い。"""
        assert half_spread_bps(2200.0) < half_spread_bps(1250.0) < half_spread_bps(600.0)

    def test_本数を増やせば比例して広がる(self) -> None:
        assert spread_yen(1000.0, 4.0) == spread_yen(1000.0, 2.0) * 2

    def test_ゼロ本を拒否する(self) -> None:
        with pytest.raises(ValueError, match="正の値"):
            spread_yen(1000.0, 0.0)


class TestRoundTripCostAtr:
    """**この恒等式がモジュールの存在理由。**"""

    def test_同じATR円なら株価が違ってもコストは同じ(self) -> None:
        """``往復コスト = スプレッド円 ÷ ATR円``。株価は式に出てこない。

        600円 × ATR 3.33% と 2,200円 × ATR 0.91% はどちらも ATR 20円で、
        **払うコストは完全に同じ**。ATR% で判定すると前者だけが通る。
        """
        assert round_trip_cost_atr(600.0, 20.0) == pytest.approx(
            round_trip_cost_atr(2200.0, 20.0)
        )

    def test_ATR円が大きいほど安い(self) -> None:
        assert round_trip_cost_atr(1000.0, 40.0) < round_trip_cost_atr(1000.0, 20.0)

    def test_現行設定の実測相当を再現する(self) -> None:
        """600円 × ATR 3.33%（実測の中央値付近）で往復 0.10 ATR。"""
        assert round_trip_cost_atr(600.0, 600.0 * 0.0333) == pytest.approx(0.10, abs=0.001)

    def test_ユーザー提案の帯では大幅に下がる(self) -> None:
        """2,200円 × ATR 2.0% で 0.046 ATR。現行のおよそ半分以下。"""
        assert round_trip_cost_atr(2200.0, 2200.0 * 0.020) == pytest.approx(
            0.0455, abs=0.001
        )

    def test_ATRゼロを拒否する(self) -> None:
        with pytest.raises(ValueError, match="正の値"):
            round_trip_cost_atr(1000.0, 0.0)


class TestMinAtrYen:
    def test_スプレッドの5倍(self) -> None:
        assert DEFAULT_COST_ATR_MULTIPLE == 5.0
        assert min_atr_yen(1000.0) == Decimal(10)

    def test_下限を満たす銘柄はコストが上限に収まる(self) -> None:
        """下限ちょうどの銘柄で往復コストが ATR の 1/5 になる。"""
        floor = float(min_atr_yen(1000.0))
        assert round_trip_cost_atr(1000.0, floor) == pytest.approx(
            1.0 / DEFAULT_COST_ATR_MULTIPLE
        )

    def test_同じ呼値帯なら株価が違っても同じ円額(self) -> None:
        """300〜3,000円はすべて呼値1円なので、下限は一律10円。

        **これが「ATR% 2%固定」との決定的な違い。** 同じ10円でも
        600円なら1.67%、2,200円なら0.45%に相当する。
        """
        assert min_atr_yen(600.0) == min_atr_yen(2200.0) == Decimal(10)

    def test_呼値帯をまたぐと変わる(self) -> None:
        assert min_atr_yen(3000.5) > min_atr_yen(3000.0)

    def test_倍率ゼロを拒否する(self) -> None:
        with pytest.raises(ValueError, match="正の値"):
            min_atr_yen(1000.0, 0.0)


class TestAtrYenCeiling:
    """**同時保有数を絞ってもコストの天井は動かない**ことの確認。"""

    def test_ATR円の上限は比率に依存しない(self) -> None:
        """株価上限が上がる利得と ATR%上限が下がる損失が正確に打ち消し合う。

        これを取り違えると「集中すればコストが下がる」という誤った
        期待のもとに安全装置#7を緩めることになる。
        """
        for n in (5, 4, 3, 2):
            weight = 1.0 / n
            ceiling = float(max_affordable_price(CAPITAL, weight))
            atr_pct_max = max_atr_pct(max_weight_per_symbol=weight)
            assert ceiling * atr_pct_max == pytest.approx(
                float(max_atr_yen(CAPITAL)), rel=1e-9
            )

    def test_導出値は66円台(self) -> None:
        """50万円 × 2% ÷ (100株 × 1.5) = 66.7円。"""
        assert float(max_atr_yen(CAPITAL)) == pytest.approx(66.67, abs=0.01)

    def test_ブレーカーを緩めれば天井も上がる(self) -> None:
        """導出値が置き去りにならないことの確認。"""
        assert float(max_atr_yen(CAPITAL, daily_breaker_pct=0.04)) == pytest.approx(
            2 * float(max_atr_yen(CAPITAL))
        )

    def test_達成しうる最良コスト(self) -> None:
        """天井の銘柄でも往復 0.03 ATR。**これが構造的な下限。**"""
        best = float(max_atr_yen(CAPITAL))
        assert round_trip_cost_atr(1250.0, best) == pytest.approx(0.030, abs=0.001)

    def test_不正な入力を拒否する(self) -> None:
        with pytest.raises(ValueError, match="資金"):
            max_atr_yen(Decimal(0))
        with pytest.raises(ValueError, match="損切り倍率"):
            max_atr_yen(CAPITAL, stop_atr_mult=0.0)
        with pytest.raises(ValueError, match="単元株数"):
            max_atr_yen(CAPITAL, lot_size=0)
