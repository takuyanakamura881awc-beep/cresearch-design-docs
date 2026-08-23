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

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_KILL_FILE = Path("KILL")


def is_triggered(kill_file: Path = DEFAULT_KILL_FILE) -> bool:
    """キルスイッチが発動しているか。

    **中身は見ない。存在だけで判定する。**
    空ファイルでも壊れたファイルでも ``touch KILL`` でも止まる必要がある。
    中身を要求すると、慌てて作ったファイルで止まらないという事故が起きうる。

    **例外を投げない。** 判定に失敗したら**発動しているとみなす**
    （権限エラーやディスク不調で「止まれない」より、
    誤って止まるほうが安全。CLAUDE.md 規約5）。
    """
    try:
        return kill_file.exists()
    except OSError as exc:
        logger.error(
            "キルスイッチの確認に失敗した。**発動とみなして停止する**: %s", exc
        )
        return True


def trigger(kill_file: Path = DEFAULT_KILL_FILE, reason: str = "") -> None:
    """キルスイッチを発動する。理由をファイルに書き残す。

    **既存のファイルを上書きしない。** すでに発動しているなら、
    最初に発動した理由のほうが原因に近い。

    ファイルの書き込みに失敗しても例外は投げるが、
    呼び出し側は**停止処理そのものを続けること**。
    記録できなかったことは停止しない理由にならない。
    """
    if kill_file.exists():
        logger.warning("キルスイッチは既に発動している。理由を上書きしない")
        return
    kill_file.parent.mkdir(parents=True, exist_ok=True)
    kill_file.write_text(
        f"{datetime.now().isoformat()}\n{reason}\n", encoding="utf-8"
    )
    logger.critical("キルスイッチを発動した: %s", reason)


def reason(kill_file: Path = DEFAULT_KILL_FILE) -> str:
    """発動理由。読めなければ空文字を返す。

    **読めないことを発動していない理由にしない。** 判定は `is_triggered`。
    """
    try:
        return kill_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def clear(kill_file: Path = DEFAULT_KILL_FILE) -> None:
    """キルスイッチを解除する。

    **人が明示的に実行することを想定する。自動で解除してはならない。**
    プログラムから呼ぶのはテストと `scripts/resume_trading.py` だけ。

    発動していない状態で呼んでも失敗しない（冪等）。
    """
    kill_file.unlink(missing_ok=True)
    logger.warning("キルスイッチを解除した")
