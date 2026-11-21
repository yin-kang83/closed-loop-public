"""（已废弃）早期会计专用的契约文件。

本原型已重构为"通用内核 + 域包"结构，契约见 ``loop_mvp/types.py``，
会计科目/凭证/不变量见 ``loop_mvp/finance.py``。本文件保留仅为避免旧引用报错，
不参与任何逻辑。
"""

from .types import (  # noqa: F401
    ActionOp,
    Interval,
    Observation,
    Prediction,
    ResidualReport,
    StateSnapshot,
    ZERO,
    dec,
    q,
)
