#!/usr/bin/env python3
"""ブレーカー発動後の取引再開（docs/06-operations.md §3.2）。

    python scripts/resume_trading.py --acknowledge "<再開理由>"

【人の明示承認が必要なブレーカー】
- 連続損失（3営業日連続マイナス）
- ドローダウン上限（ピークから -15%）

日次損失上限（-2%）は翌営業日に自動復帰するため、このスクリプトは不要。

【なぜ理由を必須にするか】
連続して負けているのは戦略が現在の相場に合っていない可能性を示す。
「なぜ再開してよいと判断したか」を監査ログに残し、後から辿れるようにする。
理由を書けないなら、まだ再開すべきではない。
"""

from __future__ import annotations

import sys


def main() -> int:
    raise NotImplementedError("Phase 3 で実装する")


if __name__ == "__main__":
    sys.exit(main())
