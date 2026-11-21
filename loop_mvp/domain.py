"""域协议（Domain / Environment）与闭环所需的公共类型。

跨域复用的边界就在这里：
- ``Domain`` 提供：初始状态、确定性转移、业务不变量、规划、规则解析、骨架实例化；
- ``Environment`` 提供：文档抽取（可能出错）、真实执行、期末回执（可能延迟）、歧义确认。

闭环内核（gate / loop / memory / estimator / audit / ledger）只依赖这两个协议，
换域 = 换一个 Domain + Environment 实现，内核代码零改动（有测试守着）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any, List, Mapping, Optional, Tuple

from .estimator import HypothesisSet
from .types import (
    ActionOp,
    FactBundle,
    Observation,
    Plan,
    PlannedAction,
    ResidualReport,
    StateSnapshot,
    Decimal,
    q,
)


class Domain(ABC):
    """一个域包：确定性转移 + 不变量 + 规划 + 规则版本。"""

    name: str = "abstract"
    goal: str = "abstract_goal"
    observation_keys: Tuple[str, ...] = ()
    required_fact_keys: Tuple[str, ...] = ()

    # -- 状态与转移 ---------------------------------------------------------
    @abstractmethod
    def initial_state(self) -> StateSnapshot:
        """期初状态（新账期/新料单）。"""

    @abstractmethod
    def transition(
        self,
        state: StateSnapshot,
        action: ActionOp,
        params: Mapping[str, Any],
    ) -> StateSnapshot:
        """确定性转移：f(state, action, params) -> state'。params 来自假设。"""

    @abstractmethod
    def violations(
        self,
        state: StateSnapshot,
        action: ActionOp,
        params: Mapping[str, Any],
    ) -> Tuple[str, ...]:
        """业务不变量检查；返回违例代码（空元组=通过）。"""

    # -- 规划与经验复用 -----------------------------------------------------
    @abstractmethod
    def plan(
        self,
        state: StateSnapshot,
        facts: Mapping[str, Decimal],
        hypotheses: HypothesisSet,
        business_date: date,
    ) -> Plan:
        """从当前状态规划一个账期/料单的动作序列。"""

    @abstractmethod
    def instantiate(
        self,
        skeleton: Tuple[str, ...],
        state: StateSnapshot,
        facts: Mapping[str, Decimal],
        hypotheses: HypothesisSet,
        business_date: date,
    ) -> Plan:
        """按经验骨架重新实例化（参数用当前事实），失败抛 ValueError 触发重规划。"""

    # -- 规则与观测 ---------------------------------------------------------
    @abstractmethod
    def resolve_hypotheses(self, business_date: date, entity: Optional[str]) -> HypothesisSet:
        """解析规则版本 → 假设集；缺参拒绝，歧义则返回多假设（不取最新版）。"""

    @abstractmethod
    def rule_hashes(self, business_date: date) -> Mapping[str, str]:
        """截至业务日已知的规则版本哈希（经验 case 的失效依据）。"""

    @abstractmethod
    def observation_step(self, plan: Plan) -> int:
        """观测回执对应计划中的第几步（通常是期末最后一步）。"""

    def observation_tolerance(self, key: str) -> Decimal:
        """假设筛除的容差：金额到分，长度/百分比到 0.01。"""
        return q(Decimal("0.01"))

    # -- MPC 扩展（默认退化为"单候选=贪心"，不影响既有域） ----------------
    def candidate_plans(self, state: StateSnapshot, facts: Mapping[str, Decimal],
                        hypotheses: HypothesisSet, business_date: date) -> Tuple[Plan, ...]:
        """MPC 候选计划集合；默认只有贪心计划一个。"""
        return (self.plan(state, facts, hypotheses, business_date),)

    def score_plan(self, plan: Plan, state: StateSnapshot, facts: Mapping[str, Decimal],
                   hypotheses: HypothesisSet) -> Mapping[str, Decimal]:
        """MPC 打分（越小越好）；默认无软目标，只保留合规硬约束。"""
        return {}

    def execution_hypothesis(self, hypotheses: HypothesisSet):
        """执行时用于推进"估计状态"的确定假设；默认按 id 序取第一个。

        物理风险域可覆盖为"最保守假设"（如下料域取最大切缝），
        这样真实值落在预测区间的保守一侧，减少无谓越界。
        """
        return sorted(hypotheses.hypotheses, key=lambda h: h.hyp_id)[0]

    def apply_observation(self, state: StateSnapshot, observation: Observation) -> StateSnapshot:
        """观测修正状态：默认不改（账本以记账为准）。

        物理量域（如下料）覆盖实现：用实测值修正世界状态，
        这就是"睁眼一次修正全局"的最小可审计形式。
        """
        return state

    def correction_action(
        self,
        report: ResidualReport,
        observation: Observation,
        state: StateSnapshot,
        hypotheses: HypothesisSet,
    ) -> Optional[ActionOp]:
        """观测越界后是否/如何"改账"（红字冲销、追溯调整）；默认不提供。

        返回 ``ActionOp`` 后仍必须过安全过滤闸门才允许执行 ——
        修正不是特权动作，同样要被不变量否决。
        """
        return None


class Environment(ABC):
    """真实环境接口：抽取、执行、期末回执、歧义确认。"""

    @abstractmethod
    def reset(self) -> None:
        """回到初始环境（评测重跑用）。"""

    @abstractmethod
    def extract_facts(self, period: str, business_date: date) -> FactBundle:
        """文档抽取（可能被注入错误/缺失）。"""

    @abstractmethod
    def execute(self, period: str, action: ActionOp, params: Mapping[str, Any]) -> None:
        """真实执行（可能带隐藏参数：真实税率/真实切缝/真实板长）。"""

    @abstractmethod
    def finalize(self, period: str) -> Tuple[Observation, int]:
        """期末回执：返回 (观测, 延迟几期到达)；延迟由环境决定（故障注入点）。"""

    @abstractmethod
    def confirm(self, period: str, hypotheses: HypothesisSet) -> HypothesisSet:
        """外部确认（申报回执/复核）：把歧义假设集收缩到与真实参数一致的子集。"""

    @abstractmethod
    def truth(self, period: str) -> Mapping[str, Decimal]:
        """真实环境的对照值（仅评测/诊断用，闭环决策不得读取）。"""
