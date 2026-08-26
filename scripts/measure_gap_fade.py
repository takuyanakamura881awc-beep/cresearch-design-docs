#!/usr/bin/env python3
"""ギャップ（前日終値と当日始値の差）がその日のうちに埋まる傾向があるか、安く診断する。

    python scripts/measure_gap_fade.py

事前に ``python scripts/fetch_bars.py`` などで ``data/`` に日足を
蓄積しておく（新規のネットワーク取得は行わない。ローカルの
``BarStore`` キャッシュだけを読む）。

【なぜこの診断なのか】

竹（ORB+VWAP混合）、VWAP乖離の2方向拡張（乖離2.0%・出来高確認）は
いずれも棄却が確定した（`docs/00-overview.md` 意思決定ログ46・50・52）。
ORB・VWAP乖離という手元の手がかりは使い切ったので、新しいシグナル発想が要る。

板情報は Stage A にないため使えない。**ギャップはまだ検証していない**うえ、
**日足だけで安く予備検証できる**という利点がある——5分足はまだ39〜80営業日
しかないが、日足は J-Quants 無料で最大2年（意思決定ログ53）。5分足の
検証環境（Layer2選定・約定モデル・リスクチェック）を一切構築せずに、
「そもそもこの銘柄群でギャップはフェードする傾向があるか」を桁違いに
大きい母数で先に見られる。

**Layer2 選定で使う `gap_pct`（寄り前気配 vs 前日終値）とは別物。**
選定用の寄り前気配は Stage A では取得できない（`docs/03-universe.md`）。
ここで見るのは**当日の実際の始値**——寄り付いた瞬間には確定している
情報で、先読みにならない。

【まだ合否判定はしない】

`--stress-test` のような多重比較補正した棄却判定はまだ行わない。
バケット間で符号・大きさが一貫してフェード側に振れているかを
目視で確認する診断であり、仮説を戦略化する段階で初めて検定が要る。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from autotrader.data.store import BarStore
from autotrader.provenance import banner
from autotrader.types import Bar, Symbol

DATA_ROOT = Path("data")

GAP_BUCKETS_PCT: tuple[float, ...] = (0.005, 0.010, 0.015, 0.020)
"""ギャップの下限バケット。VWAP乖離のスイープ（0.7/1.0/1.5/2.0%）と同じ刻み方。"""


@dataclass(frozen=True)
class GapFadePair:
    """1銘柄・1営業日ぶんのギャップとその日の値動き。"""

    symbol: str
    gap_pct: float
    """(当日始値 - 前日終値) / 前日終値。"""
    intraday_return_pct: float
    """(当日終値 - 当日始値) / 当日始値。"""


def gap_fade_pairs(daily_bars: dict[str, tuple[Bar, ...]]) -> tuple[GapFadePair, ...]:
    """銘柄ごとの日足から、ギャップと当日の値動きのペアを作る。

    **銘柄の初日（前日終値がない）は除外する。** 始値・前日終値が0以下の
    日も除外する（0除算対策。`autotrader.regime.daily_range_pct` と同じ規律）。
    """
    pairs: list[GapFadePair] = []
    for symbol, series in daily_bars.items():
        ordered = sorted(series, key=lambda b: b.timestamp)
        for prev, today in zip(ordered, ordered[1:], strict=False):
            if prev.close <= 0 or today.open <= 0:
                continue
            gap_pct = (today.open - prev.close) / prev.close
            intraday_return_pct = (today.close - today.open) / today.open
            pairs.append(
                GapFadePair(
                    symbol=symbol,
                    gap_pct=gap_pct,
                    intraday_return_pct=intraday_return_pct,
                )
            )
    return tuple(pairs)


def fade_score(pair: GapFadePair) -> float:
    """ギャップ方向と逆に動いたら正（フェード）、伸びたら負（ギャップ&ゴー）。

    ギャップがゼロの日は符号がないので0を返す（フェードもギャップ&ゴーもない）。
    """
    if pair.gap_pct > 0:
        return -pair.intraday_return_pct
    if pair.gap_pct < 0:
        return pair.intraday_return_pct
    return 0.0


def load_symbols() -> tuple[Symbol, ...]:
    """`scripts/backtest_take.py` の同名関数と同じ読み込み。

    **重複させている。** スクリプトファイルは pythonpath に乗らないため
    （`tests/test_backtest_take_script.py` の docstring 参照）、
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


def hr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def report(pairs: tuple[GapFadePair, ...]) -> None:
    if not pairs:
        print("  データがない。先に python scripts/fetch_bars.py を実行する")
        return

    baseline = sum(fade_score(p) for p in pairs) / len(pairs)
    print(f"  全日ベースライン: 件数 {len(pairs):>6} / fade_score平均 {baseline:>+8.4%}")
    print()
    print(f"  {'|ギャップ|下限':<14} {'件数':>8} {'fade_score平均':>14}")
    print("  " + "-" * 40)
    for threshold in GAP_BUCKETS_PCT:
        bucket = [p for p in pairs if abs(p.gap_pct) >= threshold]
        if not bucket:
            print(f"  {threshold:>12.1%}  {0:>8}  —（該当なし）")
            continue
        avg = sum(fade_score(p) for p in bucket) / len(bucket)
        print(f"  {threshold:>12.1%}  {len(bucket):>8}  {avg:>+13.4%}")

    print()
    print("  **まだ合否判定はしない。** 閾値を上げるほどベースラインより")
    print("  はっきりフェード側（正）に振れるか、目視で確認する診断。")
    print("  一貫していれば実際の戦略として実装する価値がある。")
    print("  一貫しなければこの切り口には手がかりがない。")


def main() -> int:
    print("ギャップ・フェード診断（日足のみ・安価な予備検証）")
    print(banner())

    symbols = load_symbols()
    store = BarStore(DATA_ROOT)
    daily = {s.code: store.read(s.code, "1d") for s in symbols}
    daily = {c: b for c, b in daily.items() if b}
    print(f"  日足あり: {len(daily)}銘柄")

    pairs = gap_fade_pairs(daily)
    hr("結果")
    report(pairs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
