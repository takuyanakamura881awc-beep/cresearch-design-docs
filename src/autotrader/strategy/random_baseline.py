"""ランダムエントリーのベースライン。**竹に優位があるかの検定に使う。**

【なぜ要るのか】

コストを実測に変えたところ、竹のコスト前リターンは **-0.38%**（gross PF 約0.99）
だった。ほぼゼロで、コイン投げと区別がつかない。

だが「ゼロに見える」だけでは足りない。**比較対象がないと判断できない。**
1.5×ATR で切って 2.5×ATR で利確し、180分で時間切れにして14:50に閉じる
——この手仕舞いルール自体が損益を生む。エントリーが何もしていなくても、
手仕舞いだけで数字は動く。

そこで**エントリーだけをランダムにして、他をすべて揃えたもの**と比べる。
竹の gross がランダムの分布の中に埋もれるなら、**シグナルは何もしていない。**

【なぜ TakeIntraday を継承しているのか】

`should_close` / `_roll_day` / `_can_enter` / `_stopped_out` を**継承で共有する**。
同じロジックを書き写すと、片方を直したときにもう片方が置き去りになり、
「手仕舞いは同じ」という前提が黙って崩れる。

**継承すれば、両者の違いが `generate` の1メソッドだけであることが
コードの構造として保証される。**

【比較で気をつけること】

- **1回だけ回して比べない。** 1点対1点では運と区別できない。
  20〜30シードで分布を作り、竹がどこに落ちるかを見る
- **1トレードあたりで正規化する。** 総額で比べるとトレード数の差が混ざる
- ``seed`` は必須。**再現しない検証は検証ではない**
"""

from __future__ import annotations

import logging
import random
from datetime import datetime

from autotrader.strategy.take_intraday import TakeIntraday, TakeIntradayConfig
from autotrader.types import Bar, Position, Side, Signal
from autotrader.universe.selector import compute_atr

logger = logging.getLogger(__name__)

ENTRY_REASON = "random"
"""ランダムエントリーの `Signal.reason`。シグナル別集計で竹と混ざらないようにする。"""


class RandomEntry(TakeIntraday):
    """エントリーだけをランダムにした竹。**手仕舞いと制約は竹と同一。**

    竹と揃うもの（継承）:

    - 損切り 1.5×ATR / 利確 2.5×ATR / 保有180分 / 14:50 クローズ
    - 損切り後は同日に再エントリーしない（`reenter_after_stop`）
    - 1銘柄1日1回まで（`max_entries_per_symbol_per_day`）
    - ショートにはストップを必ず載せる（安全装置 #3）

    竹と違うもの（`generate` のみ）:

    - 発火条件がない。**各バーで確率 ``entry_probability`` で入る**
    - 方向は 50/50

    監視銘柄・同時保有数・レバレッジ・売建可否はエンジン側の制約なので、
    こちらが何もしなくても同じように効く。
    """

    def __init__(
        self,
        seed: int,
        entry_probability: float,
        config: TakeIntradayConfig | None = None,
    ) -> None:
        """
        Args:
            seed: 乱数シード。**必須**（再現しない検証は検証ではない）。
            entry_probability: 1バー・1銘柄あたりのエントリー確率。
                竹の実測トレード数に合わせて呼び出し側が決める。
            config: 竹と同じパラメータ。**変えると比較が壊れる。**

        Raises:
            ValueError: 確率が 0〜1 の範囲外の場合。
        """
        super().__init__(config)
        if not 0.0 < entry_probability <= 1.0:
            raise ValueError(
                f"エントリー確率は 0 < p <= 1 である必要がある: {entry_probability}"
            )
        self._rng = random.Random(seed)
        self._entry_probability = entry_probability
        self._seed = seed

    @property
    def seed(self) -> int:
        return self._seed

    def generate(
        self,
        now: datetime,
        bars: dict[str, tuple[Bar, ...]],
        positions: tuple[Position, ...],
    ) -> tuple[Signal, ...]:
        """ランダムにエントリーする。**竹の `generate` と入れ替わるのはここだけ。**

        竹が入りうる時間帯に揃える（オープニングレンジ確定後）。
        揃えないと、ランダム側だけが 09:00〜09:30 に入れてしまい比較が歪む。
        """
        cfg = self.config
        self._roll_day(now.date())
        if now.time() < cfg.range_end:
            # 竹はオープニングレンジが確定するまで入らない
            return ()

        held = {p.symbol for p in positions}
        signals: list[Signal] = []

        # **銘柄順を固定する。** dict の順序に依存すると、
        # 同じシードでも呼び出し側の都合で結果が変わりうる
        for symbol in sorted(bars):
            if symbol in held or not self._can_enter(symbol):
                continue
            if self._rng.random() >= self._entry_probability:
                continue

            series = bars[symbol]
            atr = compute_atr(series, cfg.atr_period)
            if atr is None or atr <= 0:
                continue
            last = series[-1]
            if last.close <= 0:
                continue

            side = Side.LONG if self._rng.random() < 0.5 else Side.SHORT
            if side is Side.SHORT and not cfg.allow_short:
                continue

            # **エントリー回数を必ず数える。** これを忘れると
            # `_can_enter` の「1銘柄1日1回」が効かず、ランダム側だけが
            # 同じ銘柄に何度も入れてしまう（= 竹より有利になり比較が壊れる）。
            # 竹の `generate` も同じ位置で加算している
            self._entries[symbol] = self._entries.get(symbol, 0) + 1

            # **ストップと利確の式は竹と同一。** ここを変えると
            # 「手仕舞いを揃えた」という前提が崩れる
            direction = 1.0 if side is Side.LONG else -1.0
            signals.append(
                Signal(
                    symbol=symbol,
                    side=side,
                    strength=1.0,
                    reason=ENTRY_REASON,
                    stop_price=last.close - direction * cfg.stop_loss_atr_mult * atr,
                    take_profit_price=(
                        last.close + direction * cfg.take_profit_atr_mult * atr
                    ),
                )
            )

        return tuple(signals)


def entry_probability_for(
    target_trades: int,
    n_symbols: int,
    n_bars: int,
) -> float:
    """竹のトレード数に頻度を合わせるためのエントリー確率。

    ``p = 目標トレード数 ÷ (銘柄数 × バー数)``。

    **これは目安であって厳密には合わない。** 同時保有数の上限・
    レバレッジ・売建可否・1銘柄1日1回の制限がエントリーを抑えるため、
    実際のトレード数はこれより少なくなる。

    **だから比較は1トレードあたりで正規化する。**
    総額で比べるとトレード数の差が混ざる。

    Raises:
        ValueError: 銘柄数・バー数が正でない場合。
    """
    if n_symbols < 1 or n_bars < 1:
        raise ValueError("銘柄数とバー数は1以上である必要がある")
    if target_trades < 1:
        raise ValueError("目標トレード数は1以上である必要がある")
    return min(1.0, target_trades / (n_symbols * n_bars))
