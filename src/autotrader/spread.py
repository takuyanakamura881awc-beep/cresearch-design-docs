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

【実装上の要点2: 夜間ギャップは終値→始値で正確に除く】

**原著どおりの補正（高安が重ならないときだけ平行移動）では、この
銘柄群では推定不能になる。** 実データ133銘柄すべてが負の推定値を
返した（意思決定ログ58）。

γ は「2日を通した高安」なので夜間に飛んだぶんがそのまま入るが、
β は各日の日中レンジの和なので入らない。日本株は夜間（海外市場・
ニュース）の飛びが大きく、**日中レンジと部分的に重なったまま γ だけを
押し上げる**ため、原著の条件「完全に離れている」に当てはまらず
補正が発動しない。結果 γ ≫ β となり α が負に振り切れる。

合成データでの実測（真のスプレッド20bps・日中ボラ2%）:

    夜間ボラ    原著の補正    終値→始値で補正
      0.0%        18.6bps        18.6bps
      0.5%         2.9bps        18.6bps
      1.0%       -34.5bps        18.6bps
      2.0%      -120.0bps        18.6bps

**原著が高値・安値しか使わないのは、それ以外が無い前提での一般性の
ためであって、あれば使ってよい。** ``前日終値 → 当日始値`` の比で
当日の高安を割り戻し、夜間の飛びを正確に除いている（`_beta_gamma`）。

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

__all__ = [
    "SpreadEstimate",
    "corwin_schultz",
    "corwin_schultz_pooled",
    "spread_from_beta_gamma",
]

_K = 3.0 - 2.0 * math.sqrt(2.0)
"""原著の定数 ``3 − 2√2``。α の式の分母。"""


def _beta_gamma(previous: Bar, current: Bar) -> tuple[float, float] | None:
    """隣接2日から β（日数に比例する成分）と γ（2日通しの高安）を出す。

    【夜間ギャップの除去がこの推定量の生死を分ける】

    γ は「2日を通した高安」なので、**夜間に飛んだぶんがそのまま γ を
    膨らませる**。一方 β は各日の日中レンジの和なので夜間の飛びを含まない。
    補正しないと γ ≫ β となり、α が負に振り切れて**推定不能になる**。

    合成データでの実測（真のスプレッド20bps・日中ボラ2%）:

        夜間ボラ    原著の補正    終値→始値で補正
          0.0%        18.6bps        18.6bps
          0.5%         2.9bps        18.6bps
          1.0%       -34.5bps        18.6bps
          2.0%      -120.0bps        18.6bps

    **原著の補正（高安が重ならないときだけ平行移動）では足りない。**
    実データで133銘柄すべてが負になり、推定不能だった（意思決定ログ58）。
    日本株は夜間（海外市場・ニュース）の飛びが大きく、日中レンジと
    部分的に重なったまま γ だけを押し上げるため、原著の条件
    「完全に離れている」に当てはまらず補正が発動しない。

    **原著が高値・安値しか使わないのは、それ以外が無い前提での一般性の
    ためであって、あれば使ってよい。** 我々は始値・終値も持っているので、
    ``前日終値 → 当日始値`` の比で当日の高安を割り戻し、
    **夜間の飛びを正確に除く**。これにより2日が連続した価格パスになり、
    γ が本来意図した「2日ぶんの値動き＋スプレッド」を表すようになる。

    Returns:
        ``(beta, gamma)``。価格が0以下、または高値 < 安値なら ``None``。
    """
    if previous.low <= 0 or current.low <= 0:
        return None
    if previous.high < previous.low or current.high < current.low:
        return None

    prev_high, prev_low = previous.high, previous.low
    cur_high, cur_low = current.high, current.low

    if previous.close > 0 and current.open > 0:
        # **夜間の飛びを比率で正確に除く。** 当日を前日終値の水準へ揃える
        factor = previous.close / current.open
        cur_high *= factor
        cur_low *= factor
    else:
        # 始値・終値が使えないときだけ、原著の近似（重ならない分だけ平行移動）
        if cur_low > prev_high:
            shift = cur_low - prev_high
            cur_high -= shift
            cur_low -= shift
        elif cur_high < prev_low:
            shift = prev_low - cur_high
            cur_high += shift
            cur_low += shift

    if cur_low <= 0 or cur_high < cur_low:
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


def corwin_schultz_pooled(
    bars_by_symbol: dict[str, tuple[Bar, ...]],
) -> SpreadEstimate | None:
    """複数銘柄をまとめて1つの比例スプレッドに推定する。

    【なぜ銘柄ごとに推定して平均してはいけないのか】

    銘柄ごとの推定値は**半分近くが負になる**（実データで132銘柄中67）。
    そこで「負を捨てて正だけ平均」すると、**ノイズで上振れした銘柄
    だけが生き残る**ので推定が壊れる。ペア単位の切り捨てで踏んだのと
    同じ罠を、銘柄単位で踏むことになる。

    合成データでの実測（銘柄132・各480ペア）:

        真のスプレッド   負の割合   正のみの中央値
             0bps         87%         +4.8bps
             5bps         78%         +6.1bps
            10bps         61%         +5.6bps

    **真が 0bps でも「正のみ」は +4.8bps を返す。** つまり
    「正のみの中央値」は真の値をほとんど反映しない。

    **全銘柄の β・γ をまとめて平均し、最後に1回だけ解けば、
    切り捨てが起きないのでこのバイアスは生じない。** 銘柄ごとの
    ボラティリティの差は残るが、censoring による系統誤差よりはるかに小さい。

    Args:
        bars_by_symbol: 銘柄コード → 日足列。

    Returns:
        全銘柄をまとめた推定。使えるペアが1つもなければ ``None``。
        ``n_pairs`` は全銘柄の合計ペア数。
    """
    total_beta = 0.0
    total_gamma = 0.0
    n_pairs = 0
    for bars in bars_by_symbol.values():
        ordered = sorted(bars, key=lambda b: b.timestamp)
        for prev, cur in zip(ordered, ordered[1:], strict=False):
            value = _beta_gamma(prev, cur)
            if value is None:
                continue
            beta, gamma = value
            total_beta += beta
            total_gamma += gamma
            n_pairs += 1

    if n_pairs == 0:
        return None
    return SpreadEstimate(
        n_pairs=n_pairs,
        spread_pct=spread_from_beta_gamma(total_beta / n_pairs, total_gamma / n_pairs),
    )


def corwin_schultz(bars: tuple[Bar, ...]) -> SpreadEstimate | None:
    """1銘柄の日足列から比例スプレッドを推定する。

    **銘柄ごとの値を集計するときは `corwin_schultz_pooled` を使うこと。**
    個別の推定値は半分近くが負になり、正だけ拾うと大きく上振れする。

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
