"""日足の高値・安値だけから実効スプレッドを推定する（Corwin-Schultz 2012）。

【なぜ要るのか】

``tick.DEFAULT_SPREAD_TICKS = 2.0`` は **このプロジェクトに残っている
最大の当て推量**（`tick.py` の docstring・`CLAUDE.md`）。Stage A では
板が取れないので直接測れないが、**この1つの数値が全コスト計算を
線形にスケールする**:

- 往復コスト（bps）、`min_atr_yen`、Layer 2 のコストスコア
- バックテストの約定価格、したがって全戦略の net 損益
- `docs/07-go-live-criteria.md` の損益分岐の勝率

実際、これまでの3実験（竹・VWAP乖離単独・ギャップフェード）は
**すべて「gross は小さく正、コストがそれを上回って net 負」**という
同じ形で終わっている（意思決定ログ36・52・56）。この構図が
「優位が本当にない」のか「コストを過大に見積もっている」のかは、
スプレッドの実測値が分からないと切り分けられない。

【Corwin-Schultz とは】

Corwin, S. A., & Schultz, P. (2012). *A Simple Way to Estimate Bid-Ask
Spreads from Daily High and Low Prices.* Journal of Finance, 67(2).

**高値・安値の比だけからスプレッドを推定する。** 発想はこうである:

- 1日の高安の比には「本当の値動き」と「スプレッド」の両方が入る
- 本当の値動きの分散は**期間の長さに比例する**（2日なら2倍）が、
  スプレッドは**日数によらず一定**（毎日1本ぶん入るだけ）
- したがって「1日の高安」と「2日通しの高安」を比べると、
  **日数に比例しない成分＝スプレッド**が分離できる

板情報も約定履歴も要らず、**手元の日足だけで完結する**のが利点。

【実装上の要点: 1ペアずつ解いて平均してはいけない】

素朴に「2日ペアごとにスプレッドを解き、負を0に切り上げてから平均」
とすると、**推定が壊れるほど上振れする**。合成データ（真のスプレッドを
与えて日足を作る）で確認した実測:

    真  5bps → 推定 47.6bps   真 10bps → 推定 50.1bps
    真 20bps → 推定 55.2bps   真 40bps → 推定 66.2bps

真の値がほとんど反映されていない。ノイズで負に振れたぶんだけを
0で止めるので、平均が押し上げられるため。

**β と γ を先に平均し、最後に1回だけ解く**と、ほぼ不偏になる:

    真  5bps → 推定  3.1bps   真 10bps → 推定  7.9bps
    真 20bps → 推定 17.5bps   真 40bps → 推定 36.9bps

この形で実装している（`corwin_schultz`）。

【限界（過信しない）】

- **推定量であって実測ではない。** 板を録れるようになったら
  （楽天 マーケットスピード II RSS 等）そちらで上書きする
- **わずかに下振れする**（上の合成データで 3〜5bps）。
  高安が「連続的に観測される」ことを前提とした推定量なので、
  取引がまばらな銘柄ほど日中のレンジが実際より狭く観測され、
  スプレッドを小さく見積もる。**このプロジェクトの規約
  「検証できないものは保守的な側に倒す」に照らすと、
  推定値は下限として扱う**（コストはこれ以上になりうる）
- 日中に強いトレンドがあると逆に過大推定しやすい
- ストップ高・ストップ安の日は高安が板の状態を表さないので歪む
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from autotrader.types import Bar

__all__ = ["SpreadEstimate", "corwin_schultz", "spread_from_beta_gamma"]

_K = 3.0 - 2.0 * math.sqrt(2.0)
"""原著の定数 ``3 − 2√2``。α の式の分母。"""


def _beta_gamma(previous: Bar, current: Bar) -> tuple[float, float] | None:
    """隣接2日から β（日数に比例する成分）と γ（2日通しの高安）を出す。

    **夜間ギャップを補正する。** 前日の高安と当日の高安が重ならない
    （ギャップで飛んだ）場合、2日通しの高安がギャップぶんだけ広がり、
    スプレッドを過大推定する。原著どおり、重ならない分だけ当日の
    高安を平行移動してから2日通しの高安を取る。

    Returns:
        ``(beta, gamma)``。高値・安値が0以下、または高値 < 安値なら ``None``。
    """
    if previous.low <= 0 or current.low <= 0:
        return None
    if previous.high < previous.low or current.high < current.low:
        return None

    prev_high, prev_low = previous.high, previous.low
    cur_high, cur_low = current.high, current.low

    # 夜間ギャップの補正。重ならないぶんだけ当日を前日側へ寄せる
    if cur_low > prev_high:
        shift = cur_low - prev_high
        cur_high -= shift
        cur_low -= shift
    elif cur_high < prev_low:
        shift = prev_low - cur_high
        cur_high += shift
        cur_low += shift

    if cur_low <= 0:
        return None

    beta = math.log(prev_high / prev_low) ** 2 + math.log(cur_high / cur_low) ** 2
    gamma = math.log(max(prev_high, cur_high) / min(prev_low, cur_low)) ** 2
    return beta, gamma


def spread_from_beta_gamma(beta: float, gamma: float) -> float:
    """β・γ から比例スプレッドを解く（原著の式）。

    **平均した β・γ を渡すこと。** 1ペアずつ解いて平均すると
    大きく上振れする（モジュール docstring 参照）。

    Returns:
        比例スプレッド（例: 0.001 なら 10bps）。**負を切り上げない** —
        負が出ること自体が「その母集団では推定が効いていない」合図。
    """
    alpha = (math.sqrt(2.0 * beta) - math.sqrt(beta)) / _K - math.sqrt(gamma / _K)
    return 2.0 * (math.exp(alpha) - 1.0) / (1.0 + math.exp(alpha))


@dataclass(frozen=True)
class SpreadEstimate:
    """1銘柄ぶんの推定結果。"""

    n_pairs: int
    """推定に使えた隣接日ペアの数。"""
    spread_pct: float
    """比例スプレッド。**負のまま返すことがある**（下記）。

    負なら「この銘柄・この期間では推定が効いていない」という意味で、
    0 とみなすのではなく**推定不能として扱う**のが安全。
    """

    @property
    def spread_bps(self) -> float:
        return self.spread_pct * 10_000.0

    @property
    def usable(self) -> bool:
        """推定値として使えるか。**負なら使わない。**"""
        return self.spread_pct > 0.0


def corwin_schultz(bars: tuple[Bar, ...]) -> SpreadEstimate | None:
    """1銘柄の日足列から比例スプレッドを推定する。

    **β・γ を先に平均してから1回だけ解く。** 1ペアずつ解いて平均すると
    推定が壊れる（モジュール docstring の実測を参照）。

    Args:
        bars: 同一銘柄の日足（順不同でよい。この関数が時刻で並べ替える）。

    Returns:
        推定結果。使えるペアが1つもなければ ``None``。
    """
    ordered = sorted(bars, key=lambda b: b.timestamp)
    pairs = [
        value
        for prev, cur in zip(ordered, ordered[1:], strict=False)
        if (value := _beta_gamma(prev, cur)) is not None
    ]
    if not pairs:
        return None

    mean_beta = sum(b for b, _ in pairs) / len(pairs)
    mean_gamma = sum(g for _, g in pairs) / len(pairs)
    return SpreadEstimate(
        n_pairs=len(pairs),
        spread_pct=spread_from_beta_gamma(mean_beta, mean_gamma),
    )
