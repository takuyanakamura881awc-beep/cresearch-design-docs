"""竹 — デイトレード ロング/ショート（当日決済）。★メイン手法

docs/04-strategies.md の「竹」。Phase 4 で実装する。

===============  ==========================================================
項目             内容
===============  ==========================================================
保有期間         数十分〜大引け前（**当日決済必須**）
信用区分         デイトレ信用（手数料0・金利0・貸株料0）
ポジション       同時3〜5銘柄、各10〜15万円、1日5〜15トレード
期待成績         年利 20〜50% / 最大DD 15〜30%
必要データ       分足（自前蓄積）
===============  ==========================================================

【シグナルA】オープニングレンジ・ブレイクアウト（順張り）
    9:00-9:30 の高値/安値でレンジを定義し、そこを抜けたらエントリー。
    寄り付き後30分のレンジはその日の需給の綱引きの結果であり、
    それを抜けることは新しい方向へのエネルギーを示す、という仮説。

【シグナルB】VWAP乖離の平均回帰（逆張り）
    VWAP から一定以上乖離し、乖離が拡大から縮小に転じたらVWAP方向へエントリー。
    VWAP は機関投資家の執行基準になりやすく、回帰力が働く、という仮説。

【重要】シグナルAとBは方向が逆になる場面がある。
同一銘柄で両方が同時発火したら**エントリーを見送る**
（矛盾するシグナルは情報がない状態と扱う）。
"""

from __future__ import annotations

from datetime import datetime

from autotrader.strategy.base import Strategy
from autotrader.types import Bar, Position, Signal


class TakeIntraday(Strategy):
    """竹 — デイトレード ロング/ショート。

    パラメータは config/strategies.yaml の ``take_intraday``。
    数値はすべて暫定で、Phase 4 のウォークフォワード検証で確定する
    （in-sample で決めた成績は成績として数えない）。
    """

    def generate(
        self,
        now: datetime,
        bars: dict[str, tuple[Bar, ...]],
        positions: tuple[Position, ...],
    ) -> tuple[Signal, ...]:
        raise NotImplementedError("Phase 4 で実装する")

    def should_close(
        self, now: datetime, position: Position, bars: tuple[Bar, ...]
    ) -> tuple[bool, str]:
        raise NotImplementedError("Phase 4 で実装する")
