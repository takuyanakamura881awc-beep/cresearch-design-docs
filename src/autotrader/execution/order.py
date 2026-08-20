"""発注（安全装置 #3/#9/#13/#15）。

**全発注はこのモジュールを通る。** ここで以下を必ず実施する:

- レバレッジ1倍のチェック（``risk.leverage.enforce``）
- ショート建玉へのストップ注文の付与（#3）
- 冪等性の担保（#9）
- 価格サニティチェック（#13）
- 監査ログの記録（#15）

バイパス経路を作ってはならない。
"""

from __future__ import annotations

from autotrader.broker.base import Broker
from autotrader.types import AccountState, Order, Signal


class StopOrderRequiredError(Exception):
    """ショート建玉をストップ注文なしで作ろうとした（#3）。

    空売りは理論上損失無限大。レバレッジ1倍でもこの性質は変わらない。
    """


class PriceSanityError(Exception):
    """価格が前日終値から異常に乖離している（#13）。

    誤データまたは誤発注の可能性。発注を拒否する。
    """


def submit(
    broker: Broker,
    account: AccountState,
    signal: Signal,
    quantity: int,
) -> Order:
    """シグナルを注文に変換して発注する。

    この関数が全発注の唯一の入口。以下を順に実施する:

    1. 価格サニティチェック（前日終値からの乖離）
    2. 売建可否チェック（ショートの場合）
    3. **レバレッジ1倍のチェック**（``risk.leverage.enforce``）
    4. 注文IDを採番して**永続化**（冪等性のため発注前に行う）
    5. 発注
    6. **ショートならストップ注文をセットで発注**
    7. 監査ログに記録

    Raises:
        LeverageViolationError: レバレッジ上限を超える場合。
        StopOrderRequiredError: ショートでストップ注文を付与できない場合。
        PriceSanityError: 価格が異常な場合。
    """
    raise NotImplementedError("Phase 3 で実装する")


def submit_with_retry(
    broker: Broker,
    order: Order,
    max_retries: int = 3,
) -> Order:
    """冪等性を担保しつつリトライする（#9）。

    ネットワーク断は必ず起きる。リトライ時の二重発注を**構造的に**防ぐ:

    1. クライアント側で採番した注文IDを、発注前に永続化する
    2. リトライ時はその注文IDで ``broker.get_orders()`` を照会し、
       既に通っていないか確認する
    3. **確認できるまで再発注しない**
    """
    raise NotImplementedError("Phase 3 で実装する")
