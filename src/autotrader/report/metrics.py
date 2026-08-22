"""成績指標の計算。

**シャープレシオが最重要。** 合格基準（docs/07-go-live-criteria.md）でも
リターンそのものより優先する。

3ヶ月という期間は運と実力を分離するには短い。高いリターンが「良い戦略」なのか
「運良く大きく賭けて当たった」のかは、リターン単体では区別できない。
変動の大きさで割ることで初めて比較可能になる。

【この module の原則】検証できない状態では、良い数字を返さない。

サンプルが足りない・分散がゼロといった「計算はできるが意味がない」状況で
大きな値を返すと、**合格基準を偶然通過してしまう**。
そういう場合は保守的な側（成績が悪く見える側）に倒す。
"""

from __future__ import annotations

import math
from statistics import fmean, stdev

TRADING_DAYS_PER_YEAR = 252
MIN_SAMPLES_FOR_SHARPE = 20
"""シャープレシオを計算してよい最小サンプル数。

**20日未満の標準偏差は推定として信用できない。** 少数のサンプルで
たまたま変動が小さいと、シャープが不当に大きく出る。
"""


def sharpe_ratio(
    returns: list[float],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    min_samples: int = MIN_SAMPLES_FOR_SHARPE,
) -> float:
    """シャープレシオ（年率換算）。合格基準は > 1.0。

    無リスク金利は 0 とみなす（日本円の短期金利は目標月利に対して無視できる）。

    Args:
        returns: 期間ごとのリターン（0.01 = +1%）。
        periods_per_year: 年率換算の係数。日次なら252。

    Returns:
        年率シャープ。**計算しても意味がない場合は 0.0 を返す**:

        - サンプルが ``min_samples`` 未満（標準偏差が信用できない）
        - 標準偏差が 0（変動なし。無限大を返すと合格基準を素通りする）

        いずれも「良い」ではなく「**判定不能**」であり、
        保守的な側に倒して 0.0 とする。
    """
    if len(returns) < max(min_samples, 2):
        return 0.0
    sd = stdev(returns)
    if sd == 0:
        return 0.0
    return fmean(returns) / sd * math.sqrt(periods_per_year)


def max_drawdown(equity_curve: list[float]) -> float:
    """最大ドローダウン（負値）。合格基準は > -15%。

    ピークからの下落率の最大値。**含み損の底で測る**ので、
    日次終値だけを見ると日中の実際の下落を過小評価する点に注意。

    Returns:
        0.0 以下の値（-0.15 = -15%）。データが1点以下なら 0.0。
    """
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    worst = 0.0
    for value in equity_curve:
        if value > peak:
            peak = value
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def profit_factor(pnls: list[float]) -> float:
    """プロフィットファクター（総利益 ÷ 総損失）。

    Returns:
        PF。負けトレードが1件もない場合は ``inf``。

        **``inf`` は「優秀」ではなく「疑わしい」と読む。**
        実運用で負けが1件もないことはまずないので、
        バックテストのバグかルックアヘッドを疑う信号として扱う
        （backtest-validator の検査対象）。
        トレードが0件なら 0.0。
    """
    if not pnls:
        return 0.0
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def win_rate(pnls: list[float]) -> float:
    """勝率。

    損益ゼロ（同値撤退）は勝ちに数えない。コストを引いた後のゼロは
    「取引した意味がなかった」であって勝ちではないため。

    単独では意味が薄い。勝率が高くても1回の負けが大きければ収支は負になる。
    必ずプロフィットファクターと合わせて見る。
    """
    if not pnls:
        return 0.0
    return sum(1 for p in pnls if p > 0) / len(pnls)


def to_returns(equity_curve: list[float]) -> list[float]:
    """エクイティカーブを期間リターンの列に変換する。

    直前の値が 0 以下の区間は変化率が定義できないのでスキップする
    （破産後の値を 0 で割って inf を混ぜない）。
    """
    return [
        equity_curve[i] / equity_curve[i - 1] - 1.0
        for i in range(1, len(equity_curve))
        if equity_curve[i - 1] > 0
    ]
