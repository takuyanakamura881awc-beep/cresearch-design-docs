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


def test_StageAのモジュールがimportできる() -> None:
    """口座なしで動かす経路のモジュールが揃っていること。"""
    for name in ["autotrader.data.yahoo", "autotrader.broker.replay"]:
        __import__(name)


def test_yfinanceの期間制限が定義されている() -> None:
    """取得可能期間を超える指定を黙って切り詰めると、欠損に気づけない。

    制限値を定数として持ち、超過をエラーにできるようにしておく。
    """
    from autotrader.data.yahoo import MAX_LOOKBACK_DAYS

    assert MAX_LOOKBACK_DAYS["1m"] == 7
    assert MAX_LOOKBACK_DAYS["5m"] == 60
