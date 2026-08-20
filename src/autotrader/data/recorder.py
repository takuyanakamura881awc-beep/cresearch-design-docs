"""分足のリアルタイム蓄積（WebSocket → Parquet）。**Stage B。**

Stage A では yfinance が5分足を過去60日分まとめて返すため、
このモジュールを待たずに竹の検証を始められる（docs/09-data-sources.md §2.3）。

本モジュールの用途は Stage B:

- 5分足より細かい粒度が必要になった場合
- 実運用でのリアルタイム記録（ペーパー/実弾の約定分析）

【求められる性質】
- **毎営業日、確実に動く**（起動失敗を検知して通知する）
- **取りこぼしを検出できる**（欠損バーがわかる形で記録する）
- **プロセスが落ちてもそれまでのデータが失われない**（逐次書き出し）
- **冪等**（同じ日を再取得しても壊れない）
"""

from __future__ import annotations

from datetime import date
from pathlib import Path


class Recorder:
    """WebSocket から分足を受け取り Parquet に蓄積する。

    kabuステーションAPI の WebSocket 配信は同時50銘柄まで。
    Layer 2 で選定した監視銘柄を寄り前に登録する。
    """

    def __init__(self, output_dir: Path, symbols: tuple[str, ...]) -> None:
        raise NotImplementedError("Phase 1 で実装する")

    def run(self) -> None:
        """場中、分足を受信して逐次書き出す。

        プロセスが落ちてもそれまでのデータが残るよう、
        メモリに溜め込まず逐次フラッシュすること。
        """
        raise NotImplementedError("Phase 1 で実装する")


def check_gaps(output_dir: Path, trade_date: date) -> tuple[str, ...]:
    """指定日の分足に欠損がある銘柄を返す。

    取りこぼしを検出できないと、欠損に気づかないまま
    バックテストの前提が崩れる。週次でチェックする（docs/06-operations.md §5）。

    Returns:
        欠損が見つかった銘柄コード。
    """
    raise NotImplementedError("Phase 1 で実装する")
