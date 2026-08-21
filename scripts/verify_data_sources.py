#!/usr/bin/env python3
"""データ源の実測スクリプト。**Phase 1 の検証の中核。**

    python scripts/verify_data_sources.py

【なぜこのスクリプトが必要か】

設計時の想定（yfinance は1分足7日/5分足60日、J-Quants Free は12週遅延）は
**公開仕様に基づく未検証の値**。開発環境からは J-Quants・Yahoo の両方に
ネットワークが到達できず、実測できていない。

**推測のまま Phase 2 に進むと、期間の想定違いで詰まる。**
このスクリプトを実行し、出力を見てから次に進むこと。

【出力するもの】

1. J-Quants の疎通確認 — **失敗時は理由をそのまま表示する**
2. **レスポンスの実際の項目名** — V2 は項目名が短縮されており、正確な綴りは
   公開情報からの推定を含む。推測をコードに固定せず実データで確かめる
3. J-Quants Free の実際のデータ終端日（12週遅延の実測）
4. 日付指定の上場銘柄一覧（サバイバーシップ回避の要）
5. yfinance の実際の取得可能期間 — **境界の内側から測る**
6. 期間ズレ（yfinance 5分足と J-Quants 日足の間の穴）
7. **日足の突合検証**（J-Quants と yfinance の乖離率）
   → Light（1,650円/月）への課金判断の材料
8. yfinance のレート制限の挙動

APIキーは**マスクして表示する**。完全な値は出力しない。
"""

from __future__ import annotations

import sys
import traceback
from datetime import date, timedelta

from autotrader.config import load_credentials, mask
from autotrader.data.base import DataSourceError
from autotrader.data.jquants import (
    ENDPOINT_DAILY_BARS,
    ENDPOINT_MASTER,
    FREE_PLAN_DELAY_DAYS,
    FREE_PLAN_REQUESTS_PER_MINUTE,
    JQuantsDataSource,
)
from autotrader.data.yahoo import MAX_LOOKBACK_DAYS, YahooDataSource

PROBE_SYMBOLS = ("7203", "8306", "9432")
"""実測に使う銘柄。流動性が高く上場廃止リスクの低い大型株を選ぶ。

トヨタ・三菱UFJ・NTT。データが取れないなら銘柄側ではなくAPI側の問題と判断できる。
"""


def hr(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def check_credentials() -> str | None:
    hr("1. 認証情報")
    try:
        creds = load_credentials(require_kabus=False)
    except RuntimeError as exc:
        print(f"  NG: {exc}")
        return None
    print(f"  JQUANTS_API_KEY: {mask(creds.jquants_api_key)}")
    return creds.jquants_api_key


def probe_jquants(api_key: str) -> None:
    """疎通を単独で確認する。**失敗理由をそのまま表示する。**

    以前は全ての失敗を同一視していたため、401（認証）と 404（パス誤り）と
    空応答（遅延）が区別できず、誤った案内を出した。実際の原因は V1 の旧パスだった。
    """
    hr("2. J-Quants — 疎通確認")
    source = JQuantsDataSource(api_key)
    probe = date.today() - timedelta(days=FREE_PLAN_DELAY_DAYS + 7)

    print(f"  エンドポイント: {ENDPOINT_DAILY_BARS}")
    print(f"  試行日: {probe}")
    try:
        payload = source.get_raw(ENDPOINT_DAILY_BARS, {"date": probe.isoformat()})
    except DataSourceError as exc:
        print(f"  NG: {exc}")
        return

    print(f"  OK: 応答を受信（トップレベルのキー: {sorted(payload.keys())}）")
    _dump_record_keys(payload, "日足")


def _dump_record_keys(payload: dict[str, object], label: str) -> None:
    """レスポンスの**実際の項目名**を表示する。

    V2 は項目名が短縮されており（Open→O、Close→C など）、正確な綴りは
    公開情報からの推定を含む。**推測をコードに固定せず、実データで確かめる。**
    ここで出た項目名で data/jquants.py の _FIELD_CANDIDATES を整理する。
    """
    for key, value in payload.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            print(f"  {label}のレコード項目名（{key}）:")
            print(f"    {sorted(value[0].keys())}")
            print("  先頭レコードの中身:")
            for k, v in sorted(value[0].items()):
                print(f"    {k:>20} = {v!r}")
            return
    print(f"  ※ {label}のレコードが空だった（配列にデータなし）")


def check_jquants(api_key: str) -> date | None:
    """Free プランの実際のデータ終端日を調べる。

    Returns:
        取得できた最新の営業日。失敗なら None。
    """
    hr("3. J-Quants — 実際のデータ終端日")
    source = JQuantsDataSource(api_key, requests_per_minute=FREE_PLAN_REQUESTS_PER_MINUTE)

    expected = date.today() - timedelta(days=FREE_PLAN_DELAY_DAYS)
    print(f"  想定終端日（12週=84日遅延）: {expected}")
    print("  実測中（直近から遡って最初に取れる営業日を探す）...")

    first_error: str | None = None
    for back in range(FREE_PLAN_DELAY_DAYS - 10, FREE_PLAN_DELAY_DAYS + 25):
        probe = date.today() - timedelta(days=back)
        try:
            bars = source.get_bars_for_date(probe)
        except DataSourceError as exc:
            if first_error is None:
                first_error = f"{probe}: {exc}"
            continue
        if bars:
            print(f"  OK: {probe} のデータを取得（{len(bars)}銘柄）")
            delay = (date.today() - probe).days
            print(f"  実測の遅延: {delay}日（想定 {FREE_PLAN_DELAY_DAYS}日）")
            if abs(delay - FREE_PLAN_DELAY_DAYS) > 7:
                print("  ※ 想定と1週間以上ずれている。FREE_PLAN_DELAY_DAYS の見直しを検討")
            return probe

    print("  NG: どの日付でもデータを取得できなかった")
    if first_error:
        print(f"  最初の失敗理由: {first_error}")
    return None


def check_jquants_symbols(api_key: str, as_of: date) -> None:
    hr("4. J-Quants — 日付指定の上場銘柄一覧（サバイバーシップ回避の要）")
    source = JQuantsDataSource(api_key)
    try:
        raw = source.get_raw(ENDPOINT_MASTER, {"date": as_of.isoformat()})
        _dump_record_keys(raw, "銘柄一覧")
        symbols = source.list_symbols(as_of)
    except DataSourceError as exc:
        print(f"  NG: {exc}")
        return
    if not symbols:
        print("  NG: 空だった")
        return
    print(f"  OK: {as_of} 時点で {len(symbols)}銘柄")
    print(f"  例: {', '.join(f'{s.code}({s.name})' for s in symbols[:3])}")


def check_yahoo_lookback() -> dict[str, int]:
    """yfinance が実際に何日遡れるかを足ごとに実測する。"""
    hr("5. yfinance — 実際の取得可能期間")
    source = YahooDataSource()
    measured: dict[str, int] = {}

    print("  （境界の内側から試し、成功した最大値と失敗した最小値で上限を挟み込む）")
    print()

    for interval, expected in sorted(MAX_LOOKBACK_DAYS.items(), key=lambda kv: kv[1]):
        if interval not in ("1m", "5m", "60m"):
            continue  # 代表的な足だけ測る（リクエスト数を抑える）
        actual, failed_at = _measure_lookback(source, interval, expected)
        measured[interval] = actual
        flag = "OK" if actual >= expected - 2 else "※想定より短い"
        failure = f" / {failed_at}日は失敗" if failed_at is not None else ""
        print(
            f"  {interval:>4}: 成功 {actual:>3}日{failure}"
            f"  / 想定 {expected:>3}日  {flag}"
        )

    # 日足は制限がないはず
    actual_daily, _ = _measure_lookback(source, "1d", 400)
    print(f"  {'1d':>4}: 成功 {actual_daily:>3}日以上（制限なしのはず）")

    print()
    print("  ※ 想定と食い違う場合は data/yahoo.py の MAX_LOOKBACK_DAYS を実測値に修正する")
    return measured


def _try_fetch(source: YahooDataSource, interval: str, days: int) -> bool:
    """指定日数ぶん遡って取得できるか試す。"""
    today = date.today()
    try:
        bars = source.get_bars_batch(
            (PROBE_SYMBOLS[0],), interval, today - timedelta(days=days), today
        )
    except DataSourceError:
        return False
    return bool(bars.get(PROBE_SYMBOLS[0]))


def _measure_lookback(
    source: YahooDataSource, interval: str, hint: int
) -> tuple[int, int | None]:
    """遡れる日数を実測する。

    **境界ちょうどを試さない。** 以前は上限そのもの（60日など）から試していたため、
    Yahoo 側の「直近60日以内」の判定に触れて弾かれ、実測値が常に0になっていた。

        5m data not available ... The requested range must be within the last 60 days.

    内側から始めて、成功した最大値と失敗した最小値の**両方**を返す。
    こうすれば上限を挟み込める（「60日は成功、62日は失敗」）。

    Returns:
        (成功した最大日数, 失敗した最小日数)。後者は見つからなければ ``None``。
    """
    # 境界の内側から、少しずつ外へ
    candidates = sorted({max(1, hint - 2), hint // 2, hint // 4, 5, hint, hint + 2})

    best = 0
    smallest_failure: int | None = None
    for days in candidates:
        if _try_fetch(source, interval, days):
            best = max(best, days)
        elif smallest_failure is None or days < smallest_failure:
            smallest_failure = days

    return best, smallest_failure


def check_gap(jquants_end: date | None, yahoo_5m_days: int) -> None:
    """5分足と日足の期間が重なるかを実測値から判定する。"""
    hr("6. 期間ズレ — 5分足と日足が重なるか")

    if jquants_end is None:
        print("  判定不能（J-Quants のデータ終端日が取れていない）")
        return

    yahoo_5m_start = date.today() - timedelta(days=yahoo_5m_days or 60)
    print(f"  J-Quants 日足の終端 : {jquants_end}")
    print(f"  yfinance 5分足の始端: {yahoo_5m_start}")

    gap = (yahoo_5m_start - jquants_end).days
    if gap > 0:
        print()
        print(f"  → 重ならない。間に {gap}日の穴がある")
        print("     竹の検証には5分足と同じ期間の日足が要る（ATR%・売買代金・出来高比）")
        print("     この期間の日足は yfinance で補完する必要がある")
    else:
        print()
        print(f"  → {-gap}日ぶん重なっている。J-Quants の日足だけで賄える")


def compare_daily(api_key: str, jquants_end: date | None) -> None:
    """J-Quants と yfinance の日足を突き合わせ、乖離率を出す。

    **Light（1,650円/月）へ課金すべきかの判断材料。**
    yfinance の日足が信用できるなら Free のままでよい。
    """
    hr("7. 日足の突合検証 — J-Quants vs yfinance（課金判断の材料）")

    if jquants_end is None:
        print("  判定不能（J-Quants のデータ終端日が取れていない）")
        return

    end = jquants_end
    start = end - timedelta(days=180)
    jq = JQuantsDataSource(api_key)
    yh = YahooDataSource()

    for symbol in PROBE_SYMBOLS:
        try:
            jq_bars = jq.get_bars(symbol, "1d", start, end)
            yh_bars = yh.get_bars(symbol, "1d", start, end)
        except DataSourceError as exc:
            print(f"  {symbol}: 取得失敗 — {exc}")
            continue

        jq_map = {b.timestamp.date(): b.close for b in jq_bars}
        yh_map = {b.timestamp.date(): b.close for b in yh_bars}
        common = sorted(set(jq_map) & set(yh_map))
        if not common:
            print(f"  {symbol}: 共通する日付がない（比較不能）")
            continue

        diffs = [abs(jq_map[d] - yh_map[d]) / jq_map[d] for d in common if jq_map[d]]
        diffs.sort()
        worst = max(diffs)
        median = diffs[len(diffs) // 2]
        over_1pct = sum(1 for d in diffs if d > 0.01)

        print(
            f"  {symbol}: 共通{len(common)}日  "
            f"中央値 {median * 100:.3f}%  最大 {worst * 100:.3f}%  "
            f"1%超 {over_1pct}日"
        )

    print()
    print("  【判断の目安】")
    print("   中央値が 0.01% 未満・1%超がゼロ → yfinance の日足は信用できる。Free のままでよい")
    print("   1%超が散発する → 分割調整のズレの可能性。Light（1,650円/月）を検討")


def check_yahoo_rate_limit() -> None:
    """バッチ取得でブロックされないかを確認する。"""
    hr("8. yfinance — レート制限の挙動")
    source = YahooDataSource()
    today = date.today()
    try:
        bars = source.get_bars_batch(
            PROBE_SYMBOLS, "1d", today - timedelta(days=30), today
        )
    except DataSourceError as exc:
        print(f"  NG: {exc}")
        print("  → レート制限にかかっている可能性。時間をおいて再実行すること")
        return

    got = len(bars)
    print(f"  {len(PROBE_SYMBOLS)}銘柄中 {got}銘柄を取得")
    if got < len(PROBE_SYMBOLS):
        missing = [s for s in PROBE_SYMBOLS if s not in bars]
        print(f"  ※ 取得できなかった銘柄: {', '.join(missing)}")
        print("     大型株で取れないのはブロックの可能性が高い")
    else:
        print("  OK: 全銘柄取得できた")


def main() -> int:
    print("データ源の実測 — Phase 1 の検証")
    print(f"実行日: {date.today()}")

    api_key = check_credentials()
    if api_key is None:
        print("\n認証情報がないため中断する。.env を設定すること")
        return 1

    try:
        probe_jquants(api_key)
    except Exception:  # noqa: BLE001 - 実測スクリプトなので握らず全部見せる
        traceback.print_exc()

    jquants_end: date | None = None
    try:
        jquants_end = check_jquants(api_key)
        if jquants_end is not None:
            check_jquants_symbols(api_key, jquants_end)
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    measured: dict[str, int] = {}
    try:
        measured = check_yahoo_lookback()
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    try:
        check_gap(jquants_end, measured.get("5m", 0))
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    if api_key and jquants_end:
        try:
            compare_daily(api_key, jquants_end)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    try:
        check_yahoo_rate_limit()
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    hr("完了")
    print("  この出力を確認してから Phase 2（ユニバース構築）に進むこと。")
    print("  想定と食い違う値があれば、対応する定数を実測値に修正する。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
