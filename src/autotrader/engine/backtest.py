"""バックテストエンジン。

【最重要の設計要求】ルックアヘッドバイアスを**構造的に**防ぐ。

「注意して書く」では不十分。``PointInTimeView`` が各時点で参照可能な
データだけを露出させ、**未来のデータには物理的にアクセスできない**ようにする。

backtest-validator（.claude/agents/）はこの点を検証し、
「注意して書いている」だけなら**不合格**とする。

【約定コストのモデル】
デイトレ信用は手数料0・金利0・貸株料0だが、コストは0ではない。
スリッページとスプレッドを必ず引く。ここを甘くすると
バックテストとペーパーが乖離する（docs/07-go-live-criteria.md 基準#4）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from autotrader.strategy.base import Strategy
from autotrader.types import Bar


class PointInTimeView:
    """指定時刻までのデータだけを露出させるビュー。

    バックテストの戦略には必ずこれを経由してデータを渡す。
    生のデータフレームを渡すと、戦略が未来を覗ける経路ができてしまう。
    """

    def __init__(self, bars: dict[str, tuple[Bar, ...]], now: datetime) -> None:
        raise NotImplementedError("Phase 2 で実装する")

    def get(self, symbol: str) -> tuple[Bar, ...]:
        """``now`` 以前のバーだけを返す。"""
        raise NotImplementedError("Phase 2 で実装する")


@dataclass(frozen=True)
class BacktestResult:
    """バックテストの結果。

    ``sharpe`` が最重要指標。リターンだけを見ると、たまたま勝った
    高リスク手法を選んでしまう（docs/07-go-live-criteria.md §1）。
    """

    total_return: float
    sharpe: float
    max_drawdown: float
    n_trades: int
    win_rate: float
    profit_factor: float
    equity_curve: tuple[float, ...]


def run(
    strategy: Strategy,
    start: date,
    end: date,
    initial_cash: float = 500_000,
    slippage_bps: float = 10.0,
) -> BacktestResult:
    """バックテストを実行する。

    Args:
        slippage_bps: 片道スリッページ（bps）。**0 にしてはならない。**
            手数料が0でもコストは0ではない。

    Returns:
        結果。採用の判断前に backtest-validator の検証を通すこと。
    """
    raise NotImplementedError("Phase 2 で実装する")


def walk_forward(
    strategy: Strategy,
    start: date,
    end: date,
    train_months: int = 3,
    test_months: int = 1,
) -> tuple[BacktestResult, ...]:
    """ウォークフォワード検証を実行する。

    in-sample でパラメータを決め、out-of-sample で検証する。
    **in-sample の成績は成績として数えない。**

    Returns:
        各 out-of-sample 期間の結果。
    """
    raise NotImplementedError("Phase 2 で実装する")
