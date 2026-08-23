"""場中スケジュール（docs/06-operations.md §1）。

=========  ==========================================================
時刻       処理
=========  ==========================================================
07:00      ユニバース構築（Layer 1）
08:00      日次銘柄選定（Layer 2）→ WebSocket 50銘柄を登録
08:55      **起動時 reconcile**（不一致ならその日は発注しない）
09:00      場開始。分足 Recorder 稼働、シグナル監視開始
09:30      オープニングレンジ確定 → シグナルA の判定開始
**14:50**  **全建玉クローズ → GET /positions で残存確認**
15:30      場終了
16:00      日次レポート生成・通知
=========  ==========================================================

【設計方針】

**時刻の判定と実行を分ける。** `due()` が「今実行すべきジョブ」を返し、
`run_once()` がそれを実行する。時計を注入できるので、
1日ぶんの進行を実時間を待たずにテストできる。

滅多に走らないコードは、いざという時に動かない。
**14:50 のクローズは毎営業日必ず走る**ことで信頼性を稼ぐ（docs/05 原則2）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    """定時ジョブ。"""

    at: time
    name: str
    run: Callable[[], None]
    critical: bool = False
    """失敗を握り潰さず、後続ジョブより優先して扱うか。

    **14:50 のクローズだけが True。** 他のジョブの失敗は当日の機会損失で
    済むが、クローズの失敗は翌営業日に1注文2,200円の実損になる。
    """


class MarketScheduler:
    """場中のジョブスケジューラ。

    14:50 の全建玉クローズは**最優先で、他のジョブに阻害されず実行**されること。
    閉じ損ねると翌営業日に1注文2,200円のペナルティが発生する。
    """

    MARKET_OPEN = time(9, 0)
    MARKET_CLOSE = time(15, 30)
    CLOSE_ALL_TIME = time(14, 50)
    """全建玉クローズの時刻。**暫定値。**

    大引け直前は板が薄くスリッページが増えるため、
    ペーパー期間の実測で調整する。
    """

    def __init__(self) -> None:
        self._jobs: list[Job] = []
        self._done: dict[date, set[str]] = {}

    def register(
        self,
        at: time,
        job: Callable[[], None],
        name: str,
        *,
        critical: bool = False,
    ) -> None:
        """時刻指定でジョブを登録する。

        **同じ名前を二度登録できない。** 同じジョブが2回走ると、
        たとえばクローズなら二重返済を試みることになる。

        Raises:
            ValueError: 同じ名前が既に登録されている場合。
        """
        if any(existing.name == name for existing in self._jobs):
            raise ValueError(f"ジョブ名が重複している: {name}")
        self._jobs.append(Job(at=at, name=name, run=job, critical=critical))
        self._jobs.sort(key=lambda j: (j.at, j.name))

    def due(self, now: datetime) -> tuple[Job, ...]:
        """``now`` の時点で実行すべき未実行のジョブ。

        **時刻を過ぎたものはすべて返す。** 「ちょうどその時刻」でしか
        拾わない設計だと、プロセスが数分止まっただけで
        14:50 のクローズを飛ばしてしまう。

        **critical を先に返す。** 同じ時刻に複数あるとき、
        クローズが他のジョブの後ろに回らないようにする。
        """
        done = self._done.get(now.date(), set())
        pending = [j for j in self._jobs if j.at <= now.time() and j.name not in done]
        return tuple(sorted(pending, key=lambda j: (not j.critical, j.at, j.name)))

    def run_once(self, now: datetime) -> tuple[str, ...]:
        """``now`` の時点で実行すべきジョブをすべて走らせる。

        **1つのジョブの失敗で他を止めない。** 例外はログに残して次へ進む。
        止めると、後続の 14:50 クローズまで巻き添えになる。

        ただし ``critical`` なジョブの失敗は **CRITICAL でログに残す**。
        握り潰さないが、他のジョブの実行は妨げない。

        Returns:
            実行したジョブ名。**失敗したものも含む**（実行済みとして扱い、
            同じ日に再実行しない。再試行はジョブ自身の責務）。
        """
        executed: list[str] = []
        for job in self.due(now):
            self._done.setdefault(now.date(), set()).add(job.name)
            executed.append(job.name)
            try:
                job.run()
            except Exception:
                if job.critical:
                    logger.critical(
                        "**最重要ジョブ %s が失敗した。** 人が確認すること",
                        job.name,
                        exc_info=True,
                    )
                else:
                    logger.error("ジョブ %s が失敗した", job.name, exc_info=True)
        return tuple(executed)

    def reset_day(self, day: date) -> None:
        """その日の実行済み記録を消す。

        日付が変われば `due` は自動で未実行に戻るので、通常は不要。
        テストと、同じ日をやり直すとき（障害復旧）に使う。
        """
        self._done.pop(day, None)

    def is_market_hours(self, now: datetime) -> bool:
        """場中か。分足の監視ループを回すかの判定に使う。"""
        return self.MARKET_OPEN <= now.time() < self.MARKET_CLOSE

    def jobs(self) -> tuple[Job, ...]:
        """登録済みジョブ。時刻順。"""
        return tuple(self._jobs)
