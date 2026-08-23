"""損失ブレーカー三層とポジション制限のテスト（安全装置 #4/#5/#6/#7）。

**重点は `auto_resume` の真偽。** 層ごとに違い、ここを取り違えると
「人の承認が要る停止」が勝手に再開する。日次だけ True で、
連続と累積は False でなければならない。
"""

from __future__ import annotations

import pytest

from autotrader.risk.limits import (
    BreakerAction,
    check_consecutive_loss,
    check_daily_loss,
    check_max_drawdown,
    check_position_limits,
    max_atr_pct,
)


class TestDailyLoss:
    """#4 日次損失上限。翌営業日に自動復帰する。"""

    def test_上限に達したら全停止して全クローズ(self) -> None:
        state = check_daily_loss(-0.02)
        assert state.tripped
        assert state.action is BreakerAction.HALT_AND_CLOSE

    def test_翌営業日に自動復帰する(self) -> None:
        """日次だけは人の承認なしで復帰してよい。"""
        assert check_daily_loss(-0.03).auto_resume is True

    def test_境界の内側では発動しない(self) -> None:
        assert not check_daily_loss(-0.0199).tripped
        assert check_daily_loss(-0.02).tripped

    def test_利益が出ていれば発動しない(self) -> None:
        assert not check_daily_loss(0.05).tripped
        assert not check_daily_loss(0.0).tripped

    def test_理由を残す(self) -> None:
        """監査ログのため、なぜ止まったかが後から読めること。"""
        assert "-2.50%" in check_daily_loss(-0.025).reason

    def test_正の閾値を拒否する(self) -> None:
        """符号を取り違えると、常に発動するか一度も発動しないかになる。"""
        with pytest.raises(ValueError):
            check_daily_loss(-0.01, threshold_pct=0.02)


class TestConsecutiveLoss:
    """#5 連続損失。**復帰には人の明示承認が必要。**"""

    def test_3営業日連続マイナスで発動する(self) -> None:
        state = check_consecutive_loss([-0.01, -0.005, -0.02])
        assert state.tripped
        assert state.action is BreakerAction.HALT

    def test_自動復帰しない(self) -> None:
        """**連敗は戦略が相場に合っていない兆候。**

        自動再開すると損失を垂れ流す。人が判断する。
        """
        assert check_consecutive_loss([-0.01] * 3).auto_resume is False

    def test_間に勝ちがあればリセットされる(self) -> None:
        assert not check_consecutive_loss([-0.01, 0.02, -0.01]).tripped

    def test_直近3日だけを見る(self) -> None:
        """古い連敗を引きずらない。"""
        assert not check_consecutive_loss([-0.01, -0.01, -0.01, 0.02]).tripped

    def test_損益ゼロは連敗を途切れさせる(self) -> None:
        """**docs/05 の定義は「3営業日連続マイナス」。ゼロはマイナスではない。**

        実務上ゼロになるのは**その日1トレードもしなかった場合**で、
        「戦略が相場に合っていない」という判定根拠にならない。
        連敗に数えると、様子見の日を挟んだだけで人の承認待ちに入る。
        """
        assert not check_consecutive_loss([-0.01, 0.0, -0.01]).tripped

    def test_日数が足りなければ発動しない(self) -> None:
        assert not check_consecutive_loss([-0.01, -0.01]).tripped
        assert not check_consecutive_loss([]).tripped

    def test_閾値日数を変えられる(self) -> None:
        assert check_consecutive_loss([-0.01, -0.01], threshold_days=2).tripped

    def test_不正な日数を拒否する(self) -> None:
        with pytest.raises(ValueError):
            check_consecutive_loss([-0.01], threshold_days=0)


class TestMaxDrawdown:
    """#6 累積ドローダウン。**復帰には人の明示承認が必要。**"""

    def test_ピークから15パーセント下げたら発動する(self) -> None:
        state = check_max_drawdown([500_000, 425_000])
        assert state.tripped
        assert state.action is BreakerAction.HALT_AND_CLOSE

    def test_自動復帰しない(self) -> None:
        assert check_max_drawdown([500_000, 400_000]).auto_resume is False

    def test_境界の内側では発動しない(self) -> None:
        assert not check_max_drawdown([500_000, 425_500]).tripped  # -14.9%
        assert check_max_drawdown([500_000, 425_000]).tripped  # -15.0%

    def test_ピークを更新してからの下落で測る(self) -> None:
        """含み益を出したあとの下落も対象。取得価格からではない。"""
        assert check_max_drawdown([500_000, 600_000, 510_000]).tripped  # -15%

    def test_上がり続ければ発動しない(self) -> None:
        assert not check_max_drawdown([500_000, 550_000, 600_000]).tripped

    def test_点が足りなければ発動しない(self) -> None:
        assert not check_max_drawdown([500_000]).tripped
        assert not check_max_drawdown([]).tripped

    def test_正の閾値を拒否する(self) -> None:
        with pytest.raises(ValueError):
            check_max_drawdown([500_000], threshold_pct=0.15)


class TestPositionLimits:
    """#7 ポジション制限。**新規建てを止めるだけで既存建玉には触れない。**"""

    def test_上限内なら通す(self) -> None:
        assert not check_position_limits(5, 0.25).tripped

    def test_同時保有数を超えたら止める(self) -> None:
        state = check_position_limits(6, 0.10)
        assert state.tripped
        assert state.action is BreakerAction.HALT

    def test_1銘柄の比重を超えたら止める(self) -> None:
        state = check_position_limits(2, 0.26)
        assert state.tripped
        assert "26.0%" in state.reason

    def test_上限到達は異常ではないので自動復帰する(self) -> None:
        """想定内の制約であって、人の判断を要する事象ではない。"""
        assert check_position_limits(6, 0.10).auto_resume is True

    def test_50万円で25パーセントは1単元1250円ぶん(self) -> None:
        """`risk.sizing.max_affordable_price` と同じ制約を別方向から見ている。"""
        assert not check_position_limits(1, 125_000 / 500_000).tripped
        assert check_position_limits(1, 125_100 / 500_000).tripped


class TestMaxAtrPct:
    """ATR% 上限の導出。**定数を直書きせず安全装置から計算する。**"""

    def test_日次ブレーカーからの導出(self) -> None:
        assert max_atr_pct() == pytest.approx(0.02 / (0.25 * 1.5))
        assert max_atr_pct() == pytest.approx(0.05333, abs=1e-5)

    def test_安全装置を動かせば追随する(self) -> None:
        assert max_atr_pct(daily_breaker_pct=0.03) == pytest.approx(0.08)
        assert max_atr_pct(max_weight_per_symbol=0.20) == pytest.approx(0.0666, abs=1e-4)
        assert max_atr_pct(stop_atr_mult=2.0) == pytest.approx(0.04)

    def test_不正な引数を拒否する(self) -> None:
        with pytest.raises(ValueError):
            max_atr_pct(max_weight_per_symbol=0)
        with pytest.raises(ValueError):
            max_atr_pct(stop_atr_mult=-1)
