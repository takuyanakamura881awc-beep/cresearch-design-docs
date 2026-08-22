"""ポジションサイジングのテスト。

**単元100株の粒度が50万円という資金を強く縛る。**
端数処理を誤ると、意図した金額と実際の建玉額がずれてレバレッジ判定を狂わせる。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from autotrader.risk.sizing import calc_quantity, max_affordable_price


class TestCalcQuantity:
    def test_単元の倍数に切り下げる(self) -> None:
        """**切り上げてはならない。** 目標額を超えるとレバレッジ上限に抵触しうる。"""
        # 12.5万円 ÷ 1,000円 = 125株 → 100株に切り下げ
        assert calc_quantity(Decimal(125_000), 1000.0) == 100

    def test_ちょうど割り切れる場合(self) -> None:
        assert calc_quantity(Decimal(200_000), 1000.0) == 200

    def test_1単元も買えなければゼロ(self) -> None:
        """株価1,300円は1単元13万円。12.5万円の枠では建てられない。"""
        assert calc_quantity(Decimal(125_000), 1300.0) == 0

    def test_目標額がゼロ以下ならゼロ(self) -> None:
        assert calc_quantity(Decimal(0), 1000.0) == 0
        assert calc_quantity(Decimal(-1), 1000.0) == 0

    def test_単元株数を変えられる(self) -> None:
        assert calc_quantity(Decimal(125_000), 1000.0, lot_size=1) == 125

    def test_不正な株価を拒否する(self) -> None:
        with pytest.raises(ValueError):
            calc_quantity(Decimal(100_000), 0.0)
        with pytest.raises(ValueError):
            calc_quantity(Decimal(100_000), -100.0)

    def test_端数のある株価でも超えない(self) -> None:
        """浮動小数の誤差で1単元多く出てはならない。"""
        qty = calc_quantity(Decimal(125_000), 416.7)
        assert qty * Decimal("416.7") <= Decimal(125_000)


class TestMaxAffordablePrice:
    def test_50万円の25パーセントで買える最大株価は1250円(self) -> None:
        """**ユニバースの株価上限3,000円と食い違う。**

        1銘柄あたり総資産25%（docs/05-risk-management.md #7）を守る限り、
        1,250円を超える銘柄は1単元すら建てられない。
        選定は通るがサイジングで0株になる銘柄が生まれる。
        """
        assert max_affordable_price(Decimal(500_000)) == Decimal(1250)

    def test_資金が増えれば上がる(self) -> None:
        assert max_affordable_price(Decimal(3_000_000)) == Decimal(7500)

    def test_上限比率を緩めれば上がる(self) -> None:
        assert max_affordable_price(Decimal(500_000), max_weight_per_symbol=0.6) == Decimal(3000)
