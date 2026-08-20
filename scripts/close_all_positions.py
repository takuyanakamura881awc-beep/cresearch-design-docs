#!/usr/bin/env python3
"""全建玉を成行でクローズする緊急スクリプト（docs/06-operations.md §3）。

    python scripts/close_all_positions.py

【設計方針】このスクリプトは**戦略ロジックを一切通さない**。
建玉を成行でクローズすることだけを行う。

システムの他の部分が壊れていても動くように、依存を最小化する。
戦略・ユニバース・スケジューラを import しない。

【これも効かない場合】
証券会社の Web/アプリから手動でクローズする。これが唯一確実な最終手段。
スマートフォンに証券会社アプリを入れ、ログインできる状態にしておくこと。
**緊急時に初めて操作するのでは遅いので、平時に一度試しておく。**
"""

from __future__ import annotations

import sys


def main() -> int:
    raise NotImplementedError("Phase 3 で実装する")


if __name__ == "__main__":
    sys.exit(main())
