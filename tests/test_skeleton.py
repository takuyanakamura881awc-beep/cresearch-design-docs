"""スケルトンの整合性テスト。

Phase 0 の完了条件（docs/00-overview.md）:
全モジュールが import でき、抽象インターフェースが正しく定義されていること。
"""

from __future__ import annotations

import inspect

from autotrader.broker.base import Broker
from autotrader.strategy.base import Strategy


def test_パッケージがimportできる() -> None:
    import autotrader

    assert autotrader.__version__ == "0.1.0"


def test_全モジュールがimportできる() -> None:
    """import 時に副作用やエラーが起きないこと。"""
    modules = [
        "autotrader.config",
        "autotrader.types",
        "autotrader.broker.base",
        "autotrader.broker.kabus",
        "autotrader.broker.paper",
        "autotrader.data.jquants",
        "autotrader.data.recorder",
        "autotrader.data.shortable",
        "autotrader.data.calendar",
        "autotrader.data.store",
        "autotrader.universe.builder",
        "autotrader.universe.filters",
        "autotrader.universe.selector",
        "autotrader.strategy.base",
        "autotrader.strategy.take_intraday",
        "autotrader.risk.leverage",
        "autotrader.risk.limits",
        "autotrader.risk.sizing",
        "autotrader.risk.killswitch",
        "autotrader.engine.backtest",
        "autotrader.engine.live",
        "autotrader.engine.scheduler",
        "autotrader.execution.order",
        "autotrader.execution.close_all",
        "autotrader.execution.reconcile",
        "autotrader.report.metrics",
        "autotrader.report.daily",
    ]
    for name in modules:
        __import__(name)


def test_Brokerは抽象クラス() -> None:
    """Broker を直接インスタンス化できないこと。"""
    assert inspect.isabstract(Broker)


def test_Brokerが必要なメソッドを定義している() -> None:
    """接続先を差し替えるために必要な操作が揃っていること。"""
    required = {
        "get_account",
        "get_positions",
        "get_quote",
        "send_order",
        "cancel_order",
        "get_orders",
        "is_shortable",
    }
    assert required <= Broker.__abstractmethods__


def test_Strategyは抽象クラス() -> None:
    assert inspect.isabstract(Strategy)


def test_Strategyが必要なメソッドを定義している() -> None:
    assert {"generate", "should_close"} <= Strategy.__abstractmethods__
