"""実行結果に刻む版情報のテスト。

**来歴が取れないことで測定を止めてはならない。**
git が無い環境でも、リポジトリでない場所でも、例外を投げずに続行すること。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autotrader.provenance import Revision, banner, revision


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """コミットが1つある使い捨てリポジトリ。"""
    _git("init", "-b", "main", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    (tmp_path / "a.txt").write_text("one", encoding="utf-8")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-m", "first", cwd=tmp_path)
    return tmp_path


class TestRevision:
    def test_きれいなリポジトリから版を取れる(self, repo: Path) -> None:
        got = revision(repo)
        assert got is not None
        assert got.branch == "main"
        assert got.dirty is False
        assert len(got.commit) >= 7
        assert got.committed_at.tzinfo is not None

    def test_未コミットの変更を検出する(self, repo: Path) -> None:
        """**ここが本題。** dirty な出力はどのコミットにも対応しない。"""
        (repo / "a.txt").write_text("two", encoding="utf-8")
        got = revision(repo)
        assert got is not None
        assert got.dirty is True

    def test_未追跡ファイルも変更とみなす(self, repo: Path) -> None:
        """`git status --porcelain` は未追跡も出す。

        新しいスクリプトを足しただけの状態も「再現できない」に含める。
        """
        (repo / "new.py").write_text("x = 1", encoding="utf-8")
        got = revision(repo)
        assert got is not None
        assert got.dirty is True

    def test_リポジトリでなければNoneを返す(self, tmp_path: Path) -> None:
        """**例外を投げない。** 測定を止めないため。"""
        assert revision(tmp_path) is None


class TestBanner:
    def test_必ず1行返す(self, repo: Path) -> None:
        text = banner(repo)
        assert text.startswith("版: ")
        assert "\n" not in text

    def test_取れなくても文字列を返す(self, tmp_path: Path) -> None:
        text = banner(tmp_path)
        assert text.startswith("版: 不明")
        assert "再現できない" in text

    def test_dirtyなら警告が入る(self, repo: Path) -> None:
        """数字を docs に写す前に気づけるよう、目立たせる。"""
        clean = banner(repo)
        (repo / "a.txt").write_text("changed", encoding="utf-8")
        soiled = banner(repo)
        assert "未コミット" not in clean
        assert "未コミット" in soiled


class TestFormatting:
    def test_文字列表現にコミットとブランチが入る(self) -> None:
        from datetime import UTC, datetime

        rev = Revision(
            commit="abc1234",
            committed_at=datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
            branch="feature/x",
            dirty=False,
        )
        text = str(rev)
        assert "abc1234" in text
        assert "feature/x" in text
        assert "2026-08-24" in text
