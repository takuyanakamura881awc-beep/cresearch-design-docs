"""成績指標の計算。

**シャープレシオが最重要。** 合格基準（docs/07-go-live-criteria.md）でも
リターンそのものより優先する。

3ヶ月という期間は運と実力を分離するには短い。高いリターンが「良い戦略」なのか
「運良く大きく賭けて当たった」のかは、リターン単体では区別できない。
変動の大きさで割ることで初めて比較可能になる。
"""

from __future__ import annotations


def sharpe_ratio(returns: list[float], periods_per_year: int = 252) -> float:
    """シャープレシオ。合格基準は > 1.0。"""
    raise NotImplementedError("Phase 2 で実装する")


def max_drawdown(equity_curve: list[float]) -> float:
    """最大ドローダウン（負値）。合格基準は > -15%。"""
    raise NotImplementedError("Phase 2 で実装する")


def profit_factor(pnls: list[float]) -> float:
    """プロフィットファクター（総利益 ÷ 総損失）。"""
    raise NotImplementedError("Phase 2 で実装する")


def win_rate(pnls: list[float]) -> float:
    """勝率。

    単独では意味が薄い。勝率が高くても1回の負けが大きければ収支は負になる。
    必ずプロフィットファクターと合わせて見る。
    """
    raise NotImplementedError("Phase 2 で実装する")
