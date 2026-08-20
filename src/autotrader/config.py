"""設定の読み込み。

- パラメータ … ``config/*.yaml``
- **認証情報 … 環境変数（``.env``）のみ**

【重要】認証情報を YAML に書かない。``.env`` は .gitignore 済み。
API パスワードや J-Quants の API キーがリポジトリに入ると、
履歴から消すのは困難になる。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Credentials:
    """認証情報。環境変数からのみ読む。

    ``__repr__`` に値を出さないこと（ログに漏れる）。
    """

    kabus_api_password: str
    kabus_base_url: str
    jquants_api_key: str

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"


def load_credentials() -> Credentials:
    """``.env`` / 環境変数から認証情報を読む。

    Raises:
        RuntimeError: 必須の環境変数が未設定の場合。
            未設定のまま起動して実行時に落ちるより、起動時に止める。
    """
    raise NotImplementedError("Phase 1 で実装する")


def load_yaml(path: Path) -> dict[str, Any]:
    """YAML 設定を読む。"""
    raise NotImplementedError("Phase 1 で実装する")
