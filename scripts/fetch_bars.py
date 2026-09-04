#!/usr/bin/env python3
"""実データのバックテストに要るバーを取得してキャッシュする。

    python scripts/fetch_bars.py              # 足りないぶんだけ取る
    python scripts/fetch_bars.py --refresh    # ユニバースから取り直す

【3段構え】

===========  ==============  ====================================================
段           データ源        用途
===========  ==============  ====================================================
1. 銘柄一覧  J-Quants        市場区分・信用区分（**yfinance では取れない**）
2. 日足      yfinance        Layer 2 の日次選定（ATR%・売買代金・株価）
3. 5分足     yfinance        約定のシミュレーション（58日ぶん）
===========  ==============  ====================================================

段1だけ J-Quants で、しかも**1回で済む**（結果を ``data/universe.json`` に保存）。
5件/分の制約下で8〜12分かかるので、毎回は叩かない。

【Stage A の既知の制約 — 継ぎ目のズレ】

J-Quants Free の終端は2026-05-31、yfinance の5分足は58日ぶん。
**ユニバースを選んだ日と5分足のある期間が約28日ずれる**（docs/09 §2.4）。

段1の銘柄リストは「5/29 時点で Layer 1 を通った133銘柄」で、
検証期間（6月末〜8月）から見ると**1ヶ月古い**。
ルックアヘッドではない（過去の情報を未来に適用しているだけ）が、
本来の Layer 2 は日次で選び直す。

そこで**段2の日足は検証期間内のぶんを取り、日次の選定はそちらで回す**。
静的属性（市場・信用区分）だけ段1のものを使う。
この制約は有料プランか Stage B で解消する。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from autotrader.config import load_credentials, mask
from autotrader.data.base import DataSourceError, RateLimitError
from autotrader.data.jquants import JQuantsDataSource
from autotrader.data.store import BarStore
from autotrader.data.yahoo import MAX_LOOKBACK_DAYS, YahooDataSource
from autotrader.types import Bar, Symbol
from autotrader.universe.builder import build
from autotrader.universe.filters import FilterConfig
from autotrader.universe.selector import DEFAULT_MAX_WATCHLIST

DATA_ROOT = Path("data")
UNIVERSE_PATH = DATA_ROOT / "universe.json"
TURNOVER_DAYS = 20

DAILY_LOOKBACK_DAYS = 730
"""日足を遡る暦日数。

**Layer 2 の指標そのものは20営業日ぶんで足りる**（`SelectorConfig.min_bars`）。
5分足の期間の先頭よりさらに前から確保しないと、検証初日に選定できない、
というのが本来の下限。

**それより大きく（約2年）取っているのは `measure_gap_fade.py` のため。**
120日（約85営業日）のままでは、日足診断の強み（J-Quants無料の
2年分という桁違いの母数）を活かせない——実測でも1銘柄あたり約78日
しか貯まっておらず、5分足の検証窓（39〜80営業日）に対して2倍程度の
優位しかなかった。yfinance の日足取得は期間による追加コストが
ほぼない（`docs/09-data-sources.md` §2.1: 1d=制限なし）ので、
毎週の取得コストを増やさずに済む。
"""


def hr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# ---------------------------------------------------------------------------
# 段1: 銘柄一覧（J-Quants）
# ---------------------------------------------------------------------------


def load_universe() -> tuple[Symbol, ...] | None:
    """保存済みの銘柄一覧を読む。無ければ ``None``。"""
    if not UNIVERSE_PATH.is_file():
        return None
    payload = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    return tuple(
        Symbol(
            code=row["code"],
            name=row["name"],
            market=row.get("market"),
            margin_type=row.get("margin_type"),
            sector=row.get("sector"),
            # `save_universe` は書いているのに読み落としていた。
            # **呼値の判定に効く**（`Symbol.is_topix100`）
            scale_category=row.get("scale_category"),
        )
        for row in payload["symbols"]
    )



def load_topix100() -> tuple[Symbol, ...]:
    """TOPIX100 構成銘柄。`scripts/measure_topix100.py` が作るキャッシュを読む。

    **なぜここで要るのか。** `universe.json` は Layer 1（プライム・貸借・
    株価上限1,250円）を通った小型〜中型株で、**この銘柄群では竹・VWAP乖離・
    ギャップ・フェードの3つがすべて棄却された**。一方、唯一 net 正が出た
    TOPIX100（呼値0.1〜0.5円）の5分足は**1本も貯まっていなかった**——
    このスクリプトが `universe.json` の銘柄しか取りに行かないため。

    **yfinance の5分足は58日ぶんしか遡れない**（`MAX_LOOKBACK_DAYS`）。
    取らなかった週のデータは**二度と手に入らない**ので、
    仮説がまだ生きているうちに貯め始める（意思決定ログ76）。

    **無ければ空を返す。** 銘柄一覧そのものの取得は J-Quants の
    レート制限で8〜12分かかるので、`measure_topix100.py --refresh` の
    責務にしてある（このスクリプトは取りに行かない）。
    """
    for name in ("master_scale_historical.json", "master_scale.json"):
        path = DATA_ROOT / name
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        symbols = tuple(
            Symbol(
                code=row["code"],
                name=row["name"],
                market=row.get("market"),
                margin_type=row.get("margin_type"),
                sector=row.get("sector"),
                scale_category=row.get("scale_category"),
            )
            for row in payload["symbols"]
        )
        return tuple(s for s in symbols if s.is_topix100)
    return ()


def load_cheap_universe() -> tuple[Symbol, ...]:
    """コストで切り出したユニバース。`scripts/measure_cost_landscape.py` が作る。

    **なぜここで要るのか。** `universe.json`（Layer 1・株価≤1,250円）でも
    TOPIX100 でも3〜5手法が棄却された。地図を作ったところ、
    **安く取引できる銘柄の大半は中型・小型株の高株価帯（2,000〜3,000円）**
    で、その432銘柄を丸ごと飛ばしていたことが分かった（意思決定ログ95）。

    **成績で選んだ銘柄群ではない**——コスト・流動性・銘柄数という
    構造的な基準だけで切り出してある（`select_universe`）。

    **無ければ空を返す。** 一覧の作成は `measure_cost_landscape.py --refresh`
    の責務にしてある（J-Quants の全銘柄取得に十数分かかるため）。
    """
    path = DATA_ROOT / "universe_cheap.json"
    if not path.is_file():
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        Symbol(code=row["code"], name=row["name"], scale_category=row.get("scale_category"))
        for row in payload["symbols"]
    )


def save_universe(as_of: date, symbols: tuple[Symbol, ...]) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    UNIVERSE_PATH.write_text(
        json.dumps(
            {
                "as_of": as_of.isoformat(),
                "note": "J-Quants の Layer 1 通過銘柄。市場区分と信用区分の取得元",
                "symbols": [
                    {
                        "code": s.code,
                        "name": s.name,
                        "market": s.market,
                        "margin_type": s.margin_type,
                        "sector": s.sector,
                        # **呼値に効く**（TOPIX100 は 0.1〜0.5円）。
                        # `autotrader.types.Symbol.is_topix100` が判定に使う
                        "scale_category": s.scale_category,
                    }
                    for s in symbols
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  保存: {UNIVERSE_PATH}（{len(symbols)}銘柄 / 基準日 {as_of}）")


def fetch_universe(source: JQuantsDataSource) -> tuple[Symbol, ...] | None:
    """J-Quants から Layer 1 を通した銘柄一覧を作る。**8〜12分かかる。**"""
    print("  契約範囲内の最新営業日を探す")
    as_of: date | None = None
    for back in range(80, 100):
        probe = date.today() - timedelta(days=back)
        if probe.weekday() >= 5:
            continue
        try:
            if source.get_bars_for_date(probe):
                as_of = probe
                break
        except DataSourceError:
            continue
    if as_of is None:
        print("  NG: 基準日を決められなかった")
        return None
    print(f"  基準日: {as_of}")

    print(f"  日足を{TURNOVER_DAYS}営業日ぶん収集する（12秒間隔。8〜12分）")
    collected: dict[str, list[Bar]] = {}
    found, probe, attempts = 0, as_of, 0
    while found < TURNOVER_DAYS and attempts < TURNOVER_DAYS * 2:
        attempts += 1
        if probe.weekday() >= 5:
            probe -= timedelta(days=1)
            continue
        try:
            day_bars = source.get_bars_for_date(probe)
        except RateLimitError as exc:
            print(f"  中断: レート制限 — {exc}")
            return None
        except DataSourceError:
            probe -= timedelta(days=1)
            continue
        for code, bars in day_bars.items():
            collected.setdefault(code, []).extend(bars)
        found += 1
        print(f"    {probe}: {len(day_bars):>5}銘柄  ({found}/{TURNOVER_DAYS})")
        probe -= timedelta(days=1)

    bars_by_symbol = {
        code: tuple(sorted(bars, key=lambda b: b.timestamp))
        for code, bars in collected.items()
    }
    listed = source.list_symbols(as_of)
    if listed is None:
        print("  NG: 銘柄一覧を取得できなかった")
        return None

    snapshot = build(
        as_of, source, FilterConfig(), bars_by_symbol=bars_by_symbol, symbols=listed
    )
    by_code = {s.code: s for s in listed}
    passed = tuple(by_code[r.symbol] for r in snapshot.passed if r.symbol in by_code)
    print(f"  Layer 1 通過: {len(passed)}銘柄")
    save_universe(as_of, passed)
    return passed


# ---------------------------------------------------------------------------
# 段2・段3: バー（yfinance）
# ---------------------------------------------------------------------------


def fetch_interval(
    source: YahooDataSource,
    store: BarStore,
    symbols: tuple[Symbol, ...],
    interval: str,
    start: date,
    end: date,
) -> tuple[int, list[str]]:
    """指定の足を取得して保存する。

    Returns:
        (保存できた銘柄数, 取れなかった銘柄コード)。

        **取れなかった銘柄を黙って捨てない。** 上場廃止・ティッカー変更で
        欠けることがあり、気づかず母集団から抜けると成績が過大評価になる。
    """
    codes = tuple(s.code for s in symbols)
    print(f"  {interval}: {len(codes)}銘柄 × {start} 〜 {end}")

    try:
        fetched = source.get_bars_batch(codes, interval, start, end)
    except DataSourceError as exc:
        print(f"  NG: {exc}")
        return 0, list(codes)

    saved, missing = 0, []
    for code in codes:
        bars = fetched.get(code, ())
        if not bars:
            missing.append(code)
            continue
        store.write(code, interval, bars)
        store.record_fetch(code, interval, start, end, source.name, len(bars))
        saved += 1

    print(f"    保存 {saved}銘柄 / 取得できず {len(missing)}銘柄")
    if missing:
        print(f"    取得できなかった銘柄: {', '.join(missing[:10])}"
              + (f" ほか{len(missing) - 10}件" if len(missing) > 10 else ""))
    return saved, missing


WALKFORWARD_DAYS = 80
"""ウォークフォワードに要る営業日数の目安（学習60 + 検証20）。"""


def _print_accumulation(store: BarStore, symbols: tuple[Symbol, ...]) -> None:
    """5分足の蓄積状況を出す。

    【なぜ蓄積が要るか】

    **yfinance の5分足は常に58日ぶんしか返さない。** 一度の取得では
    ウォークフォワード（学習60 + 検証20 = 80営業日）に足りない。

    ただし `BarStore.write` は既存データとマージするので、
    **定期実行すれば58日の窓を超えて溜まっていく。**
    週1回まわせば3ヶ月後には100営業日前後になる。

    ここで進捗を出すのは、「あと何日で Phase 4 が回せるか」を
    毎回の実行で確認できるようにするため。
    """
    hr("5. 5分足の蓄積状況")

    # **銘柄ごとに数える。** 和集合で数えると、1銘柄でも80日あれば
    # 「80日（227銘柄）」と出てしまい、**ほとんど空の銘柄を「揃った」と
    # 誤読する**（意思決定ログ54・63で二度踏んだのと同じ種類の欠陥）。
    # 途中から収集対象に加わった銘柄（TOPIX100・意思決定ログ77）があるので、
    # この区別は実際に効く。
    all_days: set[date] = set()
    per_symbol: list[int] = []
    for symbol in symbols:
        symbol_days = {bar.timestamp.date() for bar in store.read(symbol.code, "5m")}
        if not symbol_days:
            continue
        per_symbol.append(len(symbol_days))
        all_days |= symbol_days

    if not per_symbol:
        print("  5分足がまだ無い")
        return

    per_symbol.sort()
    median_days = per_symbol[len(per_symbol) // 2]
    ready = sum(1 for d in per_symbol if d >= WALKFORWARD_DAYS)

    print(f"  期間          : {min(all_days)} 〜 {max(all_days)}")
    print(f"  5分足がある銘柄: {len(per_symbol)}/{len(symbols)}")
    print(f"  必要日数      : {WALKFORWARD_DAYS}日（ウォークフォワード 学習60 + 検証20）")
    print()
    print("  **銘柄ごとの営業日数**（和集合ではない——途中から加えた銘柄があるため）")
    print(f"    中央値: {median_days}日 / 最小: {per_symbol[0]}日 / 最大: {per_symbol[-1]}日")
    print(f"    {WALKFORWARD_DAYS}日以上ある銘柄: {ready}/{len(per_symbol)}")

    print()
    if ready >= DEFAULT_MAX_WATCHLIST:
        print(
            f"  **Phase 4 を回せる**（{WALKFORWARD_DAYS}日以上ある銘柄が "
            f"日次の監視枠 {DEFAULT_MAX_WATCHLIST} を満たす）"
        )
    else:
        remaining = WALKFORWARD_DAYS - median_days
        if remaining > 0:
            print(f"  あと {remaining}営業日ぶん足りない（約{remaining / 5:.0f}週）")
        else:
            print(
                f"  日数は足りているが、{WALKFORWARD_DAYS}日以上ある銘柄が "
                f"{ready}件で監視枠 {DEFAULT_MAX_WATCHLIST} に届かない"
            )
        print()
        print("  **yfinance の5分足は常に58日ぶんしか返さない。**")
        print("  BarStore は既存データとマージするので、週1回この取得を回せば")
        print("  58日の窓を超えて溜まっていく。定期実行を習慣にすること。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="銘柄一覧を J-Quants から取り直す（8〜12分かかる）",
    )
    args = parser.parse_args()

    print("実データのバーを取得する")
    print(f"実行日: {date.today()}")

    hr("1. 銘柄一覧（市場区分・信用区分）")
    symbols = None if args.refresh else load_universe()
    if symbols is None:
        try:
            creds = load_credentials(require_kabus=False)
        except RuntimeError as exc:
            print(f"  NG: {exc}")
            return 1
        print(f"  JQUANTS_API_KEY: {mask(creds.jquants_api_key)}")
        symbols = fetch_universe(JQuantsDataSource(creds.jquants_api_key))
        if symbols is None:
            return 1
    else:
        print(f"  キャッシュを使う: {UNIVERSE_PATH}（{len(symbols)}銘柄）")
        print("  取り直すには --refresh")

    # **TOPIX100 を足す。** universe.json は Layer 1（小型〜中型）だけで、
    # そこでは3手法とも棄却された。唯一 net 正が出た TOPIX100 の5分足が
    # 貯まっていないと、日足の上限見積りを実際の約定モデルで検証できない。
    cheap = load_cheap_universe()
    if cheap:
        known = {s.code for s in symbols}
        extra = tuple(s for s in cheap if s.code not in known)
        symbols = symbols + extra
        print(f"  コストで切り出したユニバース: {len(cheap)}銘柄（うち新規 {len(extra)}）")
        print("  → 中型・小型の高株価帯。**両端だけ試して真ん中を飛ばしていた**")
    else:
        print("  コストで切り出したユニバースが無い")
        print("  （python scripts/measure_cost_landscape.py --refresh で作る）")

    topix100 = load_topix100()
    if topix100:
        known = {s.code for s in symbols}
        extra = tuple(s for s in topix100 if s.code not in known)
        symbols = symbols + extra
        print(f"  TOPIX100 を追加: {len(topix100)}銘柄（うち新規 {len(extra)}）")
        print("  → 唯一 net 正が出た銘柄群。**5分足を貯めないと検証できない**")
    else:
        print("  TOPIX100 の一覧が無い（python scripts/measure_topix100.py --refresh）")
        print("  → **5分足は58日しか遡れないので、取らなかった週は永久に失われる**")

    store = BarStore(DATA_ROOT)
    yahoo = YahooDataSource()
    today = date.today()

    hr("2. 日足（Layer 2 の日次選定に使う）")
    daily_start = today - timedelta(days=DAILY_LOOKBACK_DAYS)
    _, missing_daily = fetch_interval(yahoo, store, symbols, "1d", daily_start, today)

    hr("3. 5分足（約定のシミュレーションに使う）")
    lookback = MAX_LOOKBACK_DAYS["5m"]
    intraday_start = today - timedelta(days=lookback)
    print(f"  yfinance の5分足は{lookback}日ぶんが上限（実測で確定）")
    saved_5m, missing_5m = fetch_interval(
        yahoo, store, symbols, "5m", intraday_start, today
    )

    hr("4. 結果")
    print(f"  日足   : {len(symbols) - len(missing_daily)}/{len(symbols)}銘柄")
    print(f"  5分足  : {saved_5m}/{len(symbols)}銘柄")
    print(f"  保存先 : {DATA_ROOT.resolve()}")

    both = [
        s.code
        for s in symbols
        if s.code not in missing_5m and s.code not in missing_daily
    ]
    print(f"  **両方そろった: {len(both)}銘柄**")

    # **上場廃止と yfinance 側の欠損を切り分ける。**
    # 日足も5分足も取れないなら上場廃止・ティッカー変更を疑う。
    # 日足は取れて5分足だけ取れないなら、その銘柄が存在しないのではなく
    # 分足の配信がないだけ（出来高が薄い時間帯が続くと起きる）。
    gone = sorted(set(missing_5m) & set(missing_daily))
    intraday_only = sorted(set(missing_5m) - set(missing_daily))
    if gone:
        print()
        print(f"  日足も5分足も取れない（上場廃止・ティッカー変更を疑う）: {len(gone)}銘柄")
        print(f"    {', '.join(gone[:10])}")
        print("    → data/universe.json から外すことを検討する")
    if intraday_only:
        print()
        print(f"  日足は取れるが5分足だけ取れない: {len(intraday_only)}銘柄")
        print(f"    {', '.join(intraday_only[:10])}")
        print("    → 上場はしている。yfinance 側の分足欠損。検証からは外れる")

    if not both:
        print()
        print("  NG: 検証できる銘柄がない")
        return 1

    _print_accumulation(store, symbols)

    print()
    print("  次: python scripts/backtest_take.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
