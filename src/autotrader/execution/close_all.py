"""全建玉クローズ（安全装置 #2）。**最重要コードパス。**

デイトレ信用の建玉を当日中に返済し損ねると、翌営業日に強制決済され
**1注文につき2,200円**が発生する。月利目標（日次約1,220円）の約2日分。

大引けの定時クローズと、キルスイッチ・損失ブレーカーによる緊急クローズの
**両方からここを呼ぶ**。最も重要なコードを1本に集約し、
ペーパー期間中に毎営業日（約60回）実行することで信頼性を稼ぐ。

滅多に走らないコードは、いざという時に動かない。

【手順】
1. 全建玉を成行でクローズ発注
2. ``broker.get_positions()`` で**残存を実測確認**（成功したはずと仮定しない）
3. 残存あり → 再試行 + アラート
"""

from __future__ import annotations

from dataclasses import dataclass

from autotrader.broker.base import Broker
from autotrader.types import Position


@dataclass(frozen=True)
class CloseAllResult:
    """クローズ処理の結果。

    ``residual`` が空でなければ**失敗**。アラートを上げて人が対応する
    （docs/06-operations.md §4）。翌営業日に持ち越してはならない。
    """

    attempted: tuple[Position, ...]
    closed: tuple[Position, ...]
    residual: tuple[Position, ...]
    """クローズできなかった建玉。空でなければ失敗"""
    retries: int

    @property
    def success(self) -> bool:
        return len(self.residual) == 0


def close_all(
    broker: Broker,
    reason: str,
    max_retries: int = 5,
    retry_interval_seconds: int = 10,
) -> CloseAllResult:
    """全建玉を成行でクローズし、残存がないことを実測確認する。

    部分約定・API障害・タイムアウトでも最終的に建玉が残らないことを
    保証しなければならない。test-writer は必ず障害注入テストを書くこと
    （.claude/agents/test-writer.md）。

    Args:
        broker: 接続先。
        reason: クローズ理由（"scheduled_close" / "killswitch" / "daily_loss" 等）。
            監査ログに残す。
        max_retries: 残存があった場合の再試行回数。
        retry_interval_seconds: 再試行の間隔。

    Returns:
        結果。``success`` が False ならアラートを上げること。
    """
    raise NotImplementedError("Phase 3 で実装する")
