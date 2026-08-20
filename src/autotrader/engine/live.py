"""ライブ/ペーパー共通の実行ループ。

``Broker`` 実装を差し替えるだけでペーパーと実弾が切り替わる。
**同じコードパスを通すことが、シミュレーションと本番の乖離を防ぐ唯一の確実な方法。**

【毎ループで確認すること】
1. キルスイッチ（``KILL`` ファイル）— 発動していれば即座に全手仕舞い
2. 損失ブレーカー（日次 / 連続 / 累積）
3. ハートビート（プロセス死活・API疎通・kabuステーション常駐）
"""

from __future__ import annotations

from autotrader.broker.base import Broker
from autotrader.strategy.base import Strategy


class LiveEngine:
    """ライブ/ペーパーの実行ループ。"""

    def __init__(self, broker: Broker, strategy: Strategy) -> None:
        raise NotImplementedError("Phase 3 で実装する")

    def run(self) -> None:
        """実行ループを開始する。

        起動時に必ず reconcile を実行し、不一致なら発注せずに停止する。
        状態のズレを抱えたまま自動発注を続けるのが最も危険。
        """
        raise NotImplementedError("Phase 3 で実装する")


class Heartbeat:
    """死活監視（安全装置 #16）。

    プロセスが死んだこと自体を検知する。
    kabuステーションAPI はローカル常駐アプリが提供するため、
    **PC が落ちればシステムも落ちる**。
    """

    def check(self) -> tuple[bool, str]:
        """プロセス・API疎通・kabuステーション常駐を確認する。

        Returns:
            (正常か, 詳細)。
        """
        raise NotImplementedError("Phase 3 で実装する")
