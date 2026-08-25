"""竹 — デイトレード ロング/ショート（当日決済）。★メイン手法

docs/04-strategies.md の「竹」。

===============  ==========================================================
項目             内容
===============  ==========================================================
保有期間         数十分〜大引け前（**当日決済必須**）
信用区分         デイトレ信用（手数料0・金利0・貸株料0）
ポジション       同時3〜5銘柄、各6〜12.5万円、1日3〜5トレード
必要データ       5分足（Stage A は yfinance が58日ぶん返す）
===============  ==========================================================

【シグナルA】オープニングレンジ・ブレイクアウト（順張り）
    9:00-9:30 の高値/安値でレンジを定義し、そこを抜けたらエントリー。
    寄り付き後30分のレンジはその日の需給の綱引きの結果であり、
    それを抜けることは新しい方向へのエネルギーを示す、という仮説。

【シグナルB】VWAP乖離の平均回帰（逆張り）
    VWAP から一定以上乖離し、乖離が拡大から縮小に転じたらVWAP方向へエントリー。
    VWAP は機関投資家の執行基準になりやすく、回帰力が働く、という仮説。

【重要】シグナルAとBは方向が逆になる場面がある。
同一銘柄で両方が同時発火したら**エントリーを見送る**
（矛盾するシグナルは情報がない状態と扱う）。

【ルックアヘッドについて】

``generate`` が受け取る ``bars`` は、エンジンが ``PointInTimeView`` で
**その時点までに閉じたバーだけに絞ったもの**。この戦略は渡されたものを
そのまま使えばよく、自分で時刻を見て切る必要はない（切ると二重に切れる）。

ただし**当日始まりの判定は自分で行う**。VWAP もオープニングレンジも
「その日の」値なので、前日を混ぜると意味が変わる。
`_today` が日付でバーを絞るのはそのため。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time

from autotrader.strategy.base import Strategy
from autotrader.types import Bar, Position, Side, Signal
from autotrader.universe.selector import compute_atr

logger = logging.getLogger(__name__)

DEFAULT_RANGE_START = time(9, 0)
DEFAULT_RANGE_END = time(9, 30)
DEFAULT_BREAKOUT_BUFFER_BPS = 5.0
DEFAULT_MIN_DEVIATION_PCT = 0.015
DEFAULT_STOP_ATR_MULT = 1.5
DEFAULT_TAKE_PROFIT_ATR_MULT = 2.5
DEFAULT_ATR_PERIOD = 14
DEFAULT_MAX_HOLDING_MINUTES = 180


@dataclass(frozen=True)
class TakeIntradayConfig:
    """竹のパラメータ。config/strategies.yaml の ``take_intraday`` に対応。

    **数値はすべて暫定。** Phase 4 のウォークフォワード検証で確定する
    （in-sample で決めた成績は成績として数えない）。
    """

    range_start: time = DEFAULT_RANGE_START
    range_end: time = DEFAULT_RANGE_END
    breakout_buffer_bps: float = DEFAULT_BREAKOUT_BUFFER_BPS
    """レンジ端からこの分だけ抜けたら発火。**0にしない。**

    端に触れただけで発火させると、レンジ内のノイズを全部拾う。
    """
    min_deviation_pct: float = DEFAULT_MIN_DEVIATION_PCT
    require_deviation_shrinking: bool = True
    """乖離が拡大から縮小に転じたことを確認してからエントリーするか。

    **落ちるナイフを掴まないための条件。** 乖離が拡大し続けている最中に
    逆張りすると、トレンドの初動に正面から当たる。
    """
    allow_short: bool = True
    skip_on_conflicting_signals: bool = True
    enable_breakout: bool = True
    """シグナルA（オープニングレンジブレイク）を使うか。

    **False にすると VWAP乖離だけになる。** 実測（2026-08-24 / 39営業日）で
    ORB は538件・gross -10円/件、VWAP乖離は17件・gross +250円/件だった。
    **順張りが負けて逆張りが勝っている可能性**を分離して測るためのスイッチ。
    """
    invert_breakout: bool = False
    """シグナルA の方向を反転するか（上抜けで売り／下抜けで買い）。

    **これは仮説検定用のスイッチであって、パラメータではない。**
    ランダム検定で竹が35パーセンタイル（中央値以下）だったとき、
    負けの本体が ORB だったことから立てた「この期間この銘柄群では
    順張りが効かない」という仮説を試すためだけにある。

    **out-of-sample の確認なしに True を既定にしてはならない。**
    39営業日は in-sample で、複数の変種を試せばどれかは偶然勝つ
    （4変種なら偶然95%を超えるものが出る確率は約19%）。
    """

    stop_loss_atr_mult: float = DEFAULT_STOP_ATR_MULT
    take_profit_atr_mult: float = DEFAULT_TAKE_PROFIT_ATR_MULT
    atr_period: int = DEFAULT_ATR_PERIOD
    max_holding_minutes: int = DEFAULT_MAX_HOLDING_MINUTES
    reenter_after_stop: bool = False
    """損切りした銘柄に同じ日もう一度入るか。**既定は禁止。**

    損切りは「その日のその仮説が外れた」という証拠であり、
    新しい情報なしに同じ賭けを繰り返すのはコストを払うだけ。

    実データ（39営業日）で禁止せずに回したところ、損切り直後に
    シグナルAが再発火して入り直す往復が起き、**1日28.6トレード**まで膨らんだ
    （想定は3〜5）。損切り83件の多くがこの往復だった。
    """
    volume_confirmation_mult: float | None = None
    """出来高確認のスイッチ。設定すると、直近バーの出来高が当日の
    それまでの平均バー出来高のこの倍数以上でなければシグナルB
    （VWAP乖離）を発火しない。

    **仮説検定用のフィルタ。** 乖離が大きくても薄商いなら、本当に
    売買を伴った動きか疑わしくノイズの可能性が高い。出来高を伴う
    乖離ほど平均回帰の力が働きやすい、という仮説を試す
    （`docs/00-overview.md` 意思決定ログ51）。

    **既定は None（無効）。** 既存変種の結果を変えない。
    """

    max_entries_per_symbol_per_day: int = 1
    """1銘柄あたり1日の最大エントリー回数。

    利確で手仕舞った場合は仮説が機能したので再発火を認めるが、
    **無制限にはしない。** 回転が増えるほど往復コストが積み上がる。

    **暫定値。** Phase 4 のウォークフォワードで
    「再エントリーを許す/許さない」の成績を比較して確定する
    （プレミアム枠・寄り前気配と同じ検証構造）。
    """

    def __post_init__(self) -> None:
        if self.range_start >= self.range_end:
            raise ValueError("range_start < range_end である必要がある")
        if self.breakout_buffer_bps <= 0:
            raise ValueError(
                "breakout_buffer_bps を0以下にしない。"
                "レンジ端に触れただけで発火するとノイズを全部拾う"
            )
        if self.min_deviation_pct <= 0:
            raise ValueError("min_deviation_pct は正の値")
        if self.volume_confirmation_mult is not None and self.volume_confirmation_mult <= 0:
            raise ValueError("volume_confirmation_mult は正の値")
        if self.stop_loss_atr_mult <= 0 or self.take_profit_atr_mult <= 0:
            raise ValueError("ATR 倍率は正の値")
        if self.take_profit_atr_mult <= self.stop_loss_atr_mult:
            raise ValueError(
                "利確倍率 > 損切り倍率 である必要がある。"
                "逆だと勝率が高くても期待値が負になる"
            )
        if self.max_entries_per_symbol_per_day < 1:
            raise ValueError("max_entries_per_symbol_per_day は1以上")


@dataclass(frozen=True)
class OpeningRange:
    """その日のオープニングレンジ。"""

    high: float
    low: float
    complete: bool
    """``range_end`` を過ぎて確定したか。**未確定のレンジで発火させない。**"""


def _today(bars: tuple[Bar, ...], day: date) -> tuple[Bar, ...]:
    """当日ぶんのバーだけを取り出す。

    VWAP もオープニングレンジも「その日の」値なので、前日を混ぜてはならない。
    """
    return tuple(b for b in bars if b.timestamp.date() == day)


def opening_range(
    bars: tuple[Bar, ...], now: datetime, config: TakeIntradayConfig | None = None
) -> OpeningRange | None:
    """当日のオープニングレンジ。

    Args:
        bars: **確定済みのバーのみ**（エンジンが絞ったもの）。

    Returns:
        レンジ。当日のバーが1本もなければ ``None``。
        ``complete`` が False の間はまだ形成中で、発火に使ってはならない。
    """
    cfg = config or TakeIntradayConfig()
    window = [
        b
        for b in _today(bars, now.date())
        if cfg.range_start <= b.timestamp.time() < cfg.range_end
    ]
    if not window:
        return None
    return OpeningRange(
        high=max(b.high for b in window),
        low=min(b.low for b in window),
        complete=now.time() >= cfg.range_end,
    )


def vwap(bars: tuple[Bar, ...], day: date) -> float | None:
    """当日の VWAP（出来高加重平均価格）。

    典型価格 (高値+安値+終値)/3 を出来高で加重する。

    **当日始まりでリセットする。** 前日を引きずると、寄り付き直後の
    VWAP が前日終値付近に張り付き、乖離が常に大きく出る。

    Returns:
        VWAP。当日のバーがない、または出来高がゼロなら ``None``。
    """
    today = _today(bars, day)
    if not today:
        return None
    total_volume = sum(b.volume for b in today)
    if total_volume <= 0:
        return None
    weighted = sum((b.high + b.low + b.close) / 3 * b.volume for b in today)
    return weighted / total_volume


class TakeIntraday(Strategy):
    """竹 — デイトレード ロング/ショート。

    **シグナルを返すだけで発注はしない。** サイジングとリスクチェックを
    経て初めて注文になる（`engine/backtest.py` / `engine/live.py`）。
    """

    def __init__(self, config: TakeIntradayConfig | None = None) -> None:
        self.config = config or TakeIntradayConfig()
        # **その日の履歴。日付が変わったら捨てる。**
        # 前日の損切りを引きずると、翌日の正当なシグナルまで殺してしまう。
        self._day: date | None = None
        self._entries: dict[str, int] = {}
        self._stopped_out: set[str] = set()

    def _roll_day(self, day: date) -> None:
        if self._day != day:
            self._day = day
            self._entries = {}
            self._stopped_out = set()

    def _can_enter(self, symbol: str) -> bool:
        cfg = self.config
        if symbol in self._stopped_out and not cfg.reenter_after_stop:
            return False
        return self._entries.get(symbol, 0) < cfg.max_entries_per_symbol_per_day

    # ------------------------------------------------------------------
    # エントリー
    # ------------------------------------------------------------------

    def generate(
        self,
        now: datetime,
        bars: dict[str, tuple[Bar, ...]],
        positions: tuple[Position, ...],
    ) -> tuple[Signal, ...]:
        self._roll_day(now.date())
        held = {p.symbol for p in positions}
        signals: list[Signal] = []

        for symbol, series in bars.items():
            if symbol in held or not self._can_enter(symbol):
                continue
            signal = self._for_symbol(symbol, series, now)
            if signal is not None:
                signals.append(signal)
                # **シグナルを出した時点で数える。**
                # 約定したかどうかは戦略からは見えない（枠やレバレッジで
                # 見送られることがある）。約定を待つと、見送られた銘柄に
                # 毎バー出し続けることになる。
                self._entries[symbol] = self._entries.get(symbol, 0) + 1
        return tuple(signals)

    def _for_symbol(
        self, symbol: str, series: tuple[Bar, ...], now: datetime
    ) -> Signal | None:
        """1銘柄ぶんのシグナル。両方発火したら ``None``（見送り）。"""
        cfg = self.config
        atr = compute_atr(series, cfg.atr_period)
        if atr is None or atr <= 0:
            return None
        today = _today(series, now.date())
        if not today:
            return None
        last = today[-1]
        if last.close <= 0:
            return None

        breakout = self._breakout(series, now, last) if cfg.enable_breakout else None
        reversion = self._reversion(series, now, last)

        conflicting = (
            breakout is not None and reversion is not None and breakout != reversion
        )
        if conflicting and cfg.skip_on_conflicting_signals:
            # **方向が逆。矛盾するシグナルは情報がない状態と扱う。**
            logger.debug("%s: AとBが逆方向に発火したため見送り", symbol)
            return None

        if breakout is not None and reversion is not None:
            # 同方向に両方発火。順張り側を採る（ブレイクの方が価格に近い）
            side, reason = breakout, "orb+vwap"
        elif breakout is not None:
            side, reason = breakout, "orb"
        elif reversion is not None:
            side, reason = reversion, "vwap_reversion"
        else:
            return None

        if side is Side.SHORT and not cfg.allow_short:
            return None

        # **ストップは必ず載せる。** ショートは特に必須（安全装置 #3）で、
        # 載せないと ReplayBroker が発注を拒否する。
        direction = 1.0 if side is Side.LONG else -1.0
        return Signal(
            symbol=symbol,
            side=side,
            strength=1.0,
            reason=reason,
            stop_price=last.close - direction * cfg.stop_loss_atr_mult * atr,
            take_profit_price=last.close + direction * cfg.take_profit_atr_mult * atr,
        )

    def _breakout(
        self, series: tuple[Bar, ...], now: datetime, last: Bar
    ) -> Side | None:
        """シグナルA。**レンジが確定するまで発火しない。**"""
        cfg = self.config
        rng = opening_range(series, now, cfg)
        if rng is None or not rng.complete:
            return None
        buffer = cfg.breakout_buffer_bps / 10_000.0
        if last.close > rng.high * (1 + buffer):
            side = Side.LONG
        elif last.close < rng.low * (1 - buffer):
            side = Side.SHORT
        else:
            return None
        if cfg.invert_breakout:
            # **発火条件は変えず、向きだけを反転する。**
            # 条件も変えると「順張りが悪いのか条件が悪いのか」が分離できない
            return Side.SHORT if side is Side.LONG else Side.LONG
        return side

    def _reversion(
        self, series: tuple[Bar, ...], now: datetime, last: Bar
    ) -> Side | None:
        """シグナルB。乖離が**縮小に転じてから**入る。"""
        cfg = self.config
        today = _today(series, now.date())
        current = vwap(series, now.date())
        if current is None or current <= 0 or len(today) < 2:
            return None

        deviation = (last.close - current) / current
        if abs(deviation) < cfg.min_deviation_pct:
            return None

        if cfg.require_deviation_shrinking:
            # 直前のバー時点の VWAP と比較する。**当日始まりからの累積**なので、
            # 前のバーまでで打ち切った VWAP を作り直して比べる
            previous = today[:-1]
            prev_vwap = vwap(previous, now.date())
            if prev_vwap is None or prev_vwap <= 0:
                return None
            prev_deviation = (previous[-1].close - prev_vwap) / prev_vwap
            if abs(deviation) >= abs(prev_deviation):
                # まだ拡大中。落ちるナイフを掴まない
                return None

        if cfg.volume_confirmation_mult is not None:
            previous = today[:-1]
            if not previous:
                return None
            avg_volume = sum(b.volume for b in previous) / len(previous)
            if avg_volume <= 0 or last.volume < avg_volume * cfg.volume_confirmation_mult:
                # 薄商いの乖離はノイズの可能性が高い。出来高で確認できない
                return None

        return Side.SHORT if deviation > 0 else Side.LONG

    # ------------------------------------------------------------------
    # 手仕舞い
    # ------------------------------------------------------------------

    def should_close(
        self, now: datetime, position: Position, bars: tuple[Bar, ...]
    ) -> tuple[bool, str]:
        """建玉を手仕舞うべきか。

        大引けの全建玉クローズ（14:50）と日次ブレーカーは、
        この判断とは独立にエンジンが無条件で実行する。
        ここで False を返しても関係なくクローズされる。
        """
        cfg = self.config
        self._roll_day(now.date())
        if not bars:
            return False, "hold"
        last = bars[-1]
        atr = compute_atr(bars, cfg.atr_period)
        if atr is None or atr <= 0:
            return False, "hold"

        direction = 1.0 if position.side is Side.LONG else -1.0
        move = (last.close - position.entry_price) * direction

        if move <= -cfg.stop_loss_atr_mult * atr:
            # **損切りした銘柄を記録する。** その日もう入らないため
            self._stopped_out.add(position.symbol)
            return True, "stop"
        if move >= cfg.take_profit_atr_mult * atr:
            return True, "take_profit"

        held_minutes = (now - position.opened_at).total_seconds() / 60
        if held_minutes >= cfg.max_holding_minutes:
            return True, "time_exit"
        return False, "hold"
