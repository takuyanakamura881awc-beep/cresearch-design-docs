"""日次レポートの生成（docs/06-operations.md §2）。

人が毎日確認する唯一の成果物。**所要5分で異常に気づける内容**にする。

【並び順の原則】

**危険な順に上から並べる。** 建玉が残っていることに気づくのが翌朝では遅い
（その時点で強制決済が確定している）。読み飛ばされる前提で、
最初の数行に最も高くつく異常を置く。

【必ず含める項目】
- **建玉残存数**（0 でなければ即対応。翌日1注文2,200円）
- **送信したか不明な注文**（起動時に照会が要る）
- 損益と日次損失上限（-2%）への距離
- ブレーカーの発動状況
- トレード数
- reconcile 結果
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from autotrader.execution.journal import JournalEntry, OrderState
from autotrader.execution.reconcile import ReconcileResult
from autotrader.types import Position, Trade

logger = logging.getLogger(__name__)

DAILY_LOSS_LIMIT_PCT = -0.02
"""日次損失上限（安全装置 #4）。ここへの距離をレポートに出す。"""


@dataclass(frozen=True)
class DailySummary:
    """1営業日の集計。レポートの元データ。"""

    trade_date: date
    starting_equity: Decimal
    ending_equity: Decimal
    trades: tuple[Trade, ...] = ()
    residual_positions: tuple[Position, ...] = ()
    """**大引け後に残った建玉。0 でなければ即対応。**"""
    journal_entries: tuple[JournalEntry, ...] = ()
    reconcile: ReconcileResult | None = None
    breakers_tripped: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def pnl(self) -> Decimal:
        return self.ending_equity - self.starting_equity

    @property
    def pnl_pct(self) -> float:
        if self.starting_equity <= 0:
            return 0.0
        return float(self.pnl / self.starting_equity)

    @property
    def headroom_pct(self) -> float:
        """日次損失上限までの余裕。負なら既に超えている。"""
        return self.pnl_pct - DAILY_LOSS_LIMIT_PCT

    @property
    def unresolved_orders(self) -> tuple[JournalEntry, ...]:
        """**送信したか不明な注文。** 翌朝の起動時に照会が要る。"""
        return tuple(
            e for e in self.journal_entries if e.state is OrderState.RESERVED
        )

    @property
    def rejected_orders(self) -> tuple[JournalEntry, ...]:
        return tuple(
            e for e in self.journal_entries if e.state is OrderState.REJECTED
        )

    @property
    def has_alert(self) -> bool:
        """人が**その日のうちに**対応すべき事象があるか。"""
        return bool(
            self.residual_positions
            or self.unresolved_orders
            or (self.reconcile is not None and not self.reconcile.is_consistent)
        )

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.pnl > 0) / len(self.trades)


def render(summary: DailySummary) -> str:
    """レポートの本文を組み立てる。

    **ファイル書き込みと分けてある。** 中身だけをテストしたいし、
    通知（メール・Slack）にも同じ本文を使うため。
    """
    lines: list[str] = [
        f"# 日次レポート {summary.trade_date}",
        "",
    ]

    # --- 最も高くつく異常を最初に ---
    if summary.has_alert:
        lines.append("## **要対応**")
        lines.append("")
        if summary.residual_positions:
            lines.append(
                f"- **建玉が {len(summary.residual_positions)} 件残っている。**"
                " 翌営業日に強制決済され1注文2,200円が発生する"
            )
            for position in summary.residual_positions:
                lines.append(
                    f"    - {position.symbol} {position.side.value} "
                    f"{position.quantity}株"
                )
        if summary.unresolved_orders:
            lines.append(
                f"- **送信したか不明な注文が {len(summary.unresolved_orders)} 件。**"
                " 翌朝の起動時に証券会社へ照会すること（二重発注を防ぐため）"
            )
            for entry in summary.unresolved_orders:
                lines.append(f"    - {entry.client_order_id} ({entry.symbol})")
        if summary.reconcile is not None and not summary.reconcile.is_consistent:
            lines.append("- **建玉の突合に失敗している。** 翌日は発注しない")
            lines.append(f"```\n{summary.reconcile.summary()}\n```")
        lines.append("")
    else:
        lines.append("要対応なし。")
        lines.append("")

    # --- 損益 ---
    lines.append("## 損益")
    lines.append("")
    lines.append(f"- 資産: {summary.starting_equity:,.0f} → {summary.ending_equity:,.0f} 円")
    lines.append(f"- 損益: {summary.pnl:+,.0f} 円（{summary.pnl_pct:+.2%}）")
    if summary.pnl_pct <= DAILY_LOSS_LIMIT_PCT:
        lines.append(
            f"- **日次損失上限 {DAILY_LOSS_LIMIT_PCT:.0%} に到達している**"
        )
    else:
        lines.append(
            f"- 日次損失上限 {DAILY_LOSS_LIMIT_PCT:.0%} まで "
            f"{summary.headroom_pct:.2%}"
        )
    lines.append("")

    # --- ブレーカー ---
    if summary.breakers_tripped:
        lines.append("## ブレーカー")
        lines.append("")
        for name in summary.breakers_tripped:
            lines.append(f"- {name}")
        lines.append("")

    # --- トレード ---
    lines.append("## トレード")
    lines.append("")
    lines.append(f"- 件数: {len(summary.trades)}")
    if summary.trades:
        lines.append(f"- 勝率: {summary.win_rate:.1%}")
        reasons: dict[str, int] = {}
        for trade in summary.trades:
            reasons[trade.exit_reason] = reasons.get(trade.exit_reason, 0) + 1
        lines.append(
            "- 手仕舞い理由: "
            + " / ".join(f"{k} {v}" for k, v in sorted(reasons.items()))
        )
    if summary.rejected_orders:
        lines.append(f"- 拒否された注文: {len(summary.rejected_orders)}件")
    lines.append("")

    if summary.notes:
        lines.append("## 備考")
        lines.append("")
        lines.extend(f"- {note}" for note in summary.notes)
        lines.append("")

    return "\n".join(lines)


def generate(summary: DailySummary, output_dir: Path) -> Path:
    """日次レポートを生成して保存する。

    Returns:
        生成したレポートのパス。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{summary.trade_date.isoformat()}.md"
    path.write_text(render(summary), encoding="utf-8")

    if summary.has_alert:
        # **通知を待たずにログにも出す。** 通知が届かないことがある
        logger.critical("日次レポートに要対応の事象がある: %s", path)
    else:
        logger.info("日次レポートを生成した: %s", path)
    return path
