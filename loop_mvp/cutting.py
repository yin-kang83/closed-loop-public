"""板材下料域包：物料守恒 + 切缝假设 + 实测修正（就是"切木头看出材率"的场景）。

域内动作（每张料单）：上料 → 依次切割 A/A/B… （按需求贪心，用最坏切缝做鲁棒规划）。

不变量（闸门的"物理公理"）：
- CONSERVATION_BREACH：原板 = 剩余 + 已用 + 切缝损耗（不能凭空产生/消失）；
- NEGATIVE_REMAINING：剩余料不得为负；
- YIELD_OUT_OF_RANGE：出材率必须在 0%~100%；
- INVALID_LENGTH / INVALID_BOARD：长度参数必须为正。

假设集来源：设备切缝规格两个版本同时适用（8mm 旧设备 / 12mm 新设备），
观测实测板料后收缩假设；切缝是**转移参数**而不是动作参数，所以动作不歧义，
闸门走"全假设鲁棒可行"路径 —— 与财务域形成对照。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .domain import Domain, Environment
from .estimator import HypothesisSet
from .rules import RuleBook, RuleVersion
from .types import (
    ActionOp,
    FactBundle,
    Observation,
    Plan,
    PlannedAction,
    StateSnapshot,
    Decimal,
    ZERO,
    dec,
    q,
)

# 状态键
BOARD = "原板长度_mm"
REMAIN = "原板剩余_mm"
USED = "已用长度_mm"
KERF = "切缝损耗_mm"
COUNT = "已切件数"
YIELD = "出材率_百分比"

KERF_RULE = "KERF_SPEC"
VERSIONS = (
    RuleVersion(KERF_RULE, "K1", {"kerf_mm": dec("8")}, date(2026, 1, 1), None, "ALL",
                "旧设备规格书：切缝 8mm"),
    RuleVersion(KERF_RULE, "K2", {"kerf_mm": dec("12")}, date(2026, 1, 1), None, "ALL",
                "新设备规格书：切缝 12mm"),
)

PART_ORDER = ("A", "B", "C")


class CuttingDomain(Domain):
    name = "cutting_yield"
    goal = "BOARD_CUT_PLAN"
    observation_keys = (REMAIN, YIELD)
    required_fact_keys = (
        "board_length_mm",
        "board_length_alt_mm",
        "demand_A", "demand_B", "demand_C",
        "length_A", "length_B", "length_C",
    )

    # -- 规则 --------------------------------------------------------------
    def rule_hashes(self, business_date: date) -> Mapping[str, str]:
        return RuleBook(VERSIONS).hashes()

    def resolve_hypotheses(self, business_date: date, entity: Optional[str]) -> HypothesisSet:
        versions = RuleBook(VERSIONS).applicable(KERF_RULE, business_date, entity_scope=None)
        if not versions:
            raise LookupError("切缝规格无适用版本（缺参，拒绝）")
        return HypothesisSet.from_rules(versions)

    # -- 状态与转移 --------------------------------------------------------
    def initial_state(self) -> StateSnapshot:
        return StateSnapshot(
            period="P00",
            values={BOARD: ZERO, REMAIN: ZERO, USED: ZERO, KERF: ZERO, COUNT: ZERO, YIELD: ZERO},
        )

    def transition(self, state: StateSnapshot, action: ActionOp,
                   params: Mapping[str, Any]) -> StateSnapshot:
        if action.name == "LOAD_BOARD":
            board = action.decimal("board_length_mm")
            return state.with_values({
                BOARD: board, REMAIN: board, USED: ZERO, KERF: ZERO, COUNT: ZERO, YIELD: ZERO,
            })
        if action.name.startswith("CUT_"):
            board = state.get(BOARD)
            length = action.decimal("length_mm")
            kerf = dec(params["kerf_mm"])
            remain = q(state.get(REMAIN) - length - kerf)
            used = q(state.get(USED) + length)
            kerf_total = q(state.get(KERF) + kerf)
            count = q(state.get(COUNT) + 1)
            yield_pct = q(used / board * 100) if board > ZERO else ZERO
            return state.with_values({
                BOARD: board, REMAIN: remain, USED: used, KERF: kerf_total,
                COUNT: count, YIELD: yield_pct,
            })
        raise ValueError(f"下料域未知动作: {action.name}")

    def violations(self, state: StateSnapshot, action: ActionOp,
                   params: Mapping[str, Any]) -> Tuple[str, ...]:
        codes = []
        board = state.get(BOARD)
        remain = state.get(REMAIN)
        used = state.get(USED)
        kerf = state.get(KERF)
        yield_pct = state.get(YIELD)
        if board <= ZERO:
            codes.append("INVALID_BOARD")
        if abs(q(remain + used + kerf) - board) > q("0.01"):
            codes.append("CONSERVATION_BREACH")
        if remain < ZERO:
            codes.append("NEGATIVE_REMAINING")
        if used < ZERO or kerf < ZERO:
            codes.append("NEGATIVE_ACCUMULATOR")
        if yield_pct < ZERO or yield_pct > q("100"):
            codes.append("YIELD_OUT_OF_RANGE")
        if action.name.startswith("CUT_") and action.decimal("length_mm") <= ZERO:
            codes.append("INVALID_LENGTH")
        return tuple(codes)

    # -- 规划与经验复用 ----------------------------------------------------
    def plan(self, state: StateSnapshot, facts: Mapping[str, Decimal],
             hypotheses: HypothesisSet, business_date: date) -> Plan:
        return self._build_plan(state, facts, hypotheses, business_date,
                                q(facts["board_length_mm"]))

    def _build_plan(self, state: StateSnapshot, facts: Mapping[str, Decimal],
                    hypotheses: HypothesisSet, business_date: date,
                    board: Decimal) -> Plan:
        worst_kerf = max(hyp.decimal("kerf_mm") for hyp in hypotheses.hypotheses)
        lengths = {part: q(facts[f"length_{part}"]) for part in PART_ORDER}
        demand = {part: int(q(facts[f"demand_{part}"])) for part in PART_ORDER}

        actions: List[PlannedAction] = [
            PlannedAction("LOAD_BOARD", f"上料（{format(board, 'f')}mm）",
                          {"*": ActionOp("LOAD_BOARD", {"board_length_mm": board})})
        ]
        remaining = board
        for part in PART_ORDER:
            for _ in range(demand[part]):
                length = lengths[part]
                if remaining - length - worst_kerf >= ZERO:
                    actions.append(PlannedAction(
                        f"CUT_{part}", f"切 {part}（{format(length, 'f')}mm）",
                        {"*": ActionOp(f"CUT_{part}", {"length_mm": length})},
                    ))
                    remaining = q(remaining - length - worst_kerf)
        return Plan(
            period=state.period,
            goal=self.goal,
            actions=tuple(actions),
            skeleton=tuple(a.name for a in actions),
            rule_hashes=self.rule_hashes(business_date),
            fact_keys=self.required_fact_keys,
        )

    # -- MPC 接口 ----------------------------------------------------------
    def candidate_plans(self, state: StateSnapshot, facts: Mapping[str, Decimal],
                        hypotheses: HypothesisSet, business_date: date) -> Tuple[Plan, ...]:
        """候选 = 可选板材方案（主选板 + 库存备选板）。"""
        boards = [q(facts["board_length_mm"])]
        alt = facts.get("board_length_alt_mm")
        if alt is not None and q(alt) > ZERO and q(alt) != boards[0]:
            boards.append(q(alt))
        return tuple(self._build_plan(state, facts, hypotheses, business_date, board)
                     for board in boards)

    def score_plan(self, plan: Plan, state: StateSnapshot, facts: Mapping[str, Decimal],
                   hypotheses: HypothesisSet) -> Mapping[str, Decimal]:
        """用最坏切缝做确定性推演，返回成本（越小越好）。

        - compliance：违例次数（硬约束，MPC 里永远第一）；
        - fulfillment：未满足件数（交付成本）；
        - waste：剩余料长度（材料成本）。
        """
        worst = {"kerf_mm": max(hyp.decimal("kerf_mm") for hyp in hypotheses.hypotheses)}
        current = state
        compliance = Decimal(0)
        for planned in plan.actions:
            action = planned.variants.get("*")
            if action is None:
                compliance += 1
                continue
            current = self.transition(current, action, worst)
            compliance += len(self.violations(current, action, worst))
        demand_total = sum(int(q(facts[f"demand_{part}"])) for part in PART_ORDER)
        cuts = int(current.get(COUNT))
        unmet = max(0, demand_total - cuts)
        waste = current.get(REMAIN) if current.get(BOARD) > ZERO else q(facts["board_length_mm"])
        return {
            "compliance": compliance,
            "fulfillment": Decimal(unmet),
            "waste": q(waste),
        }

    def instantiate(self, skeleton: Tuple[str, ...], state: StateSnapshot,
                    facts: Mapping[str, Decimal], hypotheses: HypothesisSet,
                    business_date: date) -> Plan:
        plan = self.plan(state, facts, hypotheses, business_date)
        if plan.skeleton != tuple(skeleton):
            raise ValueError(f"需求/料单变化，经验骨架不再适用：{skeleton} != {plan.skeleton}")
        return plan

    def observation_step(self, plan: Plan) -> int:
        return len(plan.actions) - 1 if plan.actions else 0

    def execution_hypothesis(self, hypotheses: HypothesisSet):
        """保守执行：按最大切缝推进估计状态（真实切缝不大于它 → 观测落在保守一侧）。"""
        return max(hypotheses.hypotheses, key=lambda h: h.decimal("kerf_mm"))

    def apply_observation(self, state: StateSnapshot, observation: Observation) -> StateSnapshot:
        """用实测值修正世界状态：剩余料 + 实测板长（= 剩余 + 已用 + 切缝）+ 实测出材率。"""
        if REMAIN not in observation.values:
            return state
        remain = q(observation.values[REMAIN])
        used = state.get(USED)
        kerf = state.get(KERF)
        board = q(remain + used + kerf)
        yield_pct = state.get(YIELD)
        if YIELD in observation.values:
            yield_pct = q(observation.values[YIELD])
        return state.with_values({
            BOARD: board, REMAIN: remain, USED: used, KERF: kerf,
            COUNT: state.get(COUNT), YIELD: yield_pct,
        })


# ---------------------------------------------------------------------------
# 环境：真实板长/真实切缝；文档可能写错板长；回执可延迟
# ---------------------------------------------------------------------------

class CuttingEnvironment(Environment):
    def __init__(self, periods: Tuple[str, ...],
                 data: Mapping[str, Mapping[str, Any]],
                 delays: Optional[Mapping[str, int]] = None) -> None:
        self.periods = tuple(periods)
        self.data = data
        self.delays = dict(delays or {})
        self.reset()

    def reset(self) -> None:
        self.executed: Dict[str, List[Tuple[ActionOp, Mapping[str, Any]]]] = {
            p: [] for p in self.periods
        }

    # -- 抽取 / 执行 --------------------------------------------------------
    def extract_facts(self, period: str, business_date: date) -> FactBundle:
        d = self.data[period]
        facts = {
            "board_length_mm": d.get("extracted_board_length", d["true_board_length"]),
            "board_length_alt_mm": d.get("board_length_alt", "0"),
            "demand_A": d["demand_A"], "demand_B": d["demand_B"], "demand_C": d["demand_C"],
            "length_A": d["length_A"], "length_B": d["length_B"], "length_C": d["length_C"],
        }
        return FactBundle(period=period, business_date=business_date,
                          facts={k: dec(v) for k, v in facts.items()},
                          source="work_order_extract")

    def execute(self, period: str, action: ActionOp, params: Mapping[str, Any]) -> None:
        self.executed[period].append((action, dict(params)))

    # -- 回执 / 确认 --------------------------------------------------------
    def finalize(self, period: str) -> Tuple[Observation, int]:
        return self._observation(period), self.delays.get(period, 0)

    def confirm(self, period: str, hypotheses: HypothesisSet) -> HypothesisSet:
        true_kerf = dec(self.data[period]["true_kerf"])
        kept = tuple(h for h in hypotheses.hypotheses if q(h.decimal("kerf_mm")) == q(true_kerf))
        return HypothesisSet(kept) if kept else hypotheses

    def truth(self, period: str) -> Mapping[str, Decimal]:
        return self._observation(period).values

    def _observation(self, period: str) -> Observation:
        d = self.data[period]
        true_board = dec(d["true_board_length"])
        true_kerf = dec(d["true_kerf"])
        used = ZERO
        kerf_total = ZERO
        cuts = 0
        for action, _params in self.executed[period]:
            if action.name == "LOAD_BOARD":
                if d.get("true_board_from_load"):
                    true_board = action.decimal("board_length_mm")
                continue
            used += action.decimal("length_mm")
            kerf_total += true_kerf
            cuts += 1
        remain = q(true_board - used - kerf_total)
        yield_pct = q(used / true_board * 100) if true_board > ZERO else ZERO
        return Observation(
            period=period,
            values={REMAIN: remain, YIELD: yield_pct},
            source="machine_measure",
            received_at=period,
        )


def default_scenario() -> Tuple[Tuple[str, ...], Dict[str, Dict[str, Any]], Dict[str, int], Dict[str, date]]:
    """演示/评测用的 3 张料单：前两张干净，第三张文档板长写错（2440 → 实际 2400）。"""
    periods = ("P01", "P02", "P03")
    common = {
        "demand_A": 2, "demand_B": 1, "demand_C": 1,
        "length_A": "800", "length_B": "700", "length_C": "500",
        "true_kerf": "12",
    }
    data: Dict[str, Dict[str, Any]] = {
        "P01": {**common, "true_board_length": "2440"},
        "P02": {**common, "true_board_length": "2440"},
        "P03": {**common, "true_board_length": "2400", "extracted_board_length": "2440"},
    }
    dates = {"P01": date(2026, 1, 15), "P02": date(2026, 1, 16), "P03": date(2026, 1, 17)}
    return periods, data, {}, dates


def mpc_scenario() -> Tuple[Tuple[str, ...], Dict[str, Dict[str, Any]], Dict[str, int], Dict[str, date]]:
    """MPC 演示场景：P02/P03 同时有两种库存板（2440 / 3000）。

    - 2440 板：只能切 3 件（欠 1 件），余料 104mm；
    - 3000 板：能切满 4 件，余料 152mm。
    "交付优先"与"材料成本优先"会选出不同的板 → 用来验证 MPC 的代价顺序可配置、不写死。
    """
    periods = ("P01", "P02", "P03")
    base = {
        "demand_A": 2, "demand_B": 1, "demand_C": 1,
        "length_A": "800", "length_B": "700", "length_C": "500",
        "true_kerf": "12", "true_board_length": "2440",
    }
    data: Dict[str, Dict[str, Any]] = {
        "P01": {**base, "board_length_alt": "0"},
        "P02": {**base, "board_length_alt": "3000", "true_board_from_load": True},
        "P03": {**base, "board_length_alt": "3000", "true_board_from_load": True},
    }
    dates = {"P01": date(2026, 2, 10), "P02": date(2026, 2, 11), "P03": date(2026, 2, 12)}
    return periods, data, {}, dates
