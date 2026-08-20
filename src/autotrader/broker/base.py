"""Broker 抽象インターフェース。

このインターフェースを実装すれば、戦略・リスク管理・執行のコードを
一切変えずに接続先を差し替えられる。

【設計上の要求】
- 実装は必ずレート制限を自前で守る（発注5req/s、情報10req/s）。
  API側の制限に当てて弾かれるのではなく、こちらで待つ。
- ``send_order`` は冪等でなければならない。同じ ``client_order_id`` で
  二度呼ばれても、注文が二つ作られてはならない。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from autotrader.types import AccountState, Order, Position, Quote


class BrokerError(Exception):
    """証券会社との通信・発注で発生したエラー。"""


class OrderRejectedError(BrokerError):
    """注文が証券会社に拒否された。"""


class Broker(ABC):
    """証券会社の抽象インターフェース。"""

    @abstractmethod
    def get_account(self) -> AccountState:
        """口座状態（現金残高と建玉）を取得する。

        レバレッジ1倍の判定に使うため、**推測値ではなく実測値**を返すこと。
        """

    @abstractmethod
    def get_positions(self) -> tuple[Position, ...]:
        """建玉一覧を取得する。

        大引けクローズ後の残存確認と起動時 reconcile で使う。
        「クローズしたはず」と仮定せず、必ずこれで実測する。
        """

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        """気配・時価を取得する。"""

    @abstractmethod
    def send_order(self, order: Order) -> Order:
        """注文を送信する。

        冪等でなければならない。``client_order_id`` が既に送信済みなら、
        再送せず既存の注文状態を返すこと（docs/05-risk-management.md #9）。

        Raises:
            OrderRejectedError: 証券会社に拒否された場合。
        """

    @abstractmethod
    def cancel_order(self, client_order_id: str) -> None:
        """注文を取り消す。"""

    @abstractmethod
    def get_orders(self) -> tuple[Order, ...]:
        """注文一覧を取得する。リトライ前の重複確認に使う。"""

    @abstractmethod
    def is_shortable(self, symbol: str) -> bool:
        """一般信用（デイトレ）で売建可能か。

        売建可能銘柄は在庫次第で日々変動する。
        空売り価格規制の有無もここで判定する（docs/02-margin-rules.md §4）。
        """
