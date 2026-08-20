"""ローカルデータストア（Parquet + SQLite）。

- 分足・日足 … Parquet（列指向で分析に向く）
- 注文状態・運用ログ … SQLite（トランザクションが要る）

``data/`` は .gitignore 済み。データファイルをコミットしない。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from autotrader.types import Bar


class BarStore:
    """OHLCV バーの永続化。"""

    def __init__(self, root: Path) -> None:
        raise NotImplementedError("Phase 1 で実装する")

    def write(self, bars: tuple[Bar, ...]) -> None:
        """バーを書き込む。同じ期間を再取得しても壊れない冪等な実装にすること。"""
        raise NotImplementedError("Phase 1 で実装する")

    def read(
        self, symbol: str, start: date, end: date, interval: str = "1d"
    ) -> tuple[Bar, ...]:
        """バーを読み出す。"""
        raise NotImplementedError("Phase 1 で実装する")
