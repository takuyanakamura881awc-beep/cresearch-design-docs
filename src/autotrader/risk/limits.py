"""損失ブレーカーとポジション制限（安全装置 #4/#5/#6/#7）。

ブレーカーは三層構造（docs/05-risk-management.md）:

===============  =========================  =========================  ==================
層               発動条件                   動作                       復帰
===============  =========================  =========================  ==================
日次             当日 -2%                   当日全停止 + 全クローズ    翌営業日に自動
移動窓           直近15営業日で -5%         自動停止                   人の明示承認
累積             ピークから -15%            全停止                     人の明示承認
===============  =========================  =========================  ==================

**規模も期間も段階的**に置いてある（-2%/1日 → -5%/15日 → -15%/期間無制限）。
日次は自動復帰、移動窓と累積は人の判断を要求する。
削られ続けているのは戦略が現在の相場に合っていない可能性があり、
自動再開すると損失を垂れ流すため。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from autotrader.report.metrics import max_drawdown

DEFAULT_DAILY_BREAKER_PCT = 0.02
"""日次損失上限（#4）。総資産のこの割合を失ったら当日全停止する。"""

DEFAULT_MAX_WEIGHT_PER_SYMBOL = 0.25
"""1銘柄あたり総資産の上限（#7）。"""

DEFAULT_ROLLING_LOSS_PCT = -0.05
"""移動窓ブレーカー（#5）が発動する累積損失。

日次上限 -2% の **2.5倍** / 累積DD上限 -15% の **1/3**。
三層が規模も期間も段階的になるように置いた::

    #4 日次      -2%   1営業日
    #5 移動窓    -5%   直近15営業日
    #6 累積DD   -15%   ピークから（期間の制限なし）
"""

DEFAULT_ROLLING_LOSS_WINDOW = 15
"""移動窓の営業日数。約3週間。

短すぎると通常の変動で発動し、長すぎると #6 に先を越されて意味がなくなる。
実測の消耗（-0.46%/日）なら**11日目で検出**でき、#6 の34日目より大幅に早い。

【この装置の変更履歴 — 同じ形に戻さないために残す】

===  ==============================  ==========================================
版   条件                            問題
===  ==============================  ==========================================
初   3営業日連続マイナス             **誤発動**。勝っている戦略でも
                                     60営業日で93%発動する（モンテカルロ）
2    3連敗 かつ 累積 -5%             **見逃し**。連続しない消耗を検出できず、
                                     実測で -15% まで気づかなかった
3    直近15営業日の累積 -5%          連続性を要求しない
===  ==============================  ==========================================

判定するのは「一定期間でどれだけ削られたか」であって、その並び方ではない。
じわじわ負ける戦略は連続で大きく負けないので、
「連続」を条件にすると検出器として働かない。
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


def max_atr_yen(
    capital: Decimal,
    daily_breaker_pct: float = DEFAULT_DAILY_BREAKER_PCT,
    stop_atr_mult: float = DEFAULT_STOP_ATR_MULT,
    lot_size: int = 100,
) -> Decimal:
    """**建てられる建玉の ATR の上限（円）。同時保有数に依存しない。**

    `max_atr_pct` は「1敗が日次ブレーカーに届かない ATR%」で、
    `sizing.max_affordable_price` は「1単元が上限比率に収まる株価」。
    この2つを掛けると建玉比率が**約分で消える**::

        株価上限   = 資金 × 比率 ÷ 単元
        ATR%上限   = ブレーカー ÷ (比率 × 損切倍率)
        ────────────────────────────────────────────
        ATR円上限  = 資金 × ブレーカー ÷ (単元 × 損切倍率)

        = 500,000 × 0.02 ÷ (100 × 1.5) = 66.7円

    【なぜこれが重要か】

    往復コストは ``スプレッド円 ÷ ATR円``（`autotrader.tick`）。
    つまり **ATR円の上限が、達成しうるコストの下限を決めている**。

    そして上の式に建玉比率が出てこない以上、
    **同時保有数を絞ってもコストの理論的な下限は1ミリも動かない。**
    集中すると高い株が買えるが（tick に有利）、同時に ATR% の上限が
    下がる（不利）。この2つが正確に打ち消し合う。

    ただし**実在する銘柄の分布は変わる**。同時2銘柄なら
    「2,200円 かつ ATR 2.0%」で天井に近づけるが、これはプライムの
    中大型に普通にある。同時4銘柄で天井に届くには
    「1,250円 かつ ATR 5.33%」が要り、こちらは極端に荒い小型株で稀。
    **どちらの母集団が厚いかは計算では決まらないので実測する**
    （`scripts/measure_universe.py` セクション8）。

    Raises:
        ValueError: 資金・損切り倍率・単元が正の値でない場合。
    """
    if capital <= 0:
        raise ValueError(f"資金は正の値である必要がある: {capital}")
    if stop_atr_mult <= 0:
        raise ValueError(f"損切り倍率は正の値である必要がある: {stop_atr_mult}")
    if lot_size < 1:
        raise ValueError(f"単元株数は1以上である必要がある: {lot_size}")
    return (
        capital
        * Decimal(str(daily_breaker_pct))
        / (Decimal(lot_size) * Decimal(str(stop_atr_mult)))
    )


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


def check_rolling_loss(
    daily_pnls: list[float],
    window_days: int = DEFAULT_ROLLING_LOSS_WINDOW,
    threshold_pct: float = DEFAULT_ROLLING_LOSS_PCT,
) -> BreakerState:
    """直近 N 営業日の累積損失を判定する（#5）。**復帰には人の明示承認が必要。**

    一定期間で削られ続けているのは戦略が現在の相場に合っていない可能性があり、
    自動再開すると損失を垂れ流す。だから ``auto_resume`` は False。

    **連続性を要求しない。** 判定するのは「どれだけ削られたか」であって
    その並び方ではない（`DEFAULT_ROLLING_LOSS_WINDOW` の変更履歴を参照）。

    Args:
        daily_pnls: 日次損益率の列。**時系列の昇順**（末尾が直近）。
        window_days: 何営業日ぶんを見るか。
        threshold_pct: 累積がこれ以下なら発動。**負の値**で渡す。

    Note:
        **日数が窓に満たないうちは、あるぶんだけで判定する。**
        運用初日から効かせたい。15日揃うのを待つ間に -10% になっては
        意味がない。
    """
    if window_days < 1:
        raise ValueError(f"window_days は1以上: {window_days}")
    if threshold_pct >= 0:
        raise ValueError(f"threshold_pct は負の値: {threshold_pct}")
    if not daily_pnls:
        return OK

    recent = daily_pnls[-window_days:]
    cumulative = sum(recent)
    if cumulative > threshold_pct:
        return OK

    return BreakerState(
        tripped=True,
        action=BreakerAction.HALT,
        reason=(
            f"直近{len(recent)}営業日の累積 {cumulative:.2%} が "
            f"上限 {threshold_pct:.2%} に到達"
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
