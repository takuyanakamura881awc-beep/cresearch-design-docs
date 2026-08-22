"""成績指標のテスト。

**重点は「検証できない状態で良い数字を返さない」こと。**
サンプル不足や分散ゼロで大きな値が出ると、合格基準
（docs/07-go-live-criteria.md）を偶然通過してしまう。
"""

from __future__ import annotations

import math

import pytest

from autotrader.report.metrics import (
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    to_returns,
    win_rate,
)


class TestSharpe:
    def test_年率換算する(self) -> None:
        """標準偏差は不偏（n-1）で取る。少ないサンプルで過小評価しないため。"""
        returns = [0.01, -0.005] * 15
        mean = 0.0025
        sd = math.sqrt(30 * 0.0075**2 / 29)  # 不偏標準偏差
        got = sharpe_ratio(returns, periods_per_year=252)
        assert got == pytest.approx(mean / sd * math.sqrt(252), rel=1e-9)

    def test_サンプルが足りなければゼロを返す(self) -> None:
        """**20日未満の標準偏差は信用できない。**

        たまたま変動が小さいだけでシャープが不当に大きく出る。
        「良い」ではなく「判定不能」なので保守的に 0 とする。
        """
        assert sharpe_ratio([0.01] * 19) == 0.0
        assert sharpe_ratio([]) == 0.0

    def test_変動ゼロでもゼロを返す(self) -> None:
        """無限大を返すと合格基準（シャープ > 1.0）を素通りする。"""
        assert sharpe_ratio([0.01] * 30) == 0.0

    def test_負のリターンなら負のシャープになる(self) -> None:
        returns = [-0.01, 0.005] * 15
        assert sharpe_ratio(returns) < 0


class TestMaxDrawdown:
    def test_ピークからの最大下落を返す(self) -> None:
        assert max_drawdown([100, 120, 90, 130]) == pytest.approx(-0.25)

    def test_上がり続ければゼロ(self) -> None:
        assert max_drawdown([100, 110, 120]) == 0.0

    def test_点が足りなければゼロ(self) -> None:
        assert max_drawdown([100]) == 0.0
        assert max_drawdown([]) == 0.0

    def test_回復後に更に下げたら深い方を返す(self) -> None:
        assert max_drawdown([100, 80, 100, 60]) == pytest.approx(-0.40)


class TestProfitFactor:
    def test_総利益わる総損失(self) -> None:
        assert profit_factor([100.0, -50.0, 200.0, -50.0]) == pytest.approx(3.0)

    def test_負けがなければ無限大(self) -> None:
        """**「優秀」ではなく「疑わしい」と読む。**

        実運用で負けが1件もないことはまずない。
        バックテストのバグかルックアヘッドを疑う信号。
        """
        assert profit_factor([100.0, 50.0]) == math.inf

    def test_トレードがなければゼロ(self) -> None:
        assert profit_factor([]) == 0.0

    def test_損益ゼロだけならゼロ(self) -> None:
        assert profit_factor([0.0, 0.0]) == 0.0


class TestWinRate:
    def test_勝ちトレードの比率(self) -> None:
        assert win_rate([1.0, -1.0, 1.0, -1.0]) == 0.5

    def test_同値撤退は勝ちに数えない(self) -> None:
        """コストを引いた後のゼロは「取引した意味がなかった」であって勝ちではない。"""
        assert win_rate([0.0, 1.0]) == 0.5

    def test_トレードがなければゼロ(self) -> None:
        assert win_rate([]) == 0.0


class TestToReturns:
    def test_変化率の列にする(self) -> None:
        assert to_returns([100.0, 110.0, 99.0]) == pytest.approx([0.1, -0.1])

    def test_破産後のゼロ除算を避ける(self) -> None:
        assert to_returns([100.0, 0.0, 0.0]) == pytest.approx([-1.0])

    def test_点が1つなら空(self) -> None:
        assert to_returns([100.0]) == []
