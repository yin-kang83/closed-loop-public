"""把论文草稿 Markdown 转成 Word（.docx），便于作者批注与投稿排版。

只做最简转换：标题/段落/列表/引用/代码块/表格；不联网、不引入模板。
用法：``python tools/build_paper_docx.py [--src docs/论文草稿-可审计闭环控制.md] [--out docs/论文草稿-可审计闭环控制.docx]``
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = ROOT / "docs" / "论文草稿-可审计闭环控制.md"
DEFAULT_OUT = ROOT / "docs" / "论文草稿-可审计闭环控制.docx"

BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
IMAGE_RE = re.compile(r"^!\[(.*?)\]\((.+?)\)$")


def add_inline(paragraph, text: str) -> None:
    """支持 **加粗** 的最简行内解析。"""
    position = 0
    for match in BOLD_RE.finditer(text):
        if match.start() > position:
            paragraph.add_run(text[position:match.start()])
        run = paragraph.add_run(match.group(1))
        run.bold = True
        position = match.end()
    if position < len(text):
        paragraph.add_run(text[position:])


def build(src: Path, out: Path) -> dict:
    lines = src.read_text(encoding="utf-8").splitlines()
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "宋体"
    style.font.size = Pt(10.5)

    index = 0
    stats = {"headings": 0, "paragraphs": 0, "tables": 0, "bullets": 0, "code": 0}
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if stripped.startswith("```"):
            index += 1
            block = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                block.append(lines[index])
                index += 1
            index += 1
            paragraph = doc.add_paragraph()
            run = paragraph.add_run("\n".join(block))
            run.font.name = "Consolas"
            run.font.size = Pt(8.5)
            stats["code"] += 1
            continue
        image_match = IMAGE_RE.match(stripped)
        if image_match:
            caption, target = image_match.group(1), image_match.group(2)
            path = Path(target)
            if not path.is_absolute():
                path = (src.parent / path) if (src.parent / path).exists() else (ROOT / path)
            if path.exists():
                doc.add_picture(str(path), width=Inches(6.0))
                doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
                stats["code"] += 0
                stats.setdefault("images", 0)
                stats["images"] += 1
            if caption:
                paragraph = doc.add_paragraph()
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                add_inline(paragraph, caption)
            index += 1
            continue
        if stripped.startswith("|"):
            table_lines = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                table_lines.append(lines[index].strip())
                index += 1
            rows = []
            for row_index, row in enumerate(table_lines):
                cells = [cell.strip() for cell in row.strip("|").split("|")]
                if row_index == 1 and all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells):
                    continue
                rows.append(cells)
            if rows:
                width = max(len(row) for row in rows)
                table = doc.add_table(rows=len(rows), cols=width)
                table.style = "Table Grid"
                for row_index, row in enumerate(rows):
                    for col_index in range(width):
                        cell = table.cell(row_index, col_index)
                        text = row[col_index] if col_index < len(row) else ""
                        add_inline(cell.paragraphs[0], text)
                        if row_index == 0:
                            for run in cell.paragraphs[0].runs:
                                run.bold = True
                stats["tables"] += 1
            continue
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            text = stripped[level:].strip()
            heading = doc.add_heading("", level=min(level, 3))
            add_inline(heading, text)
            stats["headings"] += 1
            index += 1
            continue
        if stripped.startswith("> "):
            paragraph = doc.add_paragraph()
            run = paragraph.add_run(stripped[2:])
            run.italic = True
            stats["paragraphs"] += 1
            index += 1
            continue
        if stripped.startswith("- "):
            paragraph = doc.add_paragraph(style="List Bullet")
            add_inline(paragraph, stripped[2:])
            stats["bullets"] += 1
            index += 1
            continue
        match = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if match:
            paragraph = doc.add_paragraph(style="List Number")
            add_inline(paragraph, match.group(2))
            stats["bullets"] += 1
            index += 1
            continue
        paragraph = doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        add_inline(paragraph, stripped)
        if stripped.startswith("图 ") or stripped.startswith("表 "):
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        stats["paragraphs"] += 1
        index += 1

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out)
    stats["out"] = str(out)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    if not args.src.exists():
        print(f"SKIP: missing {args.src}")
        return 0
    stats = build(args.src, args.out)
    print(f"paper docx: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
