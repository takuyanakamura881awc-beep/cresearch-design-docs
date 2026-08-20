"""Strategy 抽象インターフェース。

【設計方針】
- **戦略はシグナルを返すだけで、発注はしない。**
  サイジングとリスクチェックを経て初めて注文になる。
  これにより、戦略のテストが発注を伴わずに済む。
- long / short 両対応。信用取引を使うので空売りが可能。
- 保有期間の違い（数秒〜数週間）を吸収できる形にする。

【ルックアヘッドの防止】
``generate`` に渡される ``bars`` は、バックテストエンジンが
**その時点で参照可能なデータだけに制限したもの**でなければならない。
戦略側の注意ではなく、エンジン側の構造で防ぐ（docs/03-universe.md §4.3）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from autotrader.types import Bar, Position, Signal


class Strategy(ABC):
    """取引手法の抽象インターフェース。"""

    @abstractmethod
    def generate(
        self,
        now: datetime,
        bars: dict[str, tuple[Bar, ...]],
        positions: tuple[Position, ...],
    ) -> tuple[Signal, ...]:
        """シグナルを生成する。

        Args:
            now: 現在時刻。
            bars: 銘柄コード → その時点までのバー列。
                **未来のバーが含まれていてはならない。**
            positions: 現在の建玉。手仕舞いシグナルの判断に使う。

        Returns:
            売買シグナル。発注はしない。
        """

    @abstractmethod
    def should_close(
        self, now: datetime, position: Position, bars: tuple[Bar, ...]
    ) -> tuple[bool, str]:
        """建玉を手仕舞うべきか判定する。

        Returns:
            (手仕舞うか, 理由)。理由は監査ログのため必ず返す。

        Note:
            大引けの全建玉クローズ（14:50）は戦略の判断とは独立に、
            ``execution.close_all`` が無条件で実行する。
            戦略がここで False を返しても関係なくクローズされる。
        """
