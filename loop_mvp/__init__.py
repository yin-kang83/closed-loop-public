"""闭环控制原型：一个内核 + 两个域（财务结账 / 板材下料）。

内核（跨域复用，域无关）：
    loop_mvp.types      契约：Decimal 状态/动作/区间/预测/观测/残差/事实/case
    loop_mvp.audit      哈希链审计（可校验、可定位篡改）
    loop_mvp.ledger     双时态事实账 + 可回放执行日志
    loop_mvp.estimator  假设集 / 残差分解 / 经验误差界 / 假设筛除
    loop_mvp.gate       安全过滤闸门（歧义 REVIEW / 鲁棒可行 / 全违例 BLOCK）
    loop_mvp.memory     经验 case 库（签名/校验/去重/模板复用）
    loop_mvp.loop       闭环编排（含观测越界后的"红字冲销"修正，且修正必须过闸门）
    loop_mvp.rules      版本化规则（按业务日 + 主体范围，缺参拒绝、歧义不猜）

域包（只实现 Domain / Environment 协议）：
    loop_mvp.finance    财务结账：会计恒等式 + 税率版本 + 回执延迟
    loop_mvp.cutting    板材下料：物料守恒 + 切缝假设 + 实测修正
"""

from .types import (  # noqa: F401
    ActionOp,
    CaseRecord,
    FactBundle,
    FactRecord,
    GateDecision,
    Interval,
    Observation,
    Plan,
    PlannedAction,
    Prediction,
    ResidualReport,
    ResidualRow,
    StateSnapshot,
    ZERO,
    canonical_json,
    dec,
    fingerprint,
    q,
)
from .audit import HashChain  # noqa: F401
from .estimator import (  # noqa: F401
    ConformalCalibrator,
    CoverageTracker,
    Hypothesis,
    HypothesisSet,
    HypothesisSieve,
    ResidualAnalyzer,
)
from .gate import SafetyGate  # noqa: F401
from .ledger import TemporalLedger  # noqa: F401
from .memory import CaseMemory, ReuseCheck  # noqa: F401
from .mpc import MpcConfig, MpcPlanner, MpcSelection  # noqa: F401
from .loop import ClosedLoop, LoopConfig, LoopReport, PeriodResult  # noqa: F401

__version__ = "0.1.0"
