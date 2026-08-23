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
    **境界は既存の安全装置から導出してある**（docs/03-universe.md §2）::

        通常枠の上限   = 資金 ÷ max_concurrent(5) ÷ 100株 = 1,000円
        プレミアム上限 = 資金 × 上限比率(25%)      ÷ 100株 = 1,250円

    PREMIUM は同時保有が4銘柄に減る（分散が落ちる）ため、
    通常枠より高いスコアのハードルを課す。
    """

    NORMAL = "normal"
    """通常枠 300〜1,000円（1単元 3万〜10万円）。5銘柄同時に持てる"""
    PREMIUM = "premium"
    """プレミアム枠 1,000〜1,250円（1単元 10万〜12.5万円）。4銘柄に減る"""


@dataclass(frozen=True)
class Symbol:
    """銘柄。

    市場区分と信用区分は J-Quants の銘柄一覧（``equities/master``）から取れる。
    実測で確認済みで、**ユニバースのフィルタA・Dが代理指標なしに実装できる**
    （docs/03-universe.md §1）。分足しかない Stage A でも使える。
    """

    code: str
    """銘柄コード（例: "7203"）"""
    name: str
    lot_size: int = 100
    """単元株数。日本株は通常100株"""
    market: str | None = None
    """市場区分名（例: "プライム"）。フィルタA に使う。不明なら ``None``"""
    margin_type: str | None = None
    """信用区分名（例: "貸借" / "信用" / "-"）。

    **"貸借" なら制度信用で売建できる。** フィルタD の判定に使う。

    一般信用（デイトレ）の在庫は証券会社側の情報なので Stage A では取れないが、
    貸借銘柄かどうかは分かる。流動性による代理よりはるかに正確
    （docs/09-data-sources.md §3）。
    """
    sector: str | None = None
    """33業種区分名。将来のセクター分散に使う"""


@dataclass(frozen=True)
class Bar:
    """OHLCV バー（日足または分足）。

    ``turnover`` と値幅制限フラグは J-Quants の日足から取れる（実測で確認済み）。
    yfinance にはないので、そちら由来のバーでは ``None`` になる。
    """

    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    turnover: float | None = None
    """売買代金（円）。J-Quants の ``Va``。

    フィルタB（20日平均売買代金10億円以上）に使う。
    ``close * volume`` で近似せず、**実値があるならそれを使う**
    （寄与の大きい約定が偏った時間帯にあると近似が外れる）。
    """
    limit_up: bool | None = None
    """ストップ高だったか。J-Quants の ``UL``。フィルタH に使う"""
    limit_down: bool | None = None
    """ストップ安だったか。J-Quants の ``LL``。フィルタH に使う"""

    @property
    def effective_turnover(self) -> float:
        """売買代金。実値がなければ ``close * volume`` で近似する。

        yfinance 由来のバーには実値がないため、近似にフォールバックする。
        近似であることを呼び出し側が意識しなくて済むようにここで吸収する。
        """
        if self.turnover is not None:
            return self.turnover
        return self.close * self.volume


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
    entry_reason: str = ""
    """建玉を作ったシグナル名（``"orb"`` / ``"vwap_reversion"`` など）。

    **竹は複数のシグナルを混ぜている。** どれが効いてどれが効いていないかを
    分けて測れないと、全体の成績が悪いときに何を直せばよいか分からない。
    """
    entry_cost_yen: float = 0.0
    """建玉時に払ったスリッページ（円）。返済時に合算して `Trade.cost_yen` になる。"""

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
class Trade:
    """約定して手仕舞われた1トレード。

    **コスト込みの約定価格を保持する。** 建値と手仕舞い値を「理論価格」で
    持ってしまうと、集計のどこかでコストを引き忘れても気づけない。
    ここに入る時点で既にスリッページを含んでいる。
    """

    symbol: str
    side: Side
    quantity: int
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str
    """手仕舞いの理由。監査とデバッグのため必須（例: "stop" / "close_all"）。"""
    entry_reason: str = ""
    """建玉を作ったシグナル名。シグナル別に損益を分解するのに要る。"""
    cost_yen: float = 0.0
    """このトレードで払ったスリッページ（往復・円）。

    **`pnl` は既にこれを引いた後の値。** 二重に引かないこと。
    コスト前の損益を出すときだけ ``pnl + cost_yen`` を使う。
    """

    @property
    def gross_pnl(self) -> float:
        """コスト**前**の損益（円）。``net = gross - cost`` を解いただけ。"""
        return self.pnl + self.cost_yen

    @property
    def pnl(self) -> float:
        """損益（円）。ショートは方向を反転する。"""
        diff = self.exit_price - self.entry_price
        if self.side is Side.SHORT:
            diff = -diff
        return diff * self.quantity

    @property
    def notional(self) -> float:
        """建玉金額（円）。"""
        return self.entry_price * self.quantity

    @property
    def return_pct(self) -> float:
        """建玉金額に対する損益率。"""
        return self.pnl / self.notional if self.notional else 0.0


@dataclass(frozen=True)
class UniverseEntry:
    """ユニバースに含まれる銘柄と、その日のスコア。

    ``gap_pct`` と ``premarket_volume_ratio`` は寄り前気配が必要なため
    Stage A（口座なし）では ``None`` になる（docs/09-data-sources.md §5）。
    """

    symbol: Symbol
    trade_date: date
    price_tier: PriceTier
    score: float
    atr_pct: float
    prev_volume_ratio: float
    prev_range_pct: float
    prev_close_position: float
    gap_pct: float | None = None
    """寄り前ギャップ率。Stage B のみ"""
    premarket_volume_ratio: float | None = None
    """寄り前出来高比。Stage B のみ"""
