"""実行結果に「どのコードで出したか」を刻む。

【なぜ要るのか】

約定モデルもコストモデルも選定スコアも何度も直しており、**同じスクリプトの
出力でも版が違えば数字がまったく違う**。実際に総リターンは
-5.10% → -5.21% → -17.26% → -35.94% と動いている。

過去のログを見返したとき、その数字がどの版のものか分からないと
**比較に使えないどころか、古い数字を現状だと誤認する**。
実際に「コスト前 +22.5〜+34.7%」という古い推定を根拠に議論を進めてしまった。

**測定結果には必ず版を添える。**

【dirty を強調する理由】

未コミットの変更があると、その出力は**どのコミットにも対応しない**。
あとから再現しようとしても、そのときのワークツリーはもう存在しない。
docs に数字を写す前に気づけるよう、目立つ形で出す。

【失敗させない】

来歴の記録は測定の付随物であって、本体ではない。
git が無い・リポジトリでない・コマンドが失敗した——どの場合も
**例外を投げずに「不明」と報告して続行する**。
ここで測定が止まるのは本末転倒。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

__all__ = ["Revision", "banner", "revision"]

_TIMEOUT_SECONDS = 5
"""git 呼び出しのタイムアウト。**測定を待たせない。**"""


@dataclass(frozen=True)
class Revision:
    """実行時点のリポジトリの状態。"""

    commit: str
    """短縮コミットハッシュ。"""
    committed_at: datetime
    """そのコミットの作成時刻（タイムゾーン付き）。"""
    branch: str
    """ブランチ名。detached HEAD なら ``"HEAD"``。"""
    dirty: bool
    """**未コミットの変更があるか。**

    True なら、この出力はどのコミットにも対応しない。
    数字を docs に写す前にコミットすること。
    """

    def __str__(self) -> str:
        stamp = self.committed_at.strftime("%Y-%m-%d %H:%M:%S %z")
        text = f"{self.commit} ({stamp}) on {self.branch}"
        if self.dirty:
            text += "  **未コミットの変更あり — この出力は再現できない**"
        return text


def _git(*args: str, cwd: Path | None = None) -> str | None:
    """git を呼んで標準出力を返す。失敗したら ``None``。

    **例外を外に出さない。** 呼び出し側は「取れなかった」だけを扱えばよい。
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # git が無い / 実行できない / タイムアウト
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def revision(cwd: Path | None = None) -> Revision | None:
    """いまのリポジトリの版。取れなければ ``None``。

    Args:
        cwd: git を実行するディレクトリ。省略時はカレント。

    Returns:
        版の情報。git が無い・リポジトリでない・情報が欠けている場合は ``None``。

    Note:
        **例外を投げない。** 来歴が取れないことで測定を止めない。
    """
    commit = _git("rev-parse", "--short", "HEAD", cwd=cwd)
    if not commit:
        return None

    # ISO 8601 strict。fromisoformat がそのまま食える形式で受け取る
    raw_date = _git("show", "-s", "--format=%cI", "HEAD", cwd=cwd)
    if not raw_date:
        return None
    try:
        committed_at = datetime.fromisoformat(raw_date)
    except ValueError:
        return None

    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd) or "HEAD"

    # --porcelain は変更があれば非空を返す。**None（失敗）と "" を区別する**
    status = _git("status", "--porcelain", cwd=cwd)
    if status is None:
        return None

    return Revision(
        commit=commit,
        committed_at=committed_at,
        branch=branch,
        dirty=bool(status),
    )


def banner(cwd: Path | None = None) -> str:
    """スクリプトの先頭に出す1行。**必ず何かを返す。**"""
    current = revision(cwd)
    if current is None:
        return "版: 不明（git から取得できなかった。この出力は再現できない）"
    return f"版: {current}"
