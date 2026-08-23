"""キルスイッチ（安全装置 #11）のテスト。

ファイルベースにするのは、**プロセスが応答不能でも確実に操作できる**から。
ネットワークもAPIも介さない。
"""

from __future__ import annotations

from pathlib import Path

from autotrader.risk.killswitch import clear, is_triggered, reason, trigger


class TestIsTriggered:
    def test_ファイルがあれば発動(self, tmp_path: Path) -> None:
        kill = tmp_path / "KILL"
        assert not is_triggered(kill)
        kill.touch()
        assert is_triggered(kill)

    def test_中身は見ない(self, tmp_path: Path) -> None:
        """**存在だけで判定する。**

        空でも壊れていても `touch KILL` でも止まる必要がある。
        中身を要求すると、慌てて作ったファイルで止まらない事故が起きうる。
        """
        kill = tmp_path / "KILL"
        kill.write_text("", encoding="utf-8")
        assert is_triggered(kill)

    def test_確認に失敗したら発動とみなす(self, tmp_path: Path) -> None:
        """**「止まれない」より「誤って止まる」ほうが安全**（CLAUDE.md 規約5）。"""

        class _Exploding(type(tmp_path)):  # type: ignore[misc]
            def exists(self) -> bool:
                raise OSError("権限がない")

        assert is_triggered(_Exploding(tmp_path / "KILL"))


class TestTrigger:
    def test_発動して理由を残す(self, tmp_path: Path) -> None:
        kill = tmp_path / "KILL"
        trigger(kill, "日次損失が上限に到達")

        assert is_triggered(kill)
        assert "日次損失が上限に到達" in reason(kill)

    def test_発動時刻を残す(self, tmp_path: Path) -> None:
        kill = tmp_path / "KILL"
        trigger(kill, "test")
        assert "2026-" in reason(kill) or "20" in reason(kill)

    def test_既存の理由を上書きしない(self, tmp_path: Path) -> None:
        """**最初に発動した理由のほうが原因に近い。**"""
        kill = tmp_path / "KILL"
        trigger(kill, "最初の理由")
        trigger(kill, "あとから来た理由")

        assert "最初の理由" in reason(kill)
        assert "あとから来た理由" not in reason(kill)

    def test_親ディレクトリがなくても作る(self, tmp_path: Path) -> None:
        kill = tmp_path / "state" / "KILL"
        trigger(kill, "test")
        assert is_triggered(kill)


class TestClear:
    def test_解除できる(self, tmp_path: Path) -> None:
        kill = tmp_path / "KILL"
        trigger(kill, "test")
        clear(kill)
        assert not is_triggered(kill)

    def test_発動していなくても失敗しない(self, tmp_path: Path) -> None:
        """冪等。再開スクリプトが二度走っても壊れない。"""
        clear(tmp_path / "KILL")


class TestReason:
    def test_読めなければ空文字(self, tmp_path: Path) -> None:
        """**読めないことを「発動していない」根拠にしない。**

        判定は `is_triggered` の責務。
        """
        assert reason(tmp_path / "KILL") == ""
