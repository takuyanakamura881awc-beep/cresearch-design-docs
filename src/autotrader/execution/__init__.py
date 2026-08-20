"""注文の執行層。

危険な操作ほど通過点を1本に絞る（docs/05-risk-management.md 原則1）:

- 全発注は ``order.submit`` を通り、そこで ``risk.leverage.enforce`` を必ず呼ぶ
- 全建玉クローズは ``close_all`` に集約し、大引けと緊急停止で共用する
"""
