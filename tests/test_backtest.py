"""バックテストエンジンのテスト。

**最重要は PointInTimeView。** ルックアヘッドを「注意して書く」のではなく
「起こせない」ことをここで担保する（docs/03-universe.md §4.3）。
backtest-validator は、注意で防いでいるだけの実装を不合格とする。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from autotrader.engine.backtest import (
    STAGE_A_SLIPPAGE_BPS,
    BacktestResult,
    CostModel,
    PointInTimeView,
    Trade,
)
from autotrader.types import Bar, Side


def _bar(code: str, minute: int, close: float = 1000.0) -> Bar:
    """9:00 起点で ``minute`` 分後に**開始する**5分足。"""
    return Bar(
        symbol=code,
        timestamp=datetime(2026, 6, 1, 9, 0) + timedelta(minutes=minute),
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=1000,
    )


BARS = {
    "7203": tuple(_bar("7203", m, 1000.0 + m) for m in (0, 5, 10, 15, 20)),
    "6758": tuple(_bar("6758", m, 2000.0 + m) for m in (0, 5, 10)),
}


class TestPointInTimeView:
    def test_開始時刻が現在より前のバーだけ見える(self) -> None:
        """**タイムスタンプは期間の開始時刻。**

        09:10 のバーは 09:10〜09:15 を表すので、09:10 時点では未確定。
        含めると、これから起きる5分間の高値・安値・終値が見えてしまう。
        """
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 9, 10))
        seen = view.get("7203")
        assert [b.timestamp.minute for b in seen] == [0, 5]

    def test_最初の時刻では何も見えない(self) -> None:
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 9, 0))
        assert view.get("7203") == ()
        assert view.latest("7203") is None

    def test_全期間を過ぎれば全部見える(self) -> None:
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 23, 0))
        assert len(view.get("7203")) == 5

    def test_直近の確定バーを返す(self) -> None:
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 9, 12))
        latest = view.latest("7203")
        assert latest is not None
        assert latest.timestamp.minute == 10

    def test_知らない銘柄には空を返す(self) -> None:
        """履歴がまだない銘柄と扱いを揃える。戦略は「足りなければ何もしない」だけでよい。"""
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 9, 10))
        assert view.get("9999") == ()
        assert view.latest("9999") is None

    def test_戦略に渡す辞書も切り詰められている(self) -> None:
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 9, 10))
        as_dict = view.as_dict()
        assert set(as_dict) == {"7203", "6758"}
        assert all(
            b.timestamp < datetime(2026, 6, 1, 9, 10)
            for bars in as_dict.values()
            for b in bars
        )

    def test_未来のバーは辞書経由でも漏れない(self) -> None:
        """**as_dict は「全データを渡す抜け道」であってはならない。**"""
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 9, 5))
        assert len(view.as_dict()["7203"]) == 1

    def test_二度呼んでも同じ結果になる(self) -> None:
        """境界をキャッシュしているので、キャッシュが結果を変えないこと。"""
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 9, 10))
        assert view.get("7203") == view.get("7203")

    def test_元データを書き換えない(self) -> None:
        view = PointInTimeView(BARS, datetime(2026, 6, 1, 9, 10))
        view.get("7203")
        assert len(BARS["7203"]) == 5


class TestCostModel:
    def test_既定はStageAの20bps(self) -> None:
        """板がないぶん Stage B（10bps）より厚く見積もる。"""
        assert CostModel().slippage_bps == STAGE_A_SLIPPAGE_BPS == 20.0

    def test_ゼロを拒否する(self) -> None:
        """**手数料が0でもコストは0ではない**（CLAUDE.md 規約5）。"""
        with pytest.raises(ValueError, match="0以下"):
            CostModel(slippage_bps=0.0)
        with pytest.raises(ValueError):
            CostModel(slippage_bps=-1.0)

    @pytest.mark.parametrize(
        ("side", "opening", "expected"),
        [
            (Side.LONG, True, 1002.0),    # 新規買い → 高く約定
            (Side.LONG, False, 998.0),    # 返済売り → 安く約定
            (Side.SHORT, True, 998.0),    # 新規売り → 安く約定
            (Side.SHORT, False, 1002.0),  # 返済買い → 高く約定
        ],
    )
    def test_常に不利な側にずらす(self, side: Side, opening: bool, expected: float) -> None:
        cost = CostModel(slippage_bps=20.0)
        assert cost.fill_price(side, 1000.0, opening=opening) == pytest.approx(expected)

    def test_往復では2倍かかる(self) -> None:
        cost = CostModel(slippage_bps=20.0)
        entry = cost.fill_price(Side.LONG, 1000.0, opening=True)
        exit_ = cost.fill_price(Side.LONG, 1000.0, opening=False)
        assert (entry - exit_) / 1000.0 == pytest.approx(0.004)  # 40bps


def _trade(side: Side, entry: float, exit_: float, qty: int = 100) -> Trade:
    return Trade(
        symbol="7203",
        side=side,
        quantity=qty,
        entry_time=datetime(2026, 6, 1, 9, 0),
        entry_price=entry,
        exit_time=datetime(2026, 6, 1, 14, 50),
        exit_price=exit_,
        exit_reason="test",
    )


class TestTrade:
    def test_ロングの損益(self) -> None:
        assert _trade(Side.LONG, 1000.0, 1010.0).pnl == pytest.approx(1000.0)

    def test_ショートは方向が反転する(self) -> None:
        assert _trade(Side.SHORT, 1000.0, 990.0).pnl == pytest.approx(1000.0)
        assert _trade(Side.SHORT, 1000.0, 1010.0).pnl == pytest.approx(-1000.0)

    def test_建玉金額と損益率(self) -> None:
        t = _trade(Side.LONG, 1000.0, 1010.0)
        assert t.notional == pytest.approx(100_000.0)
        assert t.return_pct == pytest.approx(0.01)


class TestBacktestResult:
    def test_エクイティカーブから指標をまとめる(self) -> None:
        equity = [500_000.0 + i * 100 for i in range(30)]
        trades = [_trade(Side.LONG, 1000.0, 1010.0), _trade(Side.LONG, 1000.0, 995.0)]
        result = BacktestResult.from_equity(equity, trades, initial_cash=500_000.0)

        assert result.n_trades == 2
        assert result.win_rate == 0.5
        assert result.profit_factor == pytest.approx(1000.0 / 500.0)
        assert result.total_return == pytest.approx(equity[-1] / 500_000.0 - 1)
        assert result.max_drawdown == 0.0

    def test_トレードがなくても壊れない(self) -> None:
        result = BacktestResult.from_equity([500_000.0], [], initial_cash=500_000.0)
        assert result.n_trades == 0
        assert result.total_return == 0.0
        assert result.sharpe == 0.0

    def test_レバレッジ拒否を記録する(self) -> None:
        """安全装置が働いた回数。多いのはバグではなくサイジングの示唆。"""
        result = BacktestResult.from_equity(
            [500_000.0], [], initial_cash=500_000.0, rejected_by_leverage=7
        )
        assert result.rejected_by_leverage == 7
