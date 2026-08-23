"""発注の永続記録（安全装置 #9 と #15）。

**冪等性（#9）と監査ログ（#15）を1つのテーブルで担う。**
別々に持つと、片方だけ書けた状態が生まれて整合が取れなくなる。

【なぜ発注「前」に書くのか】

ネットワーク断は必ず起きる。``send_order`` の応答が返らなかったとき、
注文が通ったのか通らなかったのかは呼び出し側から区別できない。

発注前に注文IDを永続化しておけば、再起動後に
「送ったかもしれない注文」を列挙して照会できる。
発注後に書く設計だと、**まさに落ちた瞬間の注文だけが記録から漏れる** —
そしてそれが二重発注になる。

【SQLite を使う理由】

プロセスが落ちてもデータが残り、書き込みがアトミックであること。
JSON ファイルだと書き込み途中で落ちたときに壊れる。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from autotrader.types import Order

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    reserved_at     TEXT NOT NULL,
    state           TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    payload         TEXT NOT NULL,
    context         TEXT NOT NULL,
    broker_order_id TEXT,
    updated_at      TEXT,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state);
"""


class OrderState(Enum):
    """注文の記録上の状態。

    **``RESERVED`` は「送ったか不明」を意味する。** 送っていないのではない。
    再起動時はこの状態のものを証券会社に照会してから再発注を判断する。
    """

    RESERVED = "reserved"
    """IDを採番して永続化した。**発注したかどうかは不明**"""
    SENT = "sent"
    """証券会社が受け付けた"""
    REJECTED = "rejected"
    """証券会社に拒否された。再発注してよい"""
    ABANDONED = "abandoned"
    """人が確認して破棄した。**自動でこの状態にしない**"""


@dataclass(frozen=True)
class JournalEntry:
    """1注文の記録。**事後に完全再現できる粒度**で持つ（#15）。"""

    client_order_id: str
    state: OrderState
    symbol: str
    side: str
    quantity: int
    reserved_at: datetime
    payload: dict[str, object]
    """注文の内容そのもの。"""
    context: dict[str, object]
    """発注時の判断材料（シグナル理由・口座状態・レバレッジ判定など）。

    **これがないと「なぜこの注文を出したか」を後から再現できない。**
    """
    broker_order_id: str | None = None
    note: str = ""


class OrderJournal:
    """発注記録の読み書き。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def reserve(self, order: Order, context: dict[str, object]) -> None:
        """発注**前**に注文IDを永続化する。

        **同じIDで二度予約できない。** 二重発注を構造的に防ぐ土台なので、
        既存IDでの再予約は例外にする（黙って上書きすると記録が消える）。

        Raises:
            ValueError: 同じ ``client_order_id`` が既にある場合。
        """
        payload = {
            "symbol": order.symbol,
            "side": order.side.value,
            "quantity": order.quantity,
            "order_type": order.order_type.value,
            "cash_margin": order.cash_margin.value,
            "margin_trade_type": (
                order.margin_trade_type.value if order.margin_trade_type else None
            ),
            "limit_price": order.limit_price,
            "trigger_price": order.trigger_price,
        }
        try:
            with closing(self._connect()) as conn:
                conn.execute(
                    "INSERT INTO orders (client_order_id, reserved_at, state, symbol,"
                    " side, quantity, payload, context) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        order.client_order_id,
                        datetime.now().isoformat(),
                        OrderState.RESERVED.value,
                        order.symbol,
                        order.side.value,
                        order.quantity,
                        json.dumps(payload, ensure_ascii=False),
                        json.dumps(context, ensure_ascii=False, default=str),
                    ),
                )
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"注文ID {order.client_order_id} は既に予約済み。"
                "同じIDで二度発注してはならない（安全装置 #9）"
            ) from exc

    def mark(
        self,
        client_order_id: str,
        state: OrderState,
        *,
        broker_order_id: str | None = None,
        note: str = "",
    ) -> None:
        """状態を更新する。知らないIDなら例外。

        **予約なしに状態を書けないようにする。** 書けてしまうと
        「発注前に予約する」という規律が形骸化する。
        """
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "UPDATE orders SET state=?, broker_order_id=?, updated_at=?, note=?"
                " WHERE client_order_id=?",
                (
                    state.value,
                    broker_order_id,
                    datetime.now().isoformat(),
                    note,
                    client_order_id,
                ),
            )
            conn.commit()
        if cursor.rowcount == 0:
            raise ValueError(f"予約されていない注文ID: {client_order_id}")

    def get(self, client_order_id: str) -> JournalEntry | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT client_order_id, state, symbol, side, quantity, reserved_at,"
                " payload, context, broker_order_id, note FROM orders"
                " WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
        return _to_entry(row) if row else None

    def unresolved(self) -> tuple[JournalEntry, ...]:
        """**送ったか不明**な注文（``RESERVED`` のまま）。

        起動時にこれを列挙し、証券会社に照会してから再発注を判断する。
        空でないまま自動売買を始めてはならない。
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT client_order_id, state, symbol, side, quantity, reserved_at,"
                " payload, context, broker_order_id, note FROM orders"
                " WHERE state=? ORDER BY reserved_at",
                (OrderState.RESERVED.value,),
            ).fetchall()
        return tuple(_to_entry(row) for row in rows)

    def all_entries(self) -> tuple[JournalEntry, ...]:
        """全記録。日次レポートと事後検証に使う。"""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT client_order_id, state, symbol, side, quantity, reserved_at,"
                " payload, context, broker_order_id, note FROM orders"
                " ORDER BY reserved_at"
            ).fetchall()
        return tuple(_to_entry(row) for row in rows)


def _to_entry(row: tuple[Any, ...]) -> JournalEntry:
    """SQLite の行を `JournalEntry` にする。列順は SELECT と一致させること。"""
    return JournalEntry(
        client_order_id=str(row[0]),
        state=OrderState(str(row[1])),
        symbol=str(row[2]),
        side=str(row[3]),
        quantity=int(row[4]),
        reserved_at=datetime.fromisoformat(str(row[5])),
        payload=dict(json.loads(str(row[6]))),
        context=dict(json.loads(str(row[7]))),
        broker_order_id=str(row[8]) if row[8] is not None else None,
        note=str(row[9]) if row[9] is not None else "",
    )
