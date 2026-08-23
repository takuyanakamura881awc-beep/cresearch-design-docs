"""バックテストエンジン。

【最重要の設計要求】ルックアヘッドバイアスを**構造的に**防ぐ。

「注意して書く」では不十分。``PointInTimeView`` が各時点で参照可能な
データだけを露出させ、**未来のデータには物理的にアクセスできない**ようにする。

backtest-validator（.claude/agents/）はこの点を検証し、
「注意して書いている」だけなら**不合格**とする。

【約定コストのモデル】
デイトレ信用は手数料0・金利0・貸株料0だが、コストは0ではない。
スリッページとスプレッドを必ず引く。ここを甘くすると
バックテストとペーパーが乖離する（docs/07-go-live-criteria.md 基準#4）。
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from autotrader.broker.base import OrderRejectedError
from autotrader.broker.replay import ReplayBroker
from autotrader.data.calendar import TradingCalendar
from autotrader.report.metrics import (
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    to_returns,
    win_rate,
)
from autotrader.risk.limits import (
    check_consecutive_loss,
    check_daily_loss,
    check_max_drawdown,
)
from autotrader.risk.sizing import calc_quantity, target_notional
from autotrader.strategy.base import Strategy
from autotrader.types import Bar, Side, Trade

logger = logging.getLogger(__name__)

STAGE_A_SLIPPAGE_BPS = 20.0
"""Stage A の片道スリッページ（bps）。

**Stage B（10bps）より厚い。** 板情報がないぶん約定価格を推定に頼るため、
検証できないものは保守的な側に倒す（CLAUDE.md 規約5）。
"""


class PointInTimeView:
    """指定時刻の時点で参照してよいデータだけを露出させるビュー。

    バックテストの戦略には必ずこれを経由してデータを渡す。
    生の辞書を渡すと、戦略が未来を覗ける経路ができてしまう。

    【``<= now`` ではなく ``< now`` で切る理由】

    バーのタイムスタンプは**期間の開始時刻**（yfinance も J-Quants もこの規約）。
    09:05 のタイムスタンプを持つ5分足は 09:05〜09:10 の値動きを表すので、
    **09:05 時点ではまだ確定していない**。``<= now`` で切ると、
    これから起きる5分間の高値・安値・終値が見えてしまう。

    見えても「使わないように書く」のでは、指標の計算を1行足した誰かが
    静かに未来を参照する。**そもそも渡さない。**

    したがって ``now`` に立っている観測者に見えるのは、
    ``now`` より前に開始した = すでに閉じたバーだけになる。

    【元データを保持し、都度スライスしない理由】

    銘柄ごとに一度だけ二分探索して境界を出す。全期間ぶんをコピーすると
    銘柄数×時刻数のコピーが走り、5分足×287銘柄では現実的な速度にならない。
    """

    __slots__ = ("_bars", "_now", "_cutoff")

    def __init__(self, bars: Mapping[str, tuple[Bar, ...]], now: datetime) -> None:
        self._bars = bars
        self._now = now
        self._cutoff: dict[str, int] = {}

    @property
    def now(self) -> datetime:
        return self._now

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self._bars)

    def get(self, symbol: str) -> tuple[Bar, ...]:
        """``now`` より前に閉じたバーだけを返す。

        知らない銘柄には空を返す。**例外にしない**のは、
        ユニバースに入ったばかりでまだ履歴がない銘柄と扱いを揃えるため
        （戦略側は「データが足りなければ何もしない」だけでよい）。
        """
        series = self._bars.get(symbol)
        if not series:
            return ()
        index = self._cutoff.get(symbol)
        if index is None:
            # Bar は比較不能なのでキー列を作って二分探索する
            timestamps = [b.timestamp for b in series]
            index = bisect_left(timestamps, self._now)
            self._cutoff[symbol] = index
        return series[:index]

    def latest(self, symbol: str) -> Bar | None:
        """直近の確定バー。無ければ ``None``。"""
        series = self.get(symbol)
        return series[-1] if series else None

    def as_dict(self) -> dict[str, tuple[Bar, ...]]:
        """``Strategy.generate`` に渡す形。全銘柄ぶんを確定バーだけで作る。"""
        return {symbol: self.get(symbol) for symbol in self._bars}


@dataclass(frozen=True)
class CostModel:
    """約定コスト。**ゼロにしてはならない**（CLAUDE.md 規約5）。

    デイトレ信用は手数料0・金利0・貸株料0だが、
    スリッページとスプレッドは必ず発生する。
    コストゼロのシミュレーションは本番で必ず乖離する。
    """

    slippage_bps: float = STAGE_A_SLIPPAGE_BPS
    """片道スリッページ（bps）。往復では2倍かかる。"""

    def __post_init__(self) -> None:
        if self.slippage_bps <= 0:
            raise ValueError(
                "スリッページを0以下にしてはならない。"
                "手数料が0でもコストは0ではない（CLAUDE.md 規約5）"
            )

    @property
    def rate(self) -> float:
        return self.slippage_bps / 10_000.0

    def fill_price(self, side: Side, reference: float, *, opening: bool) -> float:
        """約定価格。**必ず不利な側にずらす。**

        買い建て・売り決済の別ではなく「その取引で自分がどちら側か」で決まる。
        新規買い/返済買いは高く、新規売り/返済売りは安く約定する。
        """
        buying = (side is Side.LONG) == opening
        return reference * (1 + self.rate) if buying else reference * (1 - self.rate)


@dataclass(frozen=True)
class BacktestResult:
    """バックテストの結果。

    ``sharpe`` が最重要指標。リターンだけを見ると、たまたま勝った
    高リスク手法を選んでしまう（docs/07-go-live-criteria.md §1）。
    """

    total_return: float
    sharpe: float
    max_drawdown: float
    n_trades: int
    win_rate: float
    profit_factor: float
    equity_curve: tuple[float, ...]
    trades: tuple[Trade, ...] = ()
    initial_cash: float = 0.0
    breaker_days: int = 0
    """日次損失ブレーカー（#4）が発動した日数。

    **成績の解釈に要る。** 発動日が多いなら、1トレードあたりのリスクが
    資金量に対して大きすぎる（docs/04 のトレード頻度想定を参照）。
    """
    halted_early: bool = False
    """連続損失（#5）か累積DD（#6）で期間の途中から停止したか。

    True の場合、**残りの期間は取引していない**。
    総リターンをそのまま年率換算してはならない。
    """
    rejected_by_leverage: int = 0
    """レバレッジ1倍の上限で発注を見送った回数。

    **多いことを異常として扱わない。** これは安全装置が働いた記録であって
    バグではない。ただし極端に多い場合はサイジングが資金量に合っていない
    （1銘柄あたりの目標額が大きすぎる）ことを示す。
    """

    @classmethod
    def from_equity(
        cls,
        equity_curve: list[float],
        trades: list[Trade],
        initial_cash: float,
        periods_per_year: int = 252,
        rejected_by_leverage: int = 0,
        breaker_days: int = 0,
        halted_early: bool = False,
    ) -> BacktestResult:
        """エクイティカーブとトレード列から指標をまとめて算出する。

        **集計をここ1箇所に閉じる。** シャープの定義が呼び出し側ごとに
        ずれると、期間をまたいだ比較ができなくなる。
        """
        pnls = [t.pnl for t in trades]
        last = equity_curve[-1] if equity_curve else initial_cash
        return cls(
            total_return=(last / initial_cash - 1.0) if initial_cash > 0 else 0.0,
            sharpe=sharpe_ratio(to_returns(equity_curve), periods_per_year),
            max_drawdown=max_drawdown(equity_curve),
            n_trades=len(trades),
            win_rate=win_rate(pnls),
            profit_factor=profit_factor(pnls),
            equity_curve=tuple(equity_curve),
            trades=tuple(trades),
            initial_cash=initial_cash,
            rejected_by_leverage=rejected_by_leverage,
            breaker_days=breaker_days,
            halted_early=halted_early,
        )


@dataclass(frozen=True)
class BacktestConfig:
    """バックテストの実行設定。"""

    initial_cash: Decimal = Decimal(500_000)
    slippage_bps: float = STAGE_A_SLIPPAGE_BPS
    close_time: time = time(14, 50)
    """当日クローズの時刻（安全装置 #2 と同じ 14:50）。

    **この時刻以降の最初のバーで全建玉を閉じる。** デイトレ信用は当日中に
    返済しないと翌営業日に強制決済され1注文2,200円（CLAUDE.md 規約2）。
    """
    max_concurrent: int = 5
    """同時保有の上限（安全装置 #7）。"""
    max_weight_per_symbol: float = 0.25
    """1銘柄あたり総資産の上限（安全装置 #7）。"""
    daily_loss_pct: float = -0.02
    """日次損失上限（安全装置 #4）。当日全停止 + 全クローズ。"""
    consecutive_loss_days: int = 3
    """連続損失で停止する営業日数（安全装置 #5）。以降は再開しない。"""
    max_drawdown_pct: float = -0.15
    """累積ドローダウンの上限（安全装置 #6）。以降は再開しない。"""
    enforce_breakers: bool = True
    """ブレーカーを再現するか。

    **False にするのは「ブレーカーの寄与を測る」ときだけ。**
    切って出した成績を採用してはならない。実運用では止まっていた日の
    取引を含んだ、実現不可能な成績になる。
    """

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash は正の値")
        if self.max_concurrent < 1:
            raise ValueError("max_concurrent は1以上")


def run(
    strategy: Strategy,
    bars: Mapping[str, tuple[Bar, ...]],
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """バックテストを実行する。

    【時間の進み方 — ここがルックアヘッド防止の核心】

    時刻 t（バー i の開始）において::

        戦略が見るもの : PointInTimeView(bars, t) = t より前に閉じたバー
        約定する価格   : バー i の始値 + スリッページ

    つまり**判断に使った最後のバーは、必ず約定より前に閉じている**。
    同じバーの終値で判断して同じバーの始値で約定する、という
    バックテスト特有の反則が構造的に起こらない。

    【当日クローズ】

    ``config.close_time`` 以降の最初のバーで全建玉を成行返済する。
    戦略の判断とは独立に無条件で実行する（``execution/close_all`` と同じ規律）。
    デイトレ信用の建玉を持ち越すと1注文2,200円が確定する。

    Args:
        strategy: 検証する戦略。
        bars: 銘柄コード → バー列（時刻の昇順）。分足を想定する。
        config: 実行設定。省略時は Stage A の既定（20bps・14:50クローズ）。

    Returns:
        結果。**採用の判断前に backtest-validator の検証を通すこと。**

    Note:
        ``equity_curve`` は**日次**（各営業日の最終バー時点）。
        シャープの年率換算（252営業日）と単位を揃えるため。

        したがって**日中のドローダウンは捉えていない。**
        日次損失ブレーカー（-2%、安全装置 #4）は日中に発動するので、
        その再現は Phase 3 で ``risk/limits.py`` を組み込んでから行う。
    """
    cfg = config or BacktestConfig()
    broker = ReplayBroker(cfg.initial_cash, bars, cfg.slippage_bps)
    if not broker.timeline:
        return BacktestResult.from_equity([], [], float(cfg.initial_cash))

    # **日次で記録する。** バーごとに記録すると、シャープの年率換算
    # （252営業日）と単位が食い違い、5分足なら約54倍に膨らんだ値になる。
    # 建玉は当日中に必ず閉じるので、日次のエクイティは実現済みの状態を表す。
    daily_equity: dict[date, float] = {}
    rejected = 0
    order_seq = 0
    current_day: date | None = None
    closed_today = False
    halted_for_good = False
    breaker_days: list[date] = []
    day_open_equity = float(cfg.initial_cash)

    while not broker.exhausted:
        now = broker.now
        if now.date() != current_day:
            # 日をまたぐ。日次ブレーカーは翌営業日に自動復帰する（#4）
            current_day = now.date()
            closed_today = False
            day_open_equity = broker.equity()

        view = PointInTimeView(bars, now)
        positions = broker.get_positions()

        # 1. 手仕舞い。当日クローズとブレーカーは戦略の判断より優先する
        #
        # **日中の損益で判定する。** 日次終値だけで見ると、日中に -2% を
        # 割ってから戻した日を見逃し、実運用では止まっていた取引を成績に含める。
        tripped = _daily_breaker_tripped(broker, cfg, day_open_equity, closed_today)
        if tripped and current_day not in breaker_days:
            breaker_days.append(current_day)
        force_close = tripped or (not closed_today and now.time() >= cfg.close_time)
        for position in positions:
            if force_close:
                should, reason = True, "daily_breaker" if tripped else "close_all"
            else:
                should, reason = strategy.should_close(
                    now, position, view.get(position.symbol)
                )
            if not should:
                continue
            order_seq += 1
            try:
                broker.market_order(
                    f"{reason}-{order_seq}",
                    position.symbol,
                    position.side,
                    position.quantity,
                    opening=False,
                    reason=reason,
                )
            except OrderRejectedError as exc:
                # **黙って続けない。** 返済できない建玉は翌日の強制決済に
                # つながるので、バックテストでも失敗として見える形で残す。
                logger.warning("%s の返済が拒否された: %s", position.symbol, exc)
        if force_close:
            closed_today = True

        # 2. 新規建て。当日クローズ後・ブレーカー発動後は建てない
        if not closed_today and not halted_for_good:
            rejected += _open_signals(strategy, broker, view, cfg, now)

        daily_equity[current_day] = broker.equity()

        # 3. 連続損失（#5）と累積DD（#6）。**発動したら以降は再開しない**
        if cfg.enforce_breakers and not halted_for_good:
            curve = [daily_equity[d] for d in sorted(daily_equity)]
            halted_for_good = _permanent_breaker(curve, cfg)

        broker.advance()

    residual = broker.get_positions()
    if residual:
        # 最終バーで閉じ切れなかった建玉。成績の解釈に影響するので必ず出す
        logger.warning(
            "再生終了時に建玉が %d 件残った: %s",
            len(residual),
            ", ".join(p.symbol for p in residual),
        )

    return BacktestResult.from_equity(
        [daily_equity[d] for d in sorted(daily_equity)],
        list(broker.trades),
        float(cfg.initial_cash),
        rejected_by_leverage=rejected,
        breaker_days=len(breaker_days),
        halted_early=halted_for_good,
    )


def _daily_breaker_tripped(
    broker: ReplayBroker,
    cfg: BacktestConfig,
    day_open_equity: float,
    closed_today: bool,
) -> bool:
    """日次損失ブレーカー（#4）が発動したか。

    **すでにクローズ済みの日は再判定しない。** クローズ後は建玉がないので
    損益は動かないが、毎バー「発動中」と数えると発動日数が水増しされる。
    """
    if not cfg.enforce_breakers or closed_today or day_open_equity <= 0:
        return False
    pnl_pct = broker.equity() / day_open_equity - 1.0
    state = check_daily_loss(pnl_pct, cfg.daily_loss_pct)
    if state.tripped:
        logger.info("日次ブレーカー発動: %s", state.reason)
    return state.tripped


def _permanent_breaker(equity_curve: list[float], cfg: BacktestConfig) -> bool:
    """連続損失（#5）と累積DD（#6）を判定する。

    **どちらも人の明示承認がないと再開しない**ので、バックテストでは
    以降の全期間を停止として扱う。ここで自動再開させると、
    実運用では止まっていた期間の成績を含めてしまう。
    """
    daily_returns = to_returns(equity_curve)
    for state in (
        check_consecutive_loss(daily_returns, cfg.consecutive_loss_days),
        check_max_drawdown(equity_curve, cfg.max_drawdown_pct),
    ):
        if state.tripped:
            logger.warning("再開に人の承認が要るブレーカーが発動: %s", state.reason)
            return True
    return False


def _open_signals(
    strategy: Strategy,
    broker: ReplayBroker,
    view: PointInTimeView,
    cfg: BacktestConfig,
    now: datetime,
) -> int:
    """シグナルから新規建てを行う。

    Returns:
        レバレッジ上限で見送った件数。
    """
    positions = broker.get_positions()
    held = {p.symbol for p in positions}
    signals = strategy.generate(now, view.as_dict(), positions)
    if not signals:
        return 0

    rejected = 0
    # 強いシグナルから順に建てる。枠が足りないとき何が優先されるかを決めておく
    for signal in sorted(signals, key=lambda s: (-s.strength, s.symbol)):
        if len(held) >= cfg.max_concurrent:
            break
        if signal.symbol in held:
            continue
        bar = broker.current_bar(signal.symbol)
        if bar is None:
            continue

        if signal.side is Side.SHORT:
            if signal.stop_price is None:
                # #3 ショートはストップなしで建てない。理論上の損失が無限大
                logger.warning("%s: ストップのないショートを無視した", signal.symbol)
                continue
            if not broker.is_shortable(signal.symbol):
                continue

        target = target_notional(
            broker.get_account().cash, max_weight_per_symbol=cfg.max_weight_per_symbol
        )
        quantity = calc_quantity(target, bar.open)
        if quantity <= 0:
            # 1単元も買えない。株価上限とサイジングが食い違っている兆候
            logger.debug("%s: 1単元も建てられない（始値 %.1f）", signal.symbol, bar.open)
            continue

        try:
            broker.market_order(
                f"{signal.reason}-{now:%Y%m%d%H%M}-{signal.symbol}",
                signal.symbol,
                signal.side,
                quantity,
                opening=True,
                stop_price=signal.stop_price,
            )
        except OrderRejectedError as exc:
            rejected += 1
            logger.debug("%s の新規建てが拒否された: %s", signal.symbol, exc)
            continue
        held.add(signal.symbol)

    return rejected


def walk_forward(
    strategy: Strategy,
    bars: Mapping[str, tuple[Bar, ...]],
    calendar: TradingCalendar,
    start: date,
    end: date,
    train_days: int = 60,
    test_days: int = 20,
    config: BacktestConfig | None = None,
) -> tuple[BacktestResult, ...]:
    """ウォークフォワード検証を実行する。

    in-sample でパラメータを決め、out-of-sample で検証する。
    **in-sample の成績は成績として数えない。** ここが返すのは
    out-of-sample の結果だけで、学習期間の結果を取り出す口を用意していない
    （用意すると、良く見える方を報告してしまう）。

    Args:
        calendar: 営業日カレンダー。**暦日ではなく営業日で区切る。**
            暦日で切ると、連休の入り方で学習期間の実データ量が変わる。
        train_days: 学習期間の営業日数。**この期間の結果は返さない。**
        test_days: 検証期間の営業日数。

    Returns:
        各 out-of-sample 期間の結果。窓が1つも取れなければ空。
    """
    if train_days < 1 or test_days < 1:
        raise ValueError("train_days と test_days は1以上")

    sessions = calendar.sessions(start, end)
    results: list[BacktestResult] = []
    cursor = train_days
    while cursor + test_days <= len(sessions):
        window = set(sessions[cursor : cursor + test_days])
        sliced = {
            code: tuple(b for b in series if b.timestamp.date() in window)
            for code, series in bars.items()
        }
        sliced = {code: series for code, series in sliced.items() if series}
        if sliced:
            results.append(run(strategy, sliced, config))
        cursor += test_days

    if not results:
        logger.warning(
            "ウォークフォワードの窓が1つも取れなかった（営業日 %d / 学習 %d + 検証 %d）",
            len(sessions),
            train_days,
            test_days,
        )
    return tuple(results)
