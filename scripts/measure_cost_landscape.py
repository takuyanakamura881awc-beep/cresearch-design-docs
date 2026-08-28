#!/usr/bin/env python3
"""往復コストの地図を作る。**仮説検定ではなく算術なので、偽陽性が原理的に出ない。**

    python scripts/measure_cost_landscape.py            # 地図だけ（データ不要）
    python scripts/measure_cost_landscape.py --refresh  # 全上場銘柄の実測を取り直す

【なぜこれを作るのか】

6つの手法がすべて棄却された（`docs/00-overview.md` 意思決定ログ88・92）。
共通の形は「必要 gross 14〜20bps に対し、見つかる優位は 0〜5bps」。
**個々のシグナルの当たり外れではなく、コスト構造そのものが効いている。**

そこで「前提として定義していた部分を変えたらどうなるか」を問う（意思決定ログ94）。
**ただし手法 × ユニバース × 閾値を総当たりすると、多重比較で偶然の当たりを拾う**
——5変種の比較にすら99%補正が要った（意思決定ログ45）。

**なので最初にやるのは検定ではなく地図。** 往復コストは

    往復コスト（bps） = 呼値 × 2本 ÷ 株価 × 10,000

という**株価の決定関数**であり、データを1件も見ずに書ける。
**地図は偽陽性を出せない。** 見てから仮説を1つだけ事前登録する。

【地図が示すこと（ゼロデータで分かる）】

呼値は絶対額なので、コストは株価の**のこぎり波**になる——
呼値が変わる境界の直下が最安で、境界を1円超えると跳ね上がる。

    通常銘柄（3,000円以下は呼値1円）
      1,250円 → 16.0bps   ← **資金50万円の株価上限**
      3,000円 →  6.7bps   ← 最安
      3,001円 → 33.3bps   ← 崖（呼値5円）

**株価上限1,250円（= 資金50万円 × 25% ÷ 100株）は、通常銘柄の
最も高いコスト帯に我々を閉じ込めている。** Layer 1 で測った 21〜30bps は
そこから来ている。

【この地図の使い方】

**ユニバースは成績で選ばない。** コストと流動性と銘柄数という
**構造的な基準だけ**で選ぶ。成績で選ぶのは、資金曲線の最大値を後から
選ぶのと同じ（意思決定ログ69で「採用しない」と登録済み）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from autotrader.config import load_credentials, mask
from autotrader.data.base import DataSourceError
from autotrader.data.jquants import FREE_PLAN_DELAY_DAYS, JQuantsDataSource
from autotrader.provenance import banner
from autotrader.tick import DEFAULT_SPREAD_TICKS, spread_yen
from autotrader.universe.filters import (
    DEFAULT_MIN_AVG_TURNOVER_YEN,
    DEFAULT_TURNOVER_LOOKBACK_DAYS,
)

DATA_ROOT = Path("data")
SNAPSHOT_PATH = DATA_ROOT / "market_snapshot.json"

MAX_POSITION_PCT = Decimal("0.25")
"""1銘柄あたりの建玉上限（安全装置#7）。**動かさない**（意思決定ログ21）。"""

LOT_SIZE = 100
"""単元株数。"""

CAPITAL_CANDIDATES_YEN: tuple[int, ...] = (
    500_000,
    800_000,
    1_200_000,
    2_000_000,
    3_000_000,
    5_000_000,
)
"""資金スイープの候補。他のスクリプトと刻みを揃えてある。"""

COST_BUCKETS_BPS: tuple[float, ...] = (5.0, 10.0, 20.0, 40.0)
"""往復コストの区切り（bps）。

年利25%に要る gross は「日次9.3bps + コスト」なので（`diagnostics.required_gross_bps`）、
**コスト5bps なら必要14bps、40bps なら必要49bps**。この差が効く。
"""


def round_trip_bps(price: float, *, topix100: bool = False) -> float:
    """往復コスト（bps）。**`autotrader.tick` をそのまま使う。**

    約定コストのモデルを診断ごとに作り直さない（意思決定ログ33以降）。
    """
    if price <= 0:
        raise ValueError("price は0より大きい")
    return float(spread_yen(price, DEFAULT_SPREAD_TICKS, topix100=topix100)) / price * 10_000.0


def price_ceiling_yen(capital_yen: int) -> Decimal:
    """その資金で建てられる株価の上限。

    ``資金 × 25%（安全装置#7）÷ 100株``。**これは市場の性質ではなく
    資金の制約**（意思決定ログ64）なので、資金を振れば動く。
    """
    return Decimal(capital_yen) * MAX_POSITION_PCT / LOT_SIZE


def cheapest_price_at_or_below(ceiling: Decimal, *, topix100: bool = False) -> Decimal:
    """``ceiling`` 以下で往復コストが最安になる株価。

    **呼値は絶対額なのでコストは株価ののこぎり波**になる——
    呼値が変わる境界の直下が最安。100株単位で買うので、
    1円刻みで探せば十分（実際の候補は境界直下と ceiling のみ）。
    """
    if ceiling <= 0:
        raise ValueError("ceiling は0より大きい")
    best = Decimal(1)
    best_bps = float("inf")
    # 呼値が変わる境界の直下と、上限そのものだけが候補になる
    candidates = [ceiling]
    for boundary in (1_000, 3_000, 5_000, 10_000, 30_000):
        edge = Decimal(boundary)
        if edge <= ceiling:
            candidates.append(edge)
    for price in candidates:
        bps = round_trip_bps(float(price), topix100=topix100)
        if bps < best_bps:
            best, best_bps = price, bps
    return best


@dataclass(frozen=True)
class MarketRow:
    """1銘柄ぶんのスナップショット。**地図を描くのに要る最小限。**"""

    code: str
    name: str
    price: float
    """直近の終値。"""
    avg_turnover_yen: float
    """20営業日の平均売買代金。**流動性の下限判定に使う。**"""
    topix100: bool
    scale_category: str | None

    @property
    def cost_bps(self) -> float:
        return round_trip_bps(self.price, topix100=self.topix100)

    def affordable(self, capital_yen: int) -> bool:
        """その資金で1単元建てられるか（安全装置#7 の1銘柄25%上限）。"""
        return Decimal(str(self.price)) <= price_ceiling_yen(capital_yen)


def tradable(
    rows: tuple[MarketRow, ...],
    capital_yen: int,
    *,
    min_turnover_yen: float = float(DEFAULT_MIN_AVG_TURNOVER_YEN),
    max_cost_bps: float = float("inf"),
) -> tuple[MarketRow, ...]:
    """その資金・その流動性下限・そのコスト上限で扱える銘柄。

    **成績は一切見ない。** 構造的な基準だけで絞る——成績で選ぶのは
    資金曲線の最大値を後から選ぶのと同じ（意思決定ログ69）。
    """
    return tuple(
        r
        for r in rows
        if r.affordable(capital_yen)
        and r.avg_turnover_yen >= min_turnover_yen
        and r.cost_bps <= max_cost_bps
    )


def hr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def _report_cost_curve() -> None:
    """株価と往復コストの関係。**データを1件も使わない。**"""
    hr("1. 往復コストは株価ののこぎり波（データ不要・純粋な算術）")
    print("  往復コスト（bps） = 呼値 × 2本 ÷ 株価 × 10,000")
    print("  **呼値は絶対額なので、境界の直下が最安・境界を超えると跳ねる。**")
    print()
    print(f"  {'株価':>9} {'通常銘柄':>10} {'TOPIX100':>10}   {'メモ':<28}")
    print("  " + "-" * 64)
    notes = {
        500: "",
        1_000: "TOPIX100 の呼値境界",
        1_250: "**資金50万円の株価上限**",
        2_000: "",
        2_999: "通常銘柄の最安",
        3_001: "崖（通常の呼値 1→5円）",
        5_000: "",
        5_001: "崖（通常の呼値 5→10円）",
        10_000: "",
    }
    for price, note in notes.items():
        print(
            f"  {price:>8,}円 {round_trip_bps(price):>9.1f}b "
            f"{round_trip_bps(price, topix100=True):>9.1f}b   {note:<28}"
        )
    print()
    print("  **株価上限1,250円は、通常銘柄の最も高いコスト帯に我々を閉じ込めている。**")
    print("  Layer 1 で測った 21〜30bps はそこから来ている（意思決定ログ67）。")


def _report_capital_ceiling() -> None:
    """資金と、その資金で狙える最安コスト。**これもデータ不要。**"""
    hr("2. 資金ごとの株価上限と、そこで狙える最安コスト")
    print("  株価上限 = 資金 × 25%（安全装置#7）÷ 100株")
    print("  **市場の性質ではなく資金の制約**なので、資金を振れば動く。")
    print()
    print(
        f"  {'資金':>11} {'株価上限':>10} {'最安の株価':>11} "
        f"{'通常':>8} {'TOPIX100':>10}"
    )
    print("  " + "-" * 56)
    for capital in CAPITAL_CANDIDATES_YEN:
        ceiling = price_ceiling_yen(capital)
        best_regular = cheapest_price_at_or_below(ceiling)
        best_topix = cheapest_price_at_or_below(ceiling, topix100=True)
        print(
            f"  {capital:>10,}円 {float(ceiling):>9,.0f}円 "
            f"{float(best_regular):>10,.0f}円 "
            f"{round_trip_bps(float(best_regular)):>7.1f}b "
            f"{round_trip_bps(float(best_topix), topix100=True):>9.1f}b"
        )
    print()
    print("  **通常銘柄でも 3,000円まで買えれば 6.7bps** で、TOPIX100 の実測")
    print("  平均4.75bps に匹敵する。そこには資金120万円が要る。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="全上場銘柄の株価・売買代金を J-Quants から取り直す（数分〜十数分）",
    )
    args = parser.parse_args()

    print("往復コストの地図（仮説検定ではなく算術）")
    print(banner())

    _report_cost_curve()
    _report_capital_ceiling()

    rows = _load_snapshot() if not args.refresh else None
    if rows is None:
        rows = _refresh_snapshot()
    if rows is None:
        hr("3. 実際に何銘柄あるか")
        print("  スナップショットが無い。--refresh で取得する")
        print("  （J-Quants の get_bars_for_date で全銘柄を一括取得する）")
        return 0

    _report_inventory(rows)
    return 0


def _report_inventory(rows: tuple[MarketRow, ...]) -> None:
    """コスト帯ごとに、実際に何銘柄あるか。**ここで初めてデータを使う。**"""
    hr("3. 資金 × コスト帯ごとの銘柄数（実測）")
    print(f"  流動性下限: 20日平均売買代金 {DEFAULT_MIN_AVG_TURNOVER_YEN:,}円")
    print(f"  対象: {len(rows)}銘柄")
    print()
    header = "  ".join(f"{b:>4.0f}bps以下" for b in COST_BUCKETS_BPS)
    print(f"  {'資金':>11} {'株価上限':>9}  {header}")
    print("  " + "-" * (24 + 12 * len(COST_BUCKETS_BPS)))
    for capital in CAPITAL_CANDIDATES_YEN:
        cells = [
            f"{len(tradable(rows, capital, max_cost_bps=b)):>9}銘柄"
            for b in COST_BUCKETS_BPS
        ]
        print(
            f"  {capital:>10,}円 {float(price_ceiling_yen(capital)):>8,.0f}円  "
            + "  ".join(cells)
        )
    print()
    print("  **日次の監視枠は50銘柄**（`universe/selector.py`）。")
    print("  それを埋められるだけの銘柄が、安いコスト帯にあるかを見る。")

    print()
    print("  【規模区分の内訳】コスト20bps以下・流動性を満たす銘柄")
    for capital in (1_200_000, 2_000_000):
        pool = tradable(rows, capital, max_cost_bps=20.0)
        by_scale: dict[str, int] = {}
        for r in pool:
            by_scale[r.scale_category or "（区分なし）"] = (
                by_scale.get(r.scale_category or "（区分なし）", 0) + 1
            )
        print(f"    資金{capital:,}円 → {len(pool)}銘柄")
        for scale, n in sorted(by_scale.items(), key=lambda kv: -kv[1])[:6]:
            print(f"      {scale:<20} {n:>4}")

    print()
    print("  **ユニバースは成績で選ばない。** コストと流動性と銘柄数という")
    print("  構造的な基準だけで選ぶ（意思決定ログ69・94）。")


def _load_snapshot() -> tuple[MarketRow, ...] | None:
    if not SNAPSHOT_PATH.is_file():
        return None
    payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    print(f"  スナップショット: {SNAPSHOT_PATH}（基準日 {payload.get('as_of')}）")
    return tuple(
        MarketRow(
            code=r["code"],
            name=r["name"],
            price=r["price"],
            avg_turnover_yen=r["avg_turnover_yen"],
            topix100=r["topix100"],
            scale_category=r.get("scale_category"),
        )
        for r in payload["rows"]
    )


def _refresh_snapshot() -> tuple[MarketRow, ...] | None:
    """全上場銘柄の株価と売買代金を取る。

    **`get_bars_for_date` を使う**——``date=`` を指定すると全銘柄が返るので、
    銘柄ごとにループするより約4分の1のリクエストで済む（`jquants.py` の注記）。
    """
    hr("全上場銘柄のスナップショットを取得する")
    try:
        creds = load_credentials(require_kabus=False)
    except RuntimeError as exc:
        print(f"  NG: {exc}")
        return None
    print(f"  JQUANTS_API_KEY: {mask(creds.jquants_api_key)}")
    source = JQuantsDataSource(creds.jquants_api_key)

    as_of = date.today() - timedelta(days=FREE_PLAN_DELAY_DAYS)
    symbols = source.list_symbols(as_of)
    if not symbols:
        print("  銘柄一覧が取れなかった")
        return None
    meta = {s.code: s for s in symbols}
    print(f"  銘柄一覧: {len(symbols)}銘柄（基準日 {as_of}）")

    closes: dict[str, float] = {}
    turnover: dict[str, list[float]] = {}
    collected = 0
    day = as_of
    print(f"  {DEFAULT_TURNOVER_LOOKBACK_DAYS}営業日ぶんを一括取得する（数分かかる）")
    while collected < DEFAULT_TURNOVER_LOOKBACK_DAYS and (as_of - day).days < 60:
        try:
            bars = source.get_bars_for_date(day)
        except DataSourceError as exc:
            print(f"    {day}: {exc}")
            day -= timedelta(days=1)
            continue
        if bars:
            collected += 1
            for code, series in bars.items():
                for bar in series:
                    if bar.close <= 0:
                        continue
                    closes.setdefault(code, bar.close)
                    turnover.setdefault(code, []).append(bar.close * bar.volume)
            print(f"    {day}: {len(bars)}銘柄（{collected}/{DEFAULT_TURNOVER_LOOKBACK_DAYS}）")
        day -= timedelta(days=1)

    if not closes:
        print("  日足が取れなかった")
        return None

    rows = tuple(
        MarketRow(
            code=code,
            name=meta[code].name if code in meta else code,
            price=price,
            avg_turnover_yen=sum(turnover[code]) / len(turnover[code]),
            topix100=meta[code].is_topix100 if code in meta else False,
            scale_category=meta[code].scale_category if code in meta else None,
        )
        for code, price in closes.items()
        if turnover.get(code)
    )
    _save_snapshot(as_of, rows)
    return rows


def _save_snapshot(as_of: date, rows: tuple[MarketRow, ...]) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps(
            {
                "as_of": as_of.isoformat(),
                "note": "全上場銘柄の株価と20日平均売買代金。コストの地図に使う",
                "rows": [
                    {
                        "code": r.code,
                        "name": r.name,
                        "price": r.price,
                        "avg_turnover_yen": r.avg_turnover_yen,
                        "topix100": r.topix100,
                        "scale_category": r.scale_category,
                    }
                    for r in rows
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"  保存: {SNAPSHOT_PATH}（{len(rows)}銘柄）")


if __name__ == "__main__":
    sys.exit(main())
