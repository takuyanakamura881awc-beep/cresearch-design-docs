"""場中スケジューラのテスト。

**14:50 の全建玉クローズが他のジョブに阻害されないこと**が最重要。
閉じ損ねると翌営業日に1注文2,200円。
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from autotrader.engine.scheduler import MarketScheduler


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, 1, hour, minute)


class TestRegister:
    def test_時刻順に並ぶ(self) -> None:
        s = MarketScheduler()
        s.register(time(14, 50), lambda: None, "close_all")
        s.register(time(7, 0), lambda: None, "universe")
        assert [j.name for j in s.jobs()] == ["universe", "close_all"]

    def test_名前の重複を拒否する(self) -> None:
        """**同じジョブが2回走ると、クローズなら二重返済を試みる。**"""
        s = MarketScheduler()
        s.register(time(9, 0), lambda: None, "start")
        with pytest.raises(ValueError, match="重複"):
            s.register(time(10, 0), lambda: None, "start")


class TestDue:
    def _scheduler(self) -> MarketScheduler:
        s = MarketScheduler()
        s.register(time(7, 0), lambda: None, "universe")
        s.register(time(8, 55), lambda: None, "reconcile")
        s.register(time(14, 50), lambda: None, "close_all", critical=True)
        return s

    def test_時刻前は返さない(self) -> None:
        assert self._scheduler().due(_at(6, 0)) == ()

    def test_過ぎたものはすべて返す(self) -> None:
        """**「ちょうどその時刻」でしか拾わない設計だと、
        プロセスが数分止まっただけで 14:50 のクローズを飛ばす。**
        """
        due = self._scheduler().due(_at(15, 0))
        assert {j.name for j in due} == {"universe", "reconcile", "close_all"}

    def test_criticalを先に返す(self) -> None:
        """同じ時点で複数あるとき、クローズが後ろに回らないこと。"""
        due = self._scheduler().due(_at(15, 0))
        assert due[0].name == "close_all"

    def test_実行済みは返さない(self) -> None:
        s = self._scheduler()
        s.run_once(_at(9, 0))
        assert {j.name for j in s.due(_at(9, 30))} == set()

    def test_日が変われば未実行に戻る(self) -> None:
        s = self._scheduler()
        s.run_once(_at(9, 0))
        tomorrow = datetime(2026, 6, 2, 9, 0)
        assert {j.name for j in s.due(tomorrow)} == {"universe", "reconcile"}


class TestRunOnce:
    def test_実行する(self) -> None:
        calls: list[str] = []
        s = MarketScheduler()
        s.register(time(7, 0), lambda: calls.append("universe"), "universe")
        assert s.run_once(_at(8, 0)) == ("universe",)
        assert calls == ["universe"]

    def test_同じ日に二度実行しない(self) -> None:
        calls: list[str] = []
        s = MarketScheduler()
        s.register(time(7, 0), lambda: calls.append("x"), "universe")
        s.run_once(_at(8, 0))
        s.run_once(_at(9, 0))
        assert calls == ["x"]

    def test_1つの失敗で他を止めない(self) -> None:
        """**止めると、後続の 14:50 クローズまで巻き添えになる。**"""

        def boom() -> None:
            raise RuntimeError("失敗")

        calls: list[str] = []
        s = MarketScheduler()
        s.register(time(8, 0), boom, "broken")
        s.register(time(14, 50), lambda: calls.append("closed"), "close_all", critical=True)

        executed = s.run_once(_at(15, 0))
        assert set(executed) == {"broken", "close_all"}
        assert calls == ["closed"]

    def test_失敗しても実行済みとして扱う(self) -> None:
        """再試行はジョブ自身の責務。スケジューラが毎ループ叩き続けない。"""

        calls: list[str] = []

        def boom() -> None:
            calls.append("tried")
            raise RuntimeError("失敗")

        s = MarketScheduler()
        s.register(time(8, 0), boom, "broken")
        s.run_once(_at(9, 0))
        s.run_once(_at(10, 0))
        assert calls == ["tried"]

    def test_criticalが先に走る(self) -> None:
        order: list[str] = []
        s = MarketScheduler()
        s.register(time(7, 0), lambda: order.append("universe"), "universe")
        s.register(time(14, 50), lambda: order.append("close"), "close_all", critical=True)
        s.run_once(_at(15, 0))
        assert order[0] == "close"


class TestMarketHours:
    @pytest.mark.parametrize(
        ("hour", "minute", "expected"),
        [(8, 59, False), (9, 0, True), (14, 50, True), (15, 29, True), (15, 30, False)],
    )
    def test_場中の判定(self, hour: int, minute: int, expected: bool) -> None:
        assert MarketScheduler().is_market_hours(_at(hour, minute)) is expected


class TestResetDay:
    def test_やり直せる(self) -> None:
        """障害復旧で同じ日をもう一度回すとき。"""
        calls: list[str] = []
        s = MarketScheduler()
        s.register(time(7, 0), lambda: calls.append("x"), "universe")
        s.run_once(_at(8, 0))
        s.reset_day(date(2026, 6, 1))
        s.run_once(_at(8, 0))
        assert calls == ["x", "x"]
