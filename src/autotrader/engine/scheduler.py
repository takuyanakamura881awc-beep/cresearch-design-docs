"""場中スケジュール（docs/06-operations.md §1）。

=========  ==========================================================
時刻       処理
=========  ==========================================================
07:00      ユニバース構築（Layer 1）
08:00      日次銘柄選定（Layer 2）→ WebSocket 50銘柄を登録
08:55      **起動時 reconcile**（不一致ならその日は発注しない）
09:00      場開始。分足 Recorder 稼働、シグナル監視開始
09:30      オープニングレンジ確定 → シグナルA の判定開始
**14:50**  **全建玉クローズ → GET /positions で残存確認**
15:30      場終了
16:00      日次レポート生成・通知
=========  ==========================================================
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import time


class MarketScheduler:
    """場中のジョブスケジューラ。

    14:50 の全建玉クローズは**最優先で、他のジョブに阻害されず実行**されること。
    閉じ損ねると翌営業日に1注文2,200円のペナルティが発生する。
    """

    MARKET_OPEN = time(9, 0)
    MARKET_CLOSE = time(15, 30)
    CLOSE_ALL_TIME = time(14, 50)
    """全建玉クローズの時刻。**暫定値。**

    大引け直前は板が薄くスリッページが増えるため、
    ペーパー期間の実測で調整する。
    """

    def register(self, at: time, job: Callable[[], None], name: str) -> None:
        """時刻指定でジョブを登録する。"""
        raise NotImplementedError("Phase 3 で実装する")

    def run(self) -> None:
        """スケジューラを起動する。"""
        raise NotImplementedError("Phase 3 で実装する")
