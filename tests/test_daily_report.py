"""日次レポートのテスト。

**人が毎日確認する唯一の成果物。所要5分で異常に気づけること。**
危険な順に上から並べる — 建玉が残っていることに気づくのが翌朝では遅い。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from autotrader.execution.journal import JournalEntry, OrderState
from autotrader.execution.reconcile import ReconcileResult
from autotrader.report.daily import DailySummary, generate, render
from autotrader.types import MarginTradeType, Position, Side, Trade

DAY = date(2026, 6, 1)
NOW = datetime(2026, 6, 1, 14, 50)


def _trade(pnl_sign: int = 1, reason: str = "take_profit") -> Trade:
    exit_price = 1100.0 if pnl_sign > 0 else 900.0
    return Trade(
        symbol="7203",
        side=Side.LONG,
        quantity=100,
        entry_time=NOW,
        entry_price=1000.0,
        exit_time=NOW,
        exit_price=exit_price,
        exit_reason=reason,
    )


def _position(symbol: str = "7203") -> Position:
    return Position(symbol, Side.LONG, 100, 1000.0, MarginTradeType.DAYTRADE, NOW)


def _entry(state: OrderState, order_id: str = "o1") -> JournalEntry:
    return JournalEntry(
        client_order_id=order_id,
        state=state,
        symbol="7203",
        side="long",
        quantity=100,
        reserved_at=NOW,
        payload={},
        context={},
    )


def _summary(**kwargs: object) -> DailySummary:
    base: dict[str, object] = {
        "trade_date": DAY,
        "starting_equity": Decimal(500_000),
        "ending_equity": Decimal(500_000),
    }
    base.update(kwargs)
    return DailySummary(**base)  # type: ignore[arg-type]


class TestPnl:
    def test_損益を計算する(self) -> None:
        s = _summary(ending_equity=Decimal(495_000))
        assert s.pnl == Decimal(-5_000)
        assert s.pnl_pct == -0.01

    def test_日次上限までの余裕(self) -> None:
        s = _summary(ending_equity=Decimal(495_000))
        assert s.headroom_pct == -0.01 - (-0.02)  # あと1%

    def test_上限到達を明示する(self) -> None:
        s = _summary(ending_equity=Decimal(490_000))
        assert "日次損失上限 -2% に到達している" in render(s)

    def test_余裕があれば距離を出す(self) -> None:
        assert "まで" in render(_summary(ending_equity=Decimal(499_000)))


class TestAlerts:
    """**最も高くつく異常を最初の数行に置く。**"""

    def test_残存建玉を先頭に出す(self) -> None:
        s = _summary(residual_positions=(_position(),))
        text = render(s)

        assert s.has_alert
        assert "要対応" in text
        assert "2,200円" in text
        # 損益より前に出る
        assert text.index("建玉が 1 件残っている") < text.index("## 損益")

    def test_送信不明の注文を出す(self) -> None:
        """**翌朝の起動時に照会しないと二重発注になる。**"""
        s = _summary(journal_entries=(_entry(OrderState.RESERVED),))
        text = render(s)

        assert s.has_alert
        assert "送信したか不明な注文が 1 件" in text
        assert "o1" in text

    def test_送信済みは要対応にしない(self) -> None:
        s = _summary(journal_entries=(_entry(OrderState.SENT),))
        assert not s.has_alert

    def test_突合の不一致を出す(self) -> None:
        result = ReconcileResult(
            matched=(), only_in_local=(), only_in_broker=(_position("9984"),)
        )
        s = _summary(reconcile=result)

        assert s.has_alert
        assert "翌日は発注しない" in render(s)

    def test_突合が一致していれば要対応にしない(self) -> None:
        result = ReconcileResult(matched=(_position(),), only_in_local=(), only_in_broker=())
        assert not _summary(reconcile=result).has_alert

    def test_異常がなければその旨を出す(self) -> None:
        text = render(_summary())
        assert "要対応なし" in text


class TestTrades:
    def test_件数と勝率を出す(self) -> None:
        s = _summary(trades=(_trade(1), _trade(-1), _trade(1)))
        text = render(s)

        assert s.win_rate == 2 / 3
        assert "件数: 3" in text
        assert "66.7%" in text

    def test_手仕舞い理由の内訳を出す(self) -> None:
        s = _summary(trades=(_trade(1, "take_profit"), _trade(-1, "stop")))
        assert "stop 1" in render(s) and "take_profit 1" in render(s)

    def test_トレードがなくても壊れない(self) -> None:
        assert "件数: 0" in render(_summary())

    def test_拒否された注文を出す(self) -> None:
        s = _summary(journal_entries=(_entry(OrderState.REJECTED),))
        assert "拒否された注文: 1件" in render(s)


class TestBreakers:
    def test_発動を出す(self) -> None:
        s = _summary(breakers_tripped=("日次損失 -2.10% が上限に到達",))
        assert "-2.10%" in render(s)

    def test_発動がなければ節を出さない(self) -> None:
        assert "## ブレーカー" not in render(_summary())


class TestGenerate:
    def test_ファイルに書く(self, tmp_path: Path) -> None:
        path = generate(_summary(), tmp_path)
        assert path.name == "2026-06-01.md"
        assert "日次レポート 2026-06-01" in path.read_text(encoding="utf-8")

    def test_ディレクトリを作る(self, tmp_path: Path) -> None:
        path = generate(_summary(), tmp_path / "reports" / "2026")
        assert path.is_file()

    def test_同じ日を上書きできる(self, tmp_path: Path) -> None:
        """障害復旧で再生成することがある。"""
        generate(_summary(), tmp_path)
        path = generate(_summary(notes=("再生成",)), tmp_path)
        assert "再生成" in path.read_text(encoding="utf-8")
