"""損失ブレーカーとポジション制限（安全装置 #4/#5/#6/#7）。

ブレーカーは三層構造（docs/05-risk-management.md）:

===============  ====================  =========================  ==================
層               発動条件              動作                       復帰
===============  ====================  =========================  ==================
日次             当日 -2%              当日全停止 + 全クローズ    翌営業日に自動
連続             3営業日連続マイナス   自動停止                   人の明示承認
累積             ピークから -15%       全停止                     人の明示承認
===============  ====================  =========================  ==================

日次は自動復帰、連続と累積は人の判断を要求する。
連続して負けているのは戦略が現在の相場に合っていない可能性があり、
自動再開すると損失を垂れ流すため。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BreakerAction(Enum):
    """ブレーカー発動時の動作。"""

    NONE = "none"
    HALT = "halt"
    """新規発注を停止する（既存建玉は保持）"""
    HALT_AND_CLOSE = "halt_and_close"
    """新規発注を停止し、全建玉をクローズする"""


@dataclass(frozen=True)
class BreakerState:
    """ブレーカーの状態。"""

    tripped: bool
    action: BreakerAction
    reason: str
    auto_resume: bool
    """False なら人の明示承認まで再開しない（scripts/resume_trading.py）"""


def check_daily_loss(pnl_pct: float, threshold_pct: float = -0.02) -> BreakerState:
    """日次損失上限を判定する（#4）。翌営業日に自動復帰する。"""
    raise NotImplementedError("Phase 3 で実装する")


def check_consecutive_loss(
    daily_pnls: list[float], threshold_days: int = 3
) -> BreakerState:
    """連続損失日数を判定する（#5）。復帰には人の明示承認が必要。"""
    raise NotImplementedError("Phase 3 で実装する")


def check_max_drawdown(
    equity_curve: list[float], threshold_pct: float = -0.15
) -> BreakerState:
    """ピークからのドローダウンを判定する（#6）。復帰には人の明示承認が必要。

    総資産が30万円を下回ると信用取引そのものが継続できなくなる
    （最低委託保証金）。この上限はそのラインへの到達をはるかに手前で止める
    役割も持つ（docs/02-margin-rules.md §2）。
    """
    raise NotImplementedError("Phase 3 で実装する")


def check_position_limits(
    n_positions: int,
    symbol_weight: float,
    max_concurrent: int = 5,
    max_weight_per_symbol: float = 0.25,
) -> BreakerState:
    """同時保有数と1銘柄あたりの比重を判定する（#7）。"""
    raise NotImplementedError("Phase 3 で実装する")
