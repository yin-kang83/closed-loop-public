# 第一原则护栏检查（机器可验证）

- 生成时间：2026-11-21T10:20:03
- 性质：静态/动态检查，不判断业务正确性；只验证『不做违法乱纪帮凶』的工程约束是否在位。
- 依据：README §0、`legalwiki-kb/docs/哲学基础.md`、`loop_mvp/tax_planning.py` 硬边界。

| 检查 | 结果 | 细节 |
|---|---|---|
| 法定税负不可优化 | PASS | allow=['compliance', 'cash']; rejected=4/4; allowed_pass=True |
| 候选白名单均有官方依据 | PASS | candidates_ok=True; legal_basis_entries=4 |
| 法定税负下界校验 | PASS | below=['TAX_BELOW_STATUTORY']; statutory=[] |
| 涉税输出免责声明 | PASS | checked=4; missing=[] |
| README §0 拒绝清单 | PASS | keywords_missing=[] |
| 禁止优化税负的函数/类名 | PASS | hits=[] |
| MPC 目标不得含 tax | PASS | hits=[] |

结论：全部通过（7/7 PASS）
