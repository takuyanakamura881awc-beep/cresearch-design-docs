"""ローカルデータストアと TTL 付きキャッシュ。

- バー … Parquet（列指向で分析に向く）
- メタ情報・TTL … SQLite（トランザクションが要る）

``data/`` は .gitignore 済み。データファイルをコミットしない。

【キャッシュが果たす2つの役割】

1. **レート制限対策** — 同じデータを何度も取りに行かない。
   yfinance は短時間に大量のリクエストを投げるとIPブロックされる。
   バックテストを繰り返し回すとき、毎回取得していたら即座に弾かれる。

2. **データソース障害時の緩衝材** — yfinance は非公式APIで、仕様変更により
   **数日間データが取れなくなる**ことがある。TTL 内のデータが残っていれば、
   API が一時的に死んでいても直近のデータで走り続けられる。
   「昨日のデータで走らせるか、何も走らせないか」なら前者のほうが実用的。

【冪等性】

同じ期間を再取得しても壊れない。銘柄×足×日付の組で上書きする。
再実行が安全でないと、失敗したバッチを気軽にやり直せない。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Hashable
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from autotrader.types import Bar

logger = logging.getLogger(__name__)

DEFAULT_PRICE_TTL_DAYS = 1
"""価格データの TTL。日足・分足とも、1日経てば取り直す。"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_log (
    symbol      TEXT NOT NULL,
    interval    TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    n_bars      INTEGER NOT NULL,
    PRIMARY KEY (symbol, interval, start_date, end_date)
);
"""


class BarStore:
    """OHLCV バーの永続化と TTL 管理。

    ファイル配置::

        <root>/bars/<interval>/<symbol>.parquet
        <root>/meta.sqlite3
    """

    def __init__(self, root: Path, ttl_days: int = DEFAULT_PRICE_TTL_DAYS) -> None:
        self._root = Path(root)
        self._bars_dir = self._root / "bars"
        self._db_path = self._root / "meta.sqlite3"
        self._ttl = timedelta(days=ttl_days)

        self._bars_dir.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _parquet_path(self, symbol: str, interval: str) -> Path:
        directory = self._bars_dir / interval
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{symbol}.parquet"

    # ------------------------------------------------------------------
    # TTL
    # ------------------------------------------------------------------

    def is_fresh(
        self,
        symbol: str,
        interval: str,
        start: date,
        end: date,
        *,
        now: datetime | None = None,
    ) -> bool:
        """キャッシュが TTL 内か。

        True なら API を叩かずにキャッシュを使ってよい。
        """
        now = now or datetime.now()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT fetched_at FROM fetch_log "
                "WHERE symbol=? AND interval=? AND start_date=? AND end_date=?",
                (symbol, interval, start.isoformat(), end.isoformat()),
            ).fetchone()

        if row is None:
            return False
        try:
            fetched_at = datetime.fromisoformat(row[0])
        except ValueError:
            return False
        return (now - fetched_at) < self._ttl

    def record_fetch(
        self,
        symbol: str,
        interval: str,
        start: date,
        end: date,
        source: str,
        n_bars: int,
        *,
        now: datetime | None = None,
    ) -> None:
        """取得したことを記録する。どのソースから取ったかも残す。

        フォールバックが発動した場合、後から「この期間は yfinance 由来」と
        追跡できる必要がある。品質差の診断に使う。
        """
        now = now or datetime.now()
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fetch_log "
                "(symbol, interval, start_date, end_date, source, fetched_at, n_bars) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    symbol,
                    interval,
                    start.isoformat(),
                    end.isoformat(),
                    source,
                    now.isoformat(),
                    n_bars,
                ),
            )
            conn.commit()

    def sources_used(self, symbol: str, interval: str) -> list[tuple[str, str, str]]:
        """その銘柄・足で使われたソースの履歴。

        Returns:
            ``(start_date, end_date, source)`` の列。
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT start_date, end_date, source FROM fetch_log "
                "WHERE symbol=? AND interval=? ORDER BY start_date",
                (symbol, interval),
            ).fetchall()
        return [(str(a), str(b), str(c)) for a, b, c in rows]

    # ------------------------------------------------------------------
    # 読み書き
    # ------------------------------------------------------------------

    def write(self, symbol: str, interval: str, bars: tuple[Bar, ...]) -> int:
        """バーを書き込む。

        既存データと結合し、``timestamp`` の重複は**新しい方で上書き**する。
        同じ期間を再取得しても壊れない（冪等）。

        Returns:
            書き込み後の総バー数。
        """
        if not bars:
            return 0

        import pandas as pd

        frame = pd.DataFrame(
            [
                {
                    "timestamp": b.timestamp,
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
                for b in bars
            ]
        )

        path = self._parquet_path(symbol, interval)
        if path.is_file():
            existing = pd.read_parquet(path)
            # 新しい方を残すため、既存を先に置いて keep="last" で落とす
            frame = pd.concat([existing, frame], ignore_index=True)

        frame = (
            frame.drop_duplicates(subset=["timestamp"], keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )
        frame.to_parquet(path, index=False)
        return len(frame)

    def read(
        self,
        symbol: str,
        interval: str,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[Bar, ...]:
        """バーを読み出す。範囲を省略すると全期間。"""
        path = self._parquet_path(symbol, interval)
        if not path.is_file():
            return ()

        import pandas as pd

        frame = pd.read_parquet(path)
        if frame.empty:
            return ()

        ts = pd.to_datetime(frame["timestamp"])
        if start is not None:
            frame = frame[ts >= pd.Timestamp(start)]
            ts = pd.to_datetime(frame["timestamp"])
        if end is not None:
            frame = frame[ts <= pd.Timestamp(end) + pd.Timedelta(days=1)]

        return tuple(_row_to_bar(symbol, record) for record in frame.to_dict("records"))

    def coverage(self, symbol: str, interval: str) -> tuple[date, date] | None:
        """保存済みバーの期間。データがなければ ``None``。

        「どこまで貯まっているか」を知るために使う。
        5分足の蓄積量が Stage A の検証期間を律速するため、
        進捗の可視化に必要。
        """
        bars = self.read(symbol, interval)
        if not bars:
            return None
        return (bars[0].timestamp.date(), bars[-1].timestamp.date())

    def symbols(self, interval: str) -> tuple[str, ...]:
        """その足で保存されている銘柄コード一覧。"""
        directory = self._bars_dir / interval
        if not directory.is_dir():
            return ()
        return tuple(sorted(p.stem for p in directory.glob("*.parquet")))


def _row_to_bar(symbol: str, record: dict[Hashable, Any]) -> Bar:
    """Parquet の1レコードを ``Bar`` に変換する。

    pandas の値は numpy 型や Timestamp で返るため、明示的に Python の型へ落とす。
    """
    raw_ts = record["timestamp"]
    timestamp = (
        raw_ts.to_pydatetime() if hasattr(raw_ts, "to_pydatetime") else raw_ts
    )
    if not isinstance(timestamp, datetime):
        raise ValueError(f"timestamp を datetime に変換できない: {raw_ts!r}")

    return Bar(
        symbol=symbol,
        timestamp=timestamp,
        open=float(record["open"]),
        high=float(record["high"]),
        low=float(record["low"]),
        close=float(record["close"]),
        volume=int(record["volume"]),
    )
