#!/usr/bin/env python3
"""スプレッドが呼値の何本ぶんかを、手元の日足から推定する。

    python scripts/measure_spread.py

事前に ``python scripts/fetch_bars.py`` で日足を貯めておく
（新規のネットワーク取得は行わない）。

【何を確かめるのか】

``tick.DEFAULT_SPREAD_TICKS = 2.0`` は **このプロジェクト最大の当て推量**。
Stage A では板が取れないので直接は測れないが、**この1つの数値が
全コスト計算を線形にスケールする**——往復コスト、`min_atr_yen`、
Layer 2 のコストスコア、バックテストの約定価格、損益分岐の勝率。

これまでの3実験（竹・VWAP乖離単独・ギャップフェード）はすべて
「gross は小さく正、コストがそれを上回って net 負」で終わっている。
**この構図が「優位が本当にない」のか「コストを過大に見積もっている」
のかは、スプレッドが分からないと切り分けられない。**

日足の高安だけから実効スプレッドを推定する Corwin-Schultz（2012）を
`autotrader.spread` に実装してあるので、それを全銘柄に当てて
**呼値（`autotrader.tick.tick_size`）の何本ぶんか**に換算する。

【この結果をどう使うか】

- **2本前後** → 現行の想定は妥当。コストは過大評価ではなく、
  これまでの net 負は本物。手法探しを続けるより、コスト構造そのもの
  （保有期間を延ばす・より高い株価帯）を見直す段階
- **1本前後** → コストを倍に見積もっていたことになる。
  **既存の全実験を再評価する必要がある**（gross は変わらないが net は変わる）
- **3本以上** → さらに厳しい。デイトレの往復では勝ち目が薄いことの傍証

**推定値は下限として扱う。** Corwin-Schultz は取引がまばらな銘柄ほど
下振れする（`autotrader.spread` の docstring 参照）。
「思ったより狭かった」という結果が出ても、**楽観側には倒さない**。
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

from autotrader.data.store import BarStore
from autotrader.provenance import banner
from autotrader.spread import corwin_schultz
from autotrader.tick import DEFAULT_SPREAD_TICKS, tick_size
from autotrader.types import Symbol

DATA_ROOT = Path("data")

MIN_PAIRS = 100
"""推定に使う最低ペア数。**これ未満の銘柄は結果に混ぜない。**

日数が少ないと β・γ の平均が安定せず、推定が大きく振れる。
"""


def hr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def load_symbols() -> tuple[Symbol, ...]:
    """`scripts/backtest_take.py` の同名関数と同じ読み込み。

    **重複させている。** スクリプトファイルは pythonpath に乗らないため、
    スクリプト間で import せず、それぞれ自己完結させる。
    """
    import json

    path = DATA_ROOT / "universe.json"
    if not path.is_file():
        raise SystemExit(f"{path} がない。先に python scripts/fetch_bars.py を実行する")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        Symbol(
            code=r["code"],
            name=r["name"],
            market=r.get("market"),
            margin_type=r.get("margin_type"),
            sector=r.get("sector"),
        )
        for r in payload["symbols"]
    )


def main() -> int:
    print("スプレッドを日足から推定する（Corwin-Schultz 2012）")
    print(banner())

    symbols = load_symbols()
    store = BarStore(DATA_ROOT)

    ticks: list[float] = []
    bps_values: list[float] = []
    n_unusable = 0
    n_short = 0

    for symbol in symbols:
        bars = store.read(symbol.code, "1d")
        if not bars:
            continue
        estimate = corwin_schultz(bars)
        if estimate is None:
            continue
        if estimate.n_pairs < MIN_PAIRS:
            n_short += 1
            continue
        if not estimate.usable:
            # 負の推定値。**0とみなさず、推定不能として数える**
            n_unusable += 1
            continue

        # 直近の終値を代表価格にして、呼値の何本ぶんかへ換算する
        price = sorted(bars, key=lambda b: b.timestamp)[-1].close
        if price <= 0:
            continue
        spread_yen_estimated = estimate.spread_pct * price
        ticks.append(spread_yen_estimated / float(tick_size(price)))
        bps_values.append(estimate.spread_bps)

    hr("結果")
    print(f"  推定できた銘柄: {len(ticks)}")
    print(f"  推定不能（負の値）: {n_unusable}")
    print(f"  日数不足（{MIN_PAIRS}ペア未満）: {n_short}")

    if not ticks:
        print()
        if n_unusable > 0:
            # **「データがない」と「推定が効かない」を混同しない。**
            # 前者は取り直せば直るが、後者は推定量が
            # この銘柄群に合っていないという別の問題
            print("  **推定が効いていない**（負の値ばかり）。データ不足ではない。")
            print("  γ（2日通しの高安）が β（各日の高安の和）を上回り続けている。")
            print("  夜間ギャップの補正が効いていない可能性が高い")
            print("  （`autotrader.spread` の docstring 参照）。")
        else:
            print("  **推定できた銘柄がない。** 先に日足を貯める:")
            print("    python scripts/fetch_bars.py")
        return 1

    print()
    print(f"  {'':16}{'中央値':>10}{'平均':>10}{'下位25%':>10}{'上位25%':>10}")
    print("  " + "-" * 56)
    quantiles_bps = statistics.quantiles(bps_values, n=4) if len(bps_values) >= 4 else None
    quantiles_ticks = statistics.quantiles(ticks, n=4) if len(ticks) >= 4 else None
    if quantiles_bps and quantiles_ticks:
        print(
            f"  {'スプレッド(bps)':16}{statistics.median(bps_values):>10.1f}"
            f"{statistics.fmean(bps_values):>10.1f}"
            f"{quantiles_bps[0]:>10.1f}{quantiles_bps[2]:>10.1f}"
        )
        print(
            f"  {'呼値の本数':16}{statistics.median(ticks):>10.2f}"
            f"{statistics.fmean(ticks):>10.2f}"
            f"{quantiles_ticks[0]:>10.2f}{quantiles_ticks[2]:>10.2f}"
        )
    else:
        print(f"  スプレッド(bps) 中央値 {statistics.median(bps_values):.1f}")
        print(f"  呼値の本数     中央値 {statistics.median(ticks):.2f}")

    median_ticks = statistics.median(ticks)
    print()
    print(f"  **現行の想定: {DEFAULT_SPREAD_TICKS:.1f}本** / 推定の中央値: {median_ticks:.2f}本")
    print()
    if median_ticks < DEFAULT_SPREAD_TICKS * 0.75:
        print("  → 想定より**狭い**。コストを過大に見積もっていた可能性がある。")
        print("     既存の実験は gross は変わらないが net が変わるので再評価が要る。")
        print("     **ただし推定は下振れする側なので、鵜呑みにして楽観側へ倒さない。**")
    elif median_ticks > DEFAULT_SPREAD_TICKS * 1.25:
        print("  → 想定より**広い**。コストはさらに重く、デイトレの往復は不利。")
        print("     保有期間を延ばすか、コスト構造そのものを見直す段階。")
    else:
        print("  → 現行の想定は**妥当**。これまでの net 負はコストの過大評価ではない。")
        print("     手法を探し続けるより、コスト構造（保有期間・株価帯）を見直す。")

    print()
    print("  **これは推定であって実測ではない。** 取引がまばらな銘柄ほど")
    print("  下振れする（`autotrader.spread` 参照）。板を録れるように")
    print("  なったら（楽天 マーケットスピード II RSS 等）上書きする。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
