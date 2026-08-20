"""kabuステーションAPI の Broker 実装（docs/01-broker-api.md）。

【前提】
- Windows 専用。kabuステーションアプリが同一PCで常駐していること
- エンドポイントは http://localhost:18080/kabusapi（本番）/ 18081（検証）
- API パスワードは .env の KABUS_API_PASSWORD から読む（コードに書かない）

【制約】
- 発注系 5 req/秒、情報系 10 req/秒 → 自前スロットル必須
- WebSocket のリアルタイム配信は同時50銘柄まで
- 過去の分足は取得できない → data/recorder.py で自前蓄積する
"""

from __future__ import annotations

from autotrader.broker.base import Broker
from autotrader.types import AccountState, Order, Position, Quote


class RateLimiter:
    """API レート制限のスロットラー（安全装置 #8）。

    API側の制限に当てて弾かれるのではなく、こちらで待つ。
    """

    def __init__(self, per_second: int) -> None:
        self.per_second = per_second

    def acquire(self) -> None:
        """必要なら制限を満たすまで待つ。"""
        raise NotImplementedError("Phase 3 で実装する")


class KabusBroker(Broker):
    """kabuステーションAPI 経由の実発注。

    Phase 3 で実装する。実弾で使う前に risk-auditor の監査を通すこと
    （docs/08-development-harness.md）。
    """

    ORDER_RATE_LIMIT = 5
    """発注系のレート制限（req/秒）"""
    INFO_RATE_LIMIT = 10
    """情報系・残高系のレート制限（req/秒）"""
    MAX_WEBSOCKET_SYMBOLS = 50
    """WebSocket のリアルタイム配信上限。日次銘柄選定の枠を規定する"""

    def __init__(self, base_url: str, api_password: str) -> None:
        raise NotImplementedError("Phase 3 で実装する")

    def get_account(self) -> AccountState:
        raise NotImplementedError("Phase 3 で実装する")

    def get_positions(self) -> tuple[Position, ...]:
        raise NotImplementedError("Phase 3 で実装する")

    def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError("Phase 3 で実装する")

    def send_order(self, order: Order) -> Order:
        raise NotImplementedError("Phase 3 で実装する")

    def cancel_order(self, client_order_id: str) -> None:
        raise NotImplementedError("Phase 3 で実装する")

    def get_orders(self) -> tuple[Order, ...]:
        raise NotImplementedError("Phase 3 で実装する")

    def is_shortable(self, symbol: str) -> bool:
        raise NotImplementedError("Phase 3 で実装する")
