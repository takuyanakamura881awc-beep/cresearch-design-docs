"""キルスイッチ（安全装置 #11）。

ファイルの存在で即時停止する。

ファイルベースにする理由: プロセスが応答不能でも、ファイルシステムなら
確実に操作できる。ネットワークもAPIも介さない。

    # 即時停止（次のループで検知 → 全手仕舞い）
    touch KILL

APIやプロセスが完全に死んでいる場合は、これも効かない。
その場合は scripts/close_all_positions.py、それも駄目なら
証券会社の Web/アプリから手動でクローズする（docs/06-operations.md §4）。
"""

from __future__ import annotations

from pathlib import Path


def is_triggered(kill_file: Path = Path("KILL")) -> bool:
    """キルスイッチが発動しているか。"""
    raise NotImplementedError("Phase 3 で実装する")


def trigger(kill_file: Path = Path("KILL"), reason: str = "") -> None:
    """キルスイッチを発動する。理由をファイルに書き残す。"""
    raise NotImplementedError("Phase 3 で実装する")


def clear(kill_file: Path = Path("KILL")) -> None:
    """キルスイッチを解除する。

    人が明示的に実行することを想定する。自動で解除してはならない。
    """
    raise NotImplementedError("Phase 3 で実装する")
