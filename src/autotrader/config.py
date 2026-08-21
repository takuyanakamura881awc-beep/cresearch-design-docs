"""設定の読み込み。

- パラメータ … ``config/*.yaml``
- **認証情報 … 環境変数（``.env``）のみ**

【重要】認証情報を YAML に書かない。``.env`` は .gitignore 済み。
API パスワードや J-Quants の API キーがリポジトリに入ると、
履歴から消すのは困難になる。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
"""リポジトリのルート。``src/autotrader/config.py`` から2つ上。"""

CONFIG_DIR = PROJECT_ROOT / "config"


def load_dotenv(path: Path | None = None) -> None:
    """``.env`` を読み込んで環境変数に反映する。

    既に設定されている環境変数は**上書きしない**（実行環境の指定を優先する）。
    ファイルが無くても例外にしない（CI など環境変数で直接渡す構成があるため）。
    """
    path = path or (PROJECT_ROOT / ".env")
    if not path.is_file():
        return

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Credentials:
    """認証情報。環境変数からのみ読む。

    ``__repr__`` を上書きして値を出さない。dataclass の既定の ``__repr__`` は
    全フィールドを文字列化するため、そのままだとログや例外メッセージに
    APIキーが漏れる。
    """

    jquants_api_key: str
    kabus_api_password: str | None
    kabus_base_url: str

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"


def load_credentials(*, require_kabus: bool = False) -> Credentials:
    """``.env`` / 環境変数から認証情報を読む。

    Args:
        require_kabus: kabuステーションAPI の認証情報を必須にするか。
            **Stage A では False**（口座もAPIも不要なため）。
            Stage B で発注系を使うときに True にする。

    Raises:
        RuntimeError: 必須の環境変数が未設定の場合。
            **未設定のまま起動して実行時に落ちるより、起動時に止める。**
            場中に認証エラーで止まるのが最も避けたい失敗。
    """
    load_dotenv()

    jquants = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not jquants:
        raise RuntimeError(
            "JQUANTS_API_KEY が未設定。.env に設定するか環境変数で渡すこと。"
            " .env.example をコピーして作成する: cp .env.example .env"
        )

    kabus_password = os.environ.get("KABUS_API_PASSWORD", "").strip() or None
    if require_kabus and not kabus_password:
        raise RuntimeError(
            "KABUS_API_PASSWORD が未設定。Stage B（発注系）には必須。"
            " Stage A なら require_kabus=False で呼ぶこと"
        )

    return Credentials(
        jquants_api_key=jquants,
        kabus_api_password=kabus_password,
        kabus_base_url=os.environ.get(
            "KABUS_BASE_URL", "http://localhost:18080/kabusapi"
        ),
    )


def mask(secret: str, visible: int = 4) -> str:
    """認証情報をログ表示用にマスクする（``2NwY...`` の形）。

    検証スクリプトなどで「キーが読めているか」だけを確認したいときに使う。
    **完全な値を出力しない。**
    """
    if not secret:
        return "(未設定)"
    if len(secret) <= visible:
        return "*" * len(secret)
    return secret[:visible] + "..." + f"({len(secret)}文字)"


def load_yaml(name: str) -> dict[str, Any]:
    """``config/`` 配下の YAML を読む。

    Args:
        name: ``universe.yaml`` のようなファイル名、または絶対パス。
    """
    import yaml

    path = Path(name)
    if not path.is_absolute():
        path = CONFIG_DIR / name

    if not path.is_file():
        raise FileNotFoundError(f"設定ファイルが見つからない: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"設定ファイルの形式が不正（辞書ではない）: {path}")
    return data
