"""日次レポートの生成（docs/06-operations.md §2）。

人が毎日確認する唯一の成果物。所要5分で異常に気づける内容にする。

【必ず含める項目】
- **建玉残存数**（0 でなければ即対応。翌日2,200円のペナルティ）
- 損益と日次損失上限（-2%）への距離
- トレード数（想定 5〜15 から外れていないか）
- 発注エラー・リトライ回数
- reconcile 結果
- スリッページ実測（モデルとの乖離）
"""

from __future__ import annotations

from datetime import date
from pathlib import Path


def generate(trade_date: date, output_dir: Path) -> Path:
    """日次レポートを生成する。

    Returns:
        生成したレポートのパス。
    """
    raise NotImplementedError("Phase 3 で実装する")
