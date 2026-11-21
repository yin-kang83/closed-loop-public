# closed-loop-public

> **Auditable Closed-Loop Control on a Deterministic Foundation**  
> Synthetic-data reproducibility package (v1.0.1) for the paper *Auditable Closed-Loop Control on a Deterministic Foundation: An Engineering Practice in Financial and Tax Compliance*.

**Zenodo DOI**: [10.5281/zenodo.24409985](https://doi.org/10.5281/zenodo.24409985)  
**Paper**: 匿名版 PDF in [`paper/论文-匿名版-v1.0.1.pdf`](paper/论文-匿名版-v1.0.1.pdf)

## What this is

- A domain-agnostic deterministic closed-loop kernel: hypothesis sets, fail-closed safety gate, hash-chained audit, bitemporal ledger, case memory, MPC with compliance hard constraint.
- A parameterized **synthetic** financial/tax dataset generator: 8 periods, 79 deterministic validation checks, 3 injected faults.
- Synthetic-scenario tests, demos, and an anonymized manuscript.

## Data boundary

**Synthetic data only.** The package contains no real enterprise accounting books, tax filings, enterprise names, personal information, buyer names/room numbers, bank accounts, or contract numbers. Parameters are author-defined assumptions for “Region A”, not official statistics, and do not correspond to any real project/company.

The original study’s internal composite real data is **not** included and is not redistributed.

## Quick start

```bash
python tools/build_synthetic_dataset.py   # regenerate 8 periods; 79 checks
python -m unittest discover -s tests      # 163 tests
python tools/philosophy_check.py          # ethics/red-line checks
```

## Layout

| Path | Content |
|---|---|
| `loop_mvp/` | deterministic closed-loop kernel |
| `synthetic/` | synthetic dataset + profile + checks |
| `tests/` | synthetic-scenario unit tests |
| `tools/` | generator, demos, philosophy check |
| `docs/` | data card + synthetic reproduction guide |
| `paper/` | anonymized manuscript PDF |

## 第一原则与红线（Ethics / red lines）

**不能成为违法乱纪者的帮凶。** 拒绝清单：逃税/避税、虚开发票、伪造篡改资料、两套账、规避监管、歪曲政策。工具只输出选项与证据，不代签、不自动申报，**不做税务筹划**。涉税输出不构成税务意见，以主管税务机关为准。

## License

- Code: MIT (`LICENSE-MIT`)
- Synthetic data, documentation, figures, anonymized manuscript: CC BY 4.0 (`LICENSE-CC-BY`)

## Citation

See `CITATION.cff`. If you use this package, cite the Zenodo DOI above.
