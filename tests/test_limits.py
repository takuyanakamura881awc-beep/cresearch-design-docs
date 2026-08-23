"""損失ブレーカー三層とポジション制限のテスト（安全装置 #4/#5/#6/#7）。

**重点は `auto_resume` の真偽。** 層ごとに違い、ここを取り違えると
「人の承認が要る停止」が勝手に再開する。日次だけ True で、
連続と累積は False でなければならない。
"""

from __future__ import annotations

import pytest

from autotrader.risk.limits import (
    DEFAULT_ROLLING_LOSS_WINDOW,
    BreakerAction,
    check_daily_loss,
    check_max_drawdown,
    check_position_limits,
    check_rolling_loss,
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


class TestRollingLoss:
    """#5 移動窓の損失。**復帰には人の明示承認が必要。**

    **この装置は2度作り直している。両方の失敗を回帰テストとして残す。**

    | 版 | 条件 | 問題 |
    |---|---|---|
    | 初版 | 3営業日連続マイナス | 誤発動。勝っている戦略でも60営業日で93%発動 |
    | 2版 | 3連敗 かつ 累積 -5% | 見逃し。連続しない消耗を -15% まで検出できない |
    | 3版 | 直近15営業日の累積 -5% | 連続性を要求しない |
    """

    def test_窓の累積が閾値に達したら発動する(self) -> None:
        state = check_rolling_loss([-0.02] * 3)  # -6%
        assert state.tripped
        assert state.action is BreakerAction.HALT

    def test_自動復帰しない(self) -> None:
        """削られ続けているのは戦略が相場に合っていない兆候。人が判断する。"""
        assert check_rolling_loss([-0.02] * 3).auto_resume is False

    def test_連続していない消耗を検出する(self) -> None:
        """**2版が見逃したケース。**

        実データで -0.46%/日 の消耗が続き、3日連続では -1.37% にしかならず
        「3連敗かつ累積-5%」では発動しなかった。結果、累積DD の -15% まで
        誰も止めなかった。移動窓なら11日目で検出できる。
        """
        bleed = [-0.00456] * 10
        assert not check_rolling_loss(bleed).tripped  # 10日で -4.56%
        assert check_rolling_loss([*bleed, -0.00456]).tripped  # 11日で -5.02%

    def test_勝ち負けが混じっていても累積で判定する(self) -> None:
        """並び方ではなく削られた量で見る。

        境界ちょうど（合計 -5.0000%）は浮動小数の積算誤差で
        どちらに転ぶか決まらないので、テストには使わない。
        """
        mixed = [-0.02, 0.01, -0.02, 0.005, -0.02, -0.006]  # 合計 -5.1%
        assert check_rolling_loss(mixed).tripped
        # 3日連続の下落は含まれていない = 「連続」条件では検出できないケース
        assert not any(
            mixed[i] < 0 and mixed[i + 1] < 0 and mixed[i + 2] < 0
            for i in range(len(mixed) - 2)
        )

    def test_微小な3連敗では発動しない(self) -> None:
        """**初版が誤発動したケース。**

        実測の -0.87% / -0.62% / -0.54%（合計 -2.0%）は
        日次ブレーカー1回ぶんの損失に3日かけて到達しただけ。
        これで止めると勝っている戦略でも3ヶ月にほぼ確実に人の承認待ちに入る。
        """
        assert not check_rolling_loss([-0.0087, -0.0062, -0.0054]).tripped

    def test_窓の外の損失は引きずらない(self) -> None:
        """古い損失で永久に発動し続けない。"""
        old_damage = [-0.03] * 3
        recovered = [0.005] * DEFAULT_ROLLING_LOSS_WINDOW
        assert not check_rolling_loss([*old_damage, *recovered]).tripped

    def test_窓に満たなくても判定する(self) -> None:
        """**運用初日から効かせる。**

        15日揃うのを待つ間に -10% になっては意味がない。
        """
        assert check_rolling_loss([-0.06]).tripped
        assert "1営業日" in check_rolling_loss([-0.06]).reason

    def test_境界(self) -> None:
        assert not check_rolling_loss([-0.049]).tripped
        assert check_rolling_loss([-0.05]).tripped

    def test_利益が出ていれば発動しない(self) -> None:
        assert not check_rolling_loss([0.01] * 20).tripped
        assert not check_rolling_loss([]).tripped

    def test_理由に窓の長さと累積を残す(self) -> None:
        reason = check_rolling_loss([-0.03, -0.03]).reason
        assert "2営業日" in reason and "-6.00%" in reason

    def test_窓の長さを変えられる(self) -> None:
        bleed = [-0.01] * 10
        assert not check_rolling_loss(bleed, window_days=3).tripped  # -3%
        assert check_rolling_loss(bleed, window_days=10).tripped  # -10%

    def test_不正な引数を拒否する(self) -> None:
        with pytest.raises(ValueError):
            check_rolling_loss([-0.03], window_days=0)
        with pytest.raises(ValueError):
            check_rolling_loss([-0.03], threshold_pct=0.05)

    def test_日次と累積DDの背後は残る(self) -> None:
        """#5 の形を変えても守りは薄くならない。

        1日で -2% を割れば #4 が、ピークから -15% で #6 が止める。
        #5 が担うのは「その中間の、じわじわ削られる状態」の検出。
        """
        assert check_daily_loss(-0.02).tripped
        assert check_max_drawdown([500_000, 425_000]).tripped


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
