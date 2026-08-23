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

from autotrader.report.metrics import max_drawdown

DEFAULT_DAILY_BREAKER_PCT = 0.02
"""日次損失上限（#4）。総資産のこの割合を失ったら当日全停止する。"""

DEFAULT_MAX_WEIGHT_PER_SYMBOL = 0.25
"""1銘柄あたり総資産の上限（#7）。"""

DEFAULT_CONSECUTIVE_LOSS_PCT = -0.05
"""連続損失ブレーカー（#5）が発動する累積損失。

**「方向」だけでなく「損害」で判定するための閾値。** 他の安全装置から導く:

- 日次上限 -2% の **2.5倍** — 1日ぶんの悪い日が3日続いた程度では止めない
- 累積DD上限 -15% の **1/3** — DD が発動する手前で先に気づける

【なぜ方向だけでは駄目か — 実測とモンテカルロ】

実データ（39営業日）で発動したのは -0.87% / -0.62% / -0.54%（合計 -2.0%）の
3日だった。日次ブレーカー1回ぶんの損失に3日かけて到達しただけで、
「戦略が相場に合っていない」証拠とは言えない。

3連敗が期間中に起きる確率（モンテカルロ）::

    下落日率     39営業日   60営業日
    55%            98%       100%
    50%            96%        99%
    45%            90%        97%
    40%(good)      81%        93%

**勝っている戦略でも3ヶ月にほぼ確実に発動する。** 再開には人の承認が
要るので、「完全自動＋キルスイッチ」という運用方針が成立しなくなる。
ペーパートレードの稼働日数も削られ、サンプルが溜まらない。
"""

DEFAULT_MAX_CONCURRENT = 5
"""同時保有の上限（#7）。通常枠の株価上限1,000円もここから導かれる。"""

DEFAULT_STOP_ATR_MULT = 1.5
"""損切り幅の ATR 倍率。config/strategies.yaml の ``stop_loss_atr_mult`` と一致させる。"""


def max_atr_pct(
    daily_breaker_pct: float = DEFAULT_DAILY_BREAKER_PCT,
    max_weight_per_symbol: float = DEFAULT_MAX_WEIGHT_PER_SYMBOL,
    stop_atr_mult: float = DEFAULT_STOP_ATR_MULT,
) -> float:
    """**1敗が日次ブレーカーに達しない ATR% の上限。**

    1敗あたりの総資産インパクトは ``建玉比率 × ATR% × 損切り倍率``。
    建玉比率が上限（25%）のとき、これが日次ブレーカー（2%）に届かない条件は::

        0.25 × ATR% × 1.5 < 0.02   →   ATR% < 5.33%

    【なぜ上限が要るのか】

    Layer 2 のスコアは **ATR% に最大の重み（0.40）** を置いている。
    値幅がないとコスト負けするので下限（2%）は当然として、
    **上限がないとスコアリング自体が「1敗で当日が終わる銘柄」を上位に押し上げる。**

    実測（2026-05-29）では ATR% の最大が 17.12% で、上位10銘柄のうち2つが
    1敗 -2% を超えていた。下限だけでは足りない。

    **定数を直書きせず導出する**のは `risk.sizing.max_affordable_price` と同じ理由。
    ブレーカーや上限比率を動かしたとき、ここが自動で追随する。
    """
    if max_weight_per_symbol <= 0 or stop_atr_mult <= 0:
        raise ValueError("上限比率と損切り倍率は正の値である必要がある")
    return daily_breaker_pct / (max_weight_per_symbol * stop_atr_mult)


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


OK = BreakerState(
    tripped=False, action=BreakerAction.NONE, reason="", auto_resume=True
)
"""発動していない状態。**共有して構わない**（frozen dataclass）。"""


def check_daily_loss(pnl_pct: float, threshold_pct: float = -0.02) -> BreakerState:
    """日次損失上限を判定する（#4）。翌営業日に自動復帰する。

    **日中の損益で判定する。** 終値だけで見ると、日中に -2% を割ってから
    戻した日を見逃す。バックテストでも同じ粒度で評価しないと、
    実運用では止まっていた日の取引を成績に含めてしまう。

    Args:
        pnl_pct: 当日の損益率（-0.02 = -2%）。
        threshold_pct: 発動する水準。**負の値**で渡す。

    Returns:
        判定結果。発動時は全建玉クローズまで行う。
    """
    if threshold_pct >= 0:
        raise ValueError(f"threshold_pct は負の値である必要がある: {threshold_pct}")
    if pnl_pct > threshold_pct:
        return OK
    return BreakerState(
        tripped=True,
        action=BreakerAction.HALT_AND_CLOSE,
        reason=f"日次損失 {pnl_pct:.2%} が上限 {threshold_pct:.2%} に到達",
        auto_resume=True,
    )


def check_consecutive_loss(
    daily_pnls: list[float],
    threshold_days: int = 3,
    min_cumulative_loss_pct: float = DEFAULT_CONSECUTIVE_LOSS_PCT,
) -> BreakerState:
    """連続損失を判定する（#5）。**復帰には人の明示承認が必要。**

    連続して負けているのは戦略が現在の相場に合っていない可能性があり、
    自動再開すると損失を垂れ流す。だから ``auto_resume`` は False。

    **連続日数と損失額の両方を要求する。** 日数だけだと微小な3連敗でも
    発動し、勝っている戦略でも3ヶ月にほぼ確実に止まる
    （`DEFAULT_CONSECUTIVE_LOSS_PCT` の docstring に実測とモンテカルロ）。
    止めるべきなのは「損害が続いているとき」であって
    「たまたま3日下げたとき」ではない。

    Args:
        daily_pnls: 日次損益率の列。**時系列の昇順**（末尾が直近）。
        threshold_days: 何営業日連続で発動するか。
        min_cumulative_loss_pct: 連敗期間の累積損失がこれ以下なら発動。
            **負の値**で渡す。

    Note:
        **損益ゼロの日は連敗を途切れさせる。** docs/05 の定義が
        「連続**マイナス**」であり、ゼロはマイナスではない。

        実務上ゼロになるのは**その日1トレードもしなかった場合**で、
        「戦略が相場に合っていない」という #5 の判定根拠にならない。
        ここを連敗に数えると、様子見の日を挟んだだけで人の承認待ちに入る。
    """
    if threshold_days < 1:
        raise ValueError(f"threshold_days は1以上: {threshold_days}")
    if min_cumulative_loss_pct >= 0:
        raise ValueError(
            f"min_cumulative_loss_pct は負の値: {min_cumulative_loss_pct}"
        )
    if len(daily_pnls) < threshold_days:
        return OK

    recent = daily_pnls[-threshold_days:]
    if any(p >= 0 for p in recent):
        return OK

    cumulative = sum(recent)
    if cumulative > min_cumulative_loss_pct:
        # 連敗はしているが損害が小さい。**止めるほどではない。**
        # 日次(-2%)と累積DD(-15%)が背後に残っているので守りは薄くならない。
        return OK

    return BreakerState(
        tripped=True,
        action=BreakerAction.HALT,
        reason=(
            f"{threshold_days}営業日連続でマイナス、累積 {cumulative:.2%} が "
            f"上限 {min_cumulative_loss_pct:.2%} に到達"
            f"（{', '.join(f'{p:.2%}' for p in recent)}）"
        ),
        auto_resume=False,
    )


def check_max_drawdown(
    equity_curve: list[float], threshold_pct: float = -0.15
) -> BreakerState:
    """ピークからのドローダウンを判定する（#6）。**復帰には人の明示承認が必要。**

    総資産が30万円を下回ると信用取引そのものが継続できなくなる
    （最低委託保証金）。この上限はそのラインへの到達をはるかに手前で止める
    役割も持つ（docs/02-margin-rules.md §2）。

    Args:
        equity_curve: 総資産の推移。**時系列の昇順**。
        threshold_pct: 発動する水準。**負の値**で渡す。
    """
    if threshold_pct >= 0:
        raise ValueError(f"threshold_pct は負の値である必要がある: {threshold_pct}")

    drawdown = max_drawdown(equity_curve)
    if drawdown > threshold_pct:
        return OK
    return BreakerState(
        tripped=True,
        action=BreakerAction.HALT_AND_CLOSE,
        reason=f"ピークからの下落 {drawdown:.2%} が上限 {threshold_pct:.2%} に到達",
        auto_resume=False,
    )


def check_position_limits(
    n_positions: int,
    symbol_weight: float,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    max_weight_per_symbol: float = DEFAULT_MAX_WEIGHT_PER_SYMBOL,
) -> BreakerState:
    """同時保有数と1銘柄あたりの比重を判定する（#7）。

    **これは新規建てを止めるだけで、既存建玉には触れない。**
    上限に達したこと自体は異常ではなく、想定内の制約。

    Args:
        n_positions: **これから建てた場合の**保有数。
        symbol_weight: その銘柄が総資産に占める比率。
    """
    if n_positions > max_concurrent:
        return BreakerState(
            tripped=True,
            action=BreakerAction.HALT,
            reason=f"同時保有 {n_positions} が上限 {max_concurrent} を超える",
            auto_resume=True,
        )
    if symbol_weight > max_weight_per_symbol:
        return BreakerState(
            tripped=True,
            action=BreakerAction.HALT,
            reason=(
                f"1銘柄の比重 {symbol_weight:.1%} が上限 "
                f"{max_weight_per_symbol:.1%} を超える"
            ),
            auto_resume=True,
        )
    return OK
