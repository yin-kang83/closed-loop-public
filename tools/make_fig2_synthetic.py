"""生成图 2（合成数据 8 期：资产 = 负债 + 权益，差额均为 0）。数据：synthetic/balance_sheet.csv。"""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "synthetic" / "balance_sheet.csv"
OUT = ROOT / "docs" / "figs" / "fig2_synthetic.png"

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial"]
plt.rcParams["axes.unicode_minus"] = False


def main() -> int:
    rows = list(csv.DictReader(CSV.open(encoding="utf-8-sig")))
    periods = [row["period"] for row in rows]
    assets = [float(row["total_assets"]) / 1e8 for row in rows]
    le = [float(row["liability_plus_equity"]) / 1e8 for row in rows]
    fig, ax = plt.subplots(figsize=(9, 3.6))
    width = 0.36
    positions = range(len(periods))
    ax.bar([p - width / 2 for p in positions], assets, width,
           label="资产总计", color="white", edgecolor="black", hatch="//")
    ax.bar([p + width / 2 for p in positions], le, width,
           label="负债 + 权益", color="black")
    ax.set_xticks(list(positions))
    ax.set_xticklabels(periods)
    ax.set_ylabel("金额（亿元）")
    ax.set_title("图 2  合成数据 8 期：资产 = 负债 + 权益（差额均为 0）", fontsize=10)
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(axis="y", linestyle=":", linewidth=0.6)
    fig.savefig(OUT, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"figure: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
