"""プロジェクト全体で共有するドメイン型。

金額は円（int）、株数は株（int）、比率は float で扱う。
株価は単元株の制約上 int で足りるが、平均取得単価などは float を使う。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum


class Side(Enum):
    """売買方向。"""

    LONG = "long"
    SHORT = "short"


class OrderType(Enum):
    """注文の種類。"""

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class CashMargin(Enum):
    """現物/信用の区分。kabuステーションAPI の CashMargin パラメータに対応。"""

    CASH = 1
    """現物"""
    MARGIN_OPEN = 2
    """信用新規"""
    MARGIN_CLOSE = 3
    """信用返済"""


class MarginTradeType(Enum):
    """信用取引の区分。kabuステーションAPI の MarginTradeType に対応。

    本プロジェクトの主軸は DAYTRADE（手数料0・金利0・貸株料0）。
    ただし当日中に必ず返済しないと翌営業日に強制決済され1注文2,200円が発生する
    （docs/02-margin-rules.md §3）。
    """

    SYSTEM = "system"
    """制度信用"""
    GENERAL_LONG = "general_long"
    """一般信用（長期）"""
    DAYTRADE = "daytrade"
    """デイトレ信用（当日決済必須）"""


class OrderStatus(Enum):
    """注文の状態。"""

    PENDING = "pending"
    """発注前（注文IDを採番して永続化した段階）"""
    SUBMITTED = "submitted"
    WORKING = "working"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class PriceTier(Enum):
    """株価レンジの枠（docs/03-universe.md §2）。

    50万円という資金では単元100株の制約が銘柄選定を強く縛る。
    PREMIUM は1単元が資金の40〜60%を占めるため、
    通常枠より高いスコアのハードルを課す。
    """

    NORMAL = "normal"
    """通常枠 300〜2,000円（1単元 3万〜20万円）"""
    PREMIUM = "premium"
    """プレミアム枠 2,000〜3,000円（1単元 20万〜30万円）"""


@dataclass(frozen=True)
class Symbol:
    """銘柄。"""

    code: str
    """銘柄コード（例: "7203"）"""
    name: str
    lot_size: int = 100
    """単元株数。日本株は通常100株"""


@dataclass(frozen=True)
class Bar:
    """OHLCV バー（日足または分足）。"""

    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class Quote:
    """気配・時価のスナップショット。"""

    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    last: float
    bid_size: int
    ask_size: int

    @property
    def spread(self) -> float:
        """スプレッド（円）。実質コストの一部なので必ず考慮する。"""
        return self.ask - self.bid


@dataclass(frozen=True)
class Order:
    """注文。

    ``client_order_id`` は発注前にクライアント側で採番し永続化する。
    リトライ時はこのIDで照会してから再発注することで、二重発注を構造的に防ぐ
    （docs/05-risk-management.md #9）。
    """

    client_order_id: str
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType
    cash_margin: CashMargin
    margin_trade_type: MarginTradeType | None = None
    limit_price: float | None = None
    trigger_price: float | None = None
    broker_order_id: str | None = None
    status: OrderStatus = OrderStatus.PENDING


@dataclass(frozen=True)
class Position:
    """建玉。"""

    symbol: str
    side: Side
    quantity: int
    entry_price: float
    margin_trade_type: MarginTradeType
    opened_at: datetime
    stop_order_id: str | None = None
    """逆指値ストップの注文ID。

    ショート建玉ではこれが None であってはならない
    （docs/05-risk-management.md #3）。
    """

    @property
    def notional(self) -> Decimal:
        """建玉金額（円）。レバレッジ判定に使う。"""
        return Decimal(str(self.entry_price)) * self.quantity


@dataclass(frozen=True)
class AccountState:
    """口座の状態。レバレッジ1倍の判定に使う。"""

    cash: Decimal
    """現金残高（円）"""
    positions: tuple[Position, ...]
    as_of: datetime

    @property
    def gross_notional(self) -> Decimal:
        """建玉総額（買建 + 売建の合計）。

        レバレッジ1倍の不変条件は ``gross_notional <= cash``
        （docs/02-margin-rules.md §1）。ロングとショートを相殺しない点に注意。
        """
        return sum((p.notional for p in self.positions), Decimal(0))


@dataclass(frozen=True)
class Signal:
    """戦略が生成する売買シグナル。

    戦略はシグナルを返すだけで、発注はしない。
    サイジングとリスクチェックを経て初めて注文になる。
    """

    symbol: str
    side: Side
    strength: float
    """シグナルの強さ（0.0〜1.0）。同時に複数出たときの優先順位に使う。"""
    reason: str
    """発火したシグナル名。監査ログとデバッグのため必須。"""
    stop_price: float | None = None
    take_profit_price: float | None = None


@dataclass(frozen=True)
class UniverseEntry:
    """ユニバースに含まれる銘柄と、その日のスコア。"""

    symbol: Symbol
    trade_date: date
    price_tier: PriceTier
    score: float
    atr_pct: float
    gap_pct: float
    premarket_volume_ratio: float
