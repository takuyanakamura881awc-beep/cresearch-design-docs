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

import logging
from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

from autotrader.report.metrics import (
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    to_returns,
    win_rate,
)
from autotrader.strategy.base import Strategy
from autotrader.types import Bar, Side

logger = logging.getLogger(__name__)

STAGE_A_SLIPPAGE_BPS = 20.0
"""Stage A の片道スリッページ（bps）。

**Stage B（10bps）より厚い。** 板情報がないぶん約定価格を推定に頼るため、
検証できないものは保守的な側に倒す（CLAUDE.md 規約5）。
"""


class PointInTimeView:
    """指定時刻の時点で参照してよいデータだけを露出させるビュー。

    バックテストの戦略には必ずこれを経由してデータを渡す。
    生の辞書を渡すと、戦略が未来を覗ける経路ができてしまう。

    【``<= now`` ではなく ``< now`` で切る理由】

    バーのタイムスタンプは**期間の開始時刻**（yfinance も J-Quants もこの規約）。
    09:05 のタイムスタンプを持つ5分足は 09:05〜09:10 の値動きを表すので、
    **09:05 時点ではまだ確定していない**。``<= now`` で切ると、
    これから起きる5分間の高値・安値・終値が見えてしまう。

    見えても「使わないように書く」のでは、指標の計算を1行足した誰かが
    静かに未来を参照する。**そもそも渡さない。**

    したがって ``now`` に立っている観測者に見えるのは、
    ``now`` より前に開始した = すでに閉じたバーだけになる。

    【元データを保持し、都度スライスしない理由】

    銘柄ごとに一度だけ二分探索して境界を出す。全期間ぶんをコピーすると
    銘柄数×時刻数のコピーが走り、5分足×287銘柄では現実的な速度にならない。
    """

    __slots__ = ("_bars", "_now", "_cutoff")

    def __init__(self, bars: Mapping[str, tuple[Bar, ...]], now: datetime) -> None:
        self._bars = bars
        self._now = now
        self._cutoff: dict[str, int] = {}

    @property
    def now(self) -> datetime:
        return self._now

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self._bars)

    def get(self, symbol: str) -> tuple[Bar, ...]:
        """``now`` より前に閉じたバーだけを返す。

        知らない銘柄には空を返す。**例外にしない**のは、
        ユニバースに入ったばかりでまだ履歴がない銘柄と扱いを揃えるため
        （戦略側は「データが足りなければ何もしない」だけでよい）。
        """
        series = self._bars.get(symbol)
        if not series:
            return ()
        index = self._cutoff.get(symbol)
        if index is None:
            # Bar は比較不能なのでキー列を作って二分探索する
            timestamps = [b.timestamp for b in series]
            index = bisect_left(timestamps, self._now)
            self._cutoff[symbol] = index
        return series[:index]

    def latest(self, symbol: str) -> Bar | None:
        """直近の確定バー。無ければ ``None``。"""
        series = self.get(symbol)
        return series[-1] if series else None

    def as_dict(self) -> dict[str, tuple[Bar, ...]]:
        """``Strategy.generate`` に渡す形。全銘柄ぶんを確定バーだけで作る。"""
        return {symbol: self.get(symbol) for symbol in self._bars}


@dataclass(frozen=True)
class CostModel:
    """約定コスト。**ゼロにしてはならない**（CLAUDE.md 規約5）。

    デイトレ信用は手数料0・金利0・貸株料0だが、
    スリッページとスプレッドは必ず発生する。
    コストゼロのシミュレーションは本番で必ず乖離する。
    """

    slippage_bps: float = STAGE_A_SLIPPAGE_BPS
    """片道スリッページ（bps）。往復では2倍かかる。"""

    def __post_init__(self) -> None:
        if self.slippage_bps <= 0:
            raise ValueError(
                "スリッページを0以下にしてはならない。"
                "手数料が0でもコストは0ではない（CLAUDE.md 規約5）"
            )

    @property
    def rate(self) -> float:
        return self.slippage_bps / 10_000.0

    def fill_price(self, side: Side, reference: float, *, opening: bool) -> float:
        """約定価格。**必ず不利な側にずらす。**

        買い建て・売り決済の別ではなく「その取引で自分がどちら側か」で決まる。
        新規買い/返済買いは高く、新規売り/返済売りは安く約定する。
        """
        buying = (side is Side.LONG) == opening
        return reference * (1 + self.rate) if buying else reference * (1 - self.rate)


@dataclass(frozen=True)
class Trade:
    """約定して手仕舞われた1トレード。

    **コスト込みの約定価格を保持する。** 建値と手仕舞い値を「理論価格」で
    持ってしまうと、集計のどこかでコストを引き忘れても気づけない。
    ここに入る時点で既にスリッページを含んでいる。
    """

    symbol: str
    side: Side
    quantity: int
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str
    """手仕舞いの理由。監査とデバッグのため必須（例: "stop" / "close_all"）。"""

    @property
    def pnl(self) -> float:
        """損益（円）。ショートは方向を反転する。"""
        diff = self.exit_price - self.entry_price
        if self.side is Side.SHORT:
            diff = -diff
        return diff * self.quantity

    @property
    def notional(self) -> float:
        """建玉金額（円）。"""
        return self.entry_price * self.quantity

    @property
    def return_pct(self) -> float:
        """建玉金額に対する損益率。"""
        return self.pnl / self.notional if self.notional else 0.0


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
    trades: tuple[Trade, ...] = ()
    initial_cash: float = 0.0
    rejected_by_leverage: int = 0
    """レバレッジ1倍の上限で発注を見送った回数。

    **多いことを異常として扱わない。** これは安全装置が働いた記録であって
    バグではない。ただし極端に多い場合はサイジングが資金量に合っていない
    （1銘柄あたりの目標額が大きすぎる）ことを示す。
    """

    @classmethod
    def from_equity(
        cls,
        equity_curve: list[float],
        trades: list[Trade],
        initial_cash: float,
        periods_per_year: int = 252,
        rejected_by_leverage: int = 0,
    ) -> BacktestResult:
        """エクイティカーブとトレード列から指標をまとめて算出する。

        **集計をここ1箇所に閉じる。** シャープの定義が呼び出し側ごとに
        ずれると、期間をまたいだ比較ができなくなる。
        """
        pnls = [t.pnl for t in trades]
        last = equity_curve[-1] if equity_curve else initial_cash
        return cls(
            total_return=(last / initial_cash - 1.0) if initial_cash > 0 else 0.0,
            sharpe=sharpe_ratio(to_returns(equity_curve), periods_per_year),
            max_drawdown=max_drawdown(equity_curve),
            n_trades=len(trades),
            win_rate=win_rate(pnls),
            profit_factor=profit_factor(pnls),
            equity_curve=tuple(equity_curve),
            trades=tuple(trades),
            initial_cash=initial_cash,
            rejected_by_leverage=rejected_by_leverage,
        )


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
