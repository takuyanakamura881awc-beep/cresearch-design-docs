#!/usr/bin/env python3
"""バックテスト結果から「どこまで届いているか」を数字で出す。

    python scripts/analyze_expectancy.py

**データを読まない。** `backtest_take.py` が出した集計値と、
戦略・コストの定数だけで計算する。データがない環境でも同じ数字が出る。

【なぜ勝率で評価するか】

総リターンは期間の長さと発動したブレーカーに依存するので、
39営業日の値をそのまま年率に伸ばしても意味が薄い。
一方、**損切り1.5×ATR / 利確2.5×ATR / 往復コスト**が決まっていれば、
「利益がゼロになる勝率」は計算で出る。実測の勝率がその上か下かは
期間の長さに依存しない判定になる。

【この計算の前提（近似であることを明示する）】

手仕舞いは損切り・利確だけではなく、保有180分の時間切れと
14:50 の強制クローズがある。それらは中途半端な価格で終わるので、
2値モデル（+2.5R / -1.5R）は**近似**。
近似に頼らない指標として PF（総利益 ÷ 総損失）も併記する。
"""

from __future__ import annotations

import math

# --- 戦略とコストの定数（実装から転記。変えたらここも変える） ---
STOP_ATR_MULT = 1.5  # strategy/take_intraday.py DEFAULT_STOP_ATR_MULT
TAKE_PROFIT_ATR_MULT = 2.5  # 同 DEFAULT_TAKE_PROFIT_ATR_MULT
SLIPPAGE_BPS_ONE_WAY = 20.0  # engine/backtest.py STAGE_A_SLIPPAGE_BPS
MEDIAN_ATR_PCT = 0.0333  # 実測（scripts/measure_universe.py §7 の中央値）
MAX_WEIGHT_PER_SYMBOL = 0.25  # 安全装置 #7
TRADES_PER_DAY = 13.0  # 実測 12.7〜13
BUSINESS_DAYS_PER_MONTH = 20

# --- 実測（scripts/backtest_take.py の出力を転記） ---
MEASURED = {
    "ブレーカー有効": dict(trades=152, win_rate=0.316, pf=0.53, ret=-0.0510, days=11),
    "ブレーカー無効": dict(trades=507, win_rate=0.333, pf=0.58, ret=-0.1600, days=39),
}


def wilson_interval(successes: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """勝率の95%信頼区間（Wilson score）。

    正規近似ではなく Wilson を使う。**サンプルが小さいと正規近似は
    区間が狭く出て、「有意に負けている」を作りやすい**（結論が甘くなる側）。
    """
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, center - margin), min(1.0, center + margin))


def cost_in_atr() -> float:
    """往復コストを ATR 何個ぶんかに換算する。"""
    round_trip = 2 * SLIPPAGE_BPS_ONE_WAY / 10_000.0
    return round_trip / MEDIAN_ATR_PCT


def required_win_rate(edge_per_trade_atr: float) -> float:
    """1トレードあたり ``edge`` (ATR単位) を得るのに要る勝率。

    p * 利確 - (1-p) * 損切り - コスト = edge を p について解く。
    """
    numerator = STOP_ATR_MULT + cost_in_atr() + edge_per_trade_atr
    return numerator / (TAKE_PROFIT_ATR_MULT + STOP_ATR_MULT)


def edge_for_monthly_target(monthly_return: float) -> float:
    """月利目標から1トレードあたりに要る優位（ATR単位）を逆算する。"""
    trades_per_month = TRADES_PER_DAY * BUSINESS_DAYS_PER_MONTH
    per_trade_equity = (1.0 + monthly_return) ** (1.0 / trades_per_month) - 1.0
    # 建玉は資金の 25%。資金に対する利益率を建玉に対する値幅へ戻す
    per_trade_position = per_trade_equity / MAX_WEIGHT_PER_SYMBOL
    return per_trade_position / MEDIAN_ATR_PCT


def monthly_from_period(total_return: float, days: int) -> float:
    """期間リターンを月次（20営業日）に換算する。"""
    return (1.0 + total_return) ** (BUSINESS_DAYS_PER_MONTH / days) - 1.0


def main() -> None:
    print("=" * 68)
    print("竹の期待パフォーマンス — 実測からの分析")
    print("=" * 68)

    print()
    print("■ 実測")
    for label, m in MEASURED.items():
        wins = m["win_rate"] * m["trades"]
        lo, hi = wilson_interval(wins, int(m["trades"]))
        print(
            f"  {label}: {m['trades']}トレード / 勝率 {m['win_rate']:.1%} "
            f"/ PF {m['pf']:.2f} / {m['ret']:+.2%}（{m['days']}営業日）"
        )
        print(f"    勝率の95%信頼区間: [{lo:.1%}, {hi:.1%}]")
        print(f"    月次換算: {monthly_from_period(m['ret'], int(m['days'])):+.2%}")

    breakeven = required_win_rate(0.0)
    print()
    print("■ 損益分岐の勝率")
    print(
        f"  往復コスト {2 * SLIPPAGE_BPS_ONE_WAY:.0f}bps ÷ ATR% {MEDIAN_ATR_PCT:.2%} "
        f"= {cost_in_atr():.3f} ATR"
    )
    print(
        f"  ({STOP_ATR_MULT} + {cost_in_atr():.3f}) / "
        f"({TAKE_PROFIT_ATR_MULT} + {STOP_ATR_MULT}) = **{breakeven:.1%}**"
    )

    measured = MEASURED["ブレーカー無効"]
    lo, hi = wilson_interval(measured["win_rate"] * measured["trades"], measured["trades"])
    verdict = "外側（構造的に負けている）" if hi < breakeven else "内側（判定不能）"
    print(f"  実測 {measured['win_rate']:.1%} の信頼区間 [{lo:.1%}, {hi:.1%}] は分岐点の{verdict}")

    print()
    print(
        f"■ 月利目標に要る勝率"
        f"（1日{TRADES_PER_DAY:.0f}トレード × {BUSINESS_DAYS_PER_MONTH}営業日）"
    )
    for target in (0.0, 0.05, 0.10):
        edge = edge_for_monthly_target(target)
        need = required_win_rate(edge)
        print(f"  月利 {target:+.0%} → 1トレード {edge:+.4f} ATR → 勝率 **{need:.1%}**")

    gap_be = breakeven - measured["win_rate"]
    gap_5 = required_win_rate(edge_for_monthly_target(0.05)) - breakeven
    print()
    print(
        f"  実測 {measured['win_rate']:.1%} → 損益分岐 {breakeven:.1%} まで "
        f"{gap_be * 100:.1f}ポイント"
    )
    print(f"  損益分岐 → 月利+5% は わずか {gap_5 * 100:.1f}ポイント")
    print("  **薄い優位を1ヶ月260回で増幅する設計。勝率にもコスト見積りにも敏感。**")

    print()
    print("■ PF から見た必要量（2値モデルに依存しない）")
    pf = measured["pf"]
    print(f"  実測 PF {pf:.2f} = 総利益は総損失の {pf:.0%}")
    print(f"  PF 1.0 にするには 総利益を {(1 / pf - 1):.0%} 増やす、")
    print(f"  または 総損失を {(1 - pf):.0%} 減らす必要がある")

    print()
    print("=" * 68)
    print("注意: パラメータは未検証（Phase 4 で決める）。")
    print("      これは『現在の設定の成績』であって『戦略の成否』ではない。")
    print("=" * 68)


if __name__ == "__main__":
    main()
