#!/usr/bin/env python3
"""
Convert markdown reports to styled HTML with Forest Hills color scheme.

This utility generates institutional-quality HTML reports with:
- Color-coded rows based on content analysis (red/orange/green)
- Consistent styling matching Forest Hills reports
- Alert boxes with colored borders
- Professional table formatting

Usage:
    uv run python etl/md_to_styled_html.py <input.md> [--output <output.html>]
"""

from __future__ import annotations

import argparse
import base64
import re
from pathlib import Path
from typing import Any

# Forest Hills color scheme
COLORS = {
    "header_bg": "#1a365d",
    "header_text": "white",
    "critical_bg": "#fed7d7",  # Red - distressed/critical
    "critical_border": "#c53030",
    "critical_text": "#c53030",
    "warning_bg": "#feebc8",  # Orange - caution/watch
    "warning_border": "#dd6b20",
    "warning_text": "#dd6b20",
    "success_bg": "#c6f6d5",  # Green - healthy/positive
    "success_border": "#276749",
    "success_text": "#276749",
    "neutral_bg": "#f7fafc",  # Light gray - alternating rows
    "total_bg": "#e2e8f0",  # Darker gray - totals row
    "border": "#ccc",
}

# Keywords that trigger color coding
CRITICAL_KEYWORDS = [
    "critical",
    "distressed",
    "severe",
    "urgent",
    "immediate",
    "vacancy loss",
    "below market",
    "underperforming",
    "risk",
]
WARNING_KEYWORDS = [
    "caution",
    "watch",
    "monitor",
    "ntv",
    "notice",
    "declining",
    "below",
    "concern",
    "attention",
    "moderate",
]
SUCCESS_KEYWORDS = [
    "healthy",
    "strong",
    "stable",
    "positive",
    "above market",
    "outperforming",
    "minimal",
    "negligible",
    "good",
]

# Occupancy thresholds
OCC_CRITICAL = 80  # Below this = critical (red)
OCC_WARNING = 90  # Below this = warning (orange)
OCC_HEALTHY = 94  # Above this = healthy (green)

# Emoji shortcode to Unicode mapping
EMOJI_MAP = {
    ":warning:": "⚠️",
    ":check:": "✅",
    ":white_check_mark:": "✅",
    ":x:": "❌",
    ":red_circle:": "🔴",
    ":yellow_circle:": "🟡",
    ":green_circle:": "🟢",
    ":star:": "★",
    ":arrow_up:": "↑",
    ":arrow_down:": "↓",
    ":arrow_right:": "→",
}

# HTML style constants
TH_STYLE = "border: 1px solid #666; padding: 8px; text-align: {align};"
TD_STYLE = "border: 1px solid {border}; padding: 8px; text-align: {align};"
ALERT_STYLE = (
    "background-color: {bg}; border-left: 4px solid {border}; " "padding: 12px; margin: 16px 0;"
)
H1_STYLE = (
    "color: {color}; border-bottom: 2px solid {color}; " "padding-bottom: 8px; margin-bottom: 16px;"
)
PRE_STYLE = (
    "background-color: #f7fafc; border: 1px solid {border}; border-radius: 4px; "
    "padding: 16px; margin: 16px 0; font-family: Consolas, Monaco, 'Courier New', "
    "monospace; font-size: 10pt; line-height: 1.4; overflow-x: auto; white-space: pre;"
)
BODY_STYLE = (
    "font-family: Calibri, Arial, sans-serif; font-size: 11pt; line-height: 1.4; "
    "color: #333; max-width: 900px; margin: 0 auto; padding: 20px;"
)


def convert_emoji_shortcodes(text: str) -> str:
    """Convert emoji shortcodes like :warning: to actual Unicode emojis."""
    for shortcode, emoji in EMOJI_MAP.items():
        text = text.replace(shortcode, emoji)
    return text


def classify_row(row_text: str, row_data: dict[str, Any] | None = None) -> str:
    """Classify a row as critical/warning/success/neutral based on content."""
    _ = row_data  # Unused but kept for future extensibility
    text_lower = row_text.lower()

    # Check for occupancy values (explicit keyword match)
    occ_match = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%", row_text)
    if occ_match and any(kw in text_lower for kw in ["occupancy", "occ", "vacant"]):
        occ_val = float(occ_match.group(1))
        if "vacancy" in text_lower or "vacant" in text_lower:
            # For vacancy, invert the logic
            if occ_val > 20:
                return "critical"
            elif occ_val > 10:
                return "warning"
        else:
            if occ_val < OCC_CRITICAL:
                return "critical"
            elif occ_val < OCC_WARNING:
                return "warning"
            elif occ_val >= OCC_HEALTHY:
                return "success"

    # Check for trailing percentage (likely occupancy in floorplan tables)
    # Pattern: row ends with a percentage like "91%" and contains floorplan-like data
    trailing_pct = re.search(r"(\d{1,3})\s*%\s*$", row_text.strip())
    if trailing_pct:
        pct_val = float(trailing_pct.group(1))
        # Only apply if it looks like occupancy (50-100%) and not LTL/concession/pricing context
        # Require >= 50 to avoid false positives on discount %, LTL %, concession %
        concession_keywords = [
            "ltl", "loss", "concession", "discount", "free", "weeks", "months",
            "rebate", "admin", "cash", "gifts", "drops", "none", "premium",
        ]
        if 50 <= pct_val <= 100 and not any(
            kw in text_lower for kw in concession_keywords
        ):
            if pct_val < OCC_CRITICAL:
                return "critical"
            elif pct_val < OCC_WARNING:
                return "warning"
            elif pct_val >= OCC_HEALTHY:
                return "success"

    # Check for LTL (Loss-to-Lease) percentages
    ltl_keywords = ["ltl", "loss-to-lease", "loss to lease"]
    if any(kw in text_lower for kw in ltl_keywords):
        ltl_match = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%", row_text)
        if ltl_match:
            ltl_val = float(ltl_match.group(1))
            if ltl_val > 10:
                return "critical"
            elif ltl_val > 5:
                return "warning"
            elif ltl_val < 2:
                return "success"

    # Check for dollar amounts with "loss" context
    if "loss" in text_lower or "drag" in text_lower:
        if re.search(r"\$[\d,]+", row_text):
            return "critical"

    # Check for explicit keywords
    for kw in CRITICAL_KEYWORDS:
        if kw in text_lower:
            return "critical"
    for kw in WARNING_KEYWORDS:
        if kw in text_lower:
            return "warning"
    for kw in SUCCESS_KEYWORDS:
        if kw in text_lower:
            return "success"

    # Check for "Total" rows
    if text_lower.startswith("total") or text_lower.startswith("**total"):
        return "total"

    return "neutral"


def get_row_style(classification: str, row_index: int) -> str:
    """Get inline style for a table row based on classification."""
    if classification == "critical":
        return f'background-color: {COLORS["critical_bg"]};'
    elif classification == "warning":
        return f'background-color: {COLORS["warning_bg"]};'
    elif classification == "success":
        return f'background-color: {COLORS["success_bg"]};'
    elif classification == "total":
        return f'background-color: {COLORS["total_bg"]}; font-weight: bold;'
    else:
        # Alternating rows
        if row_index % 2 == 0:
            return f'background-color: {COLORS["neutral_bg"]};'
        return ""


def is_separator_row(line: str) -> bool:
    """Check if a table row is a separator/alignment row (e.g., |:---:|---:|)."""
    # Split by | and check if all cells are just dashes, colons, and spaces
    cells = [cell.strip() for cell in line.split("|")]
    cells = [c for c in cells if c]  # Remove empty strings
    if not cells:
        return False
    # Each cell should only contain -, :, and spaces
    return all(re.match(r"^[\-:\s]+$", cell) for cell in cells)


def parse_markdown_table(table_lines: list[str]) -> tuple[list[str], list[list[str]]]:
    """Parse markdown table into headers and rows."""
    headers: list[str] = []
    rows: list[list[str]] = []

    for line in table_lines:
        line = line.strip()
        if not line or is_separator_row(line):
            continue

        # Split by | and clean up
        cells = [cell.strip() for cell in line.split("|")]
        cells = [c for c in cells if c]  # Remove empty strings from edges

        if not headers:
            headers = cells
        else:
            rows.append(cells)

    return headers, rows


def table_to_html(table_lines: list[str], width: str = "100%") -> str:
    """Convert markdown table to styled HTML table."""
    headers, rows = parse_markdown_table(table_lines)

    if not headers:
        return ""

    html = [f'<table style="border-collapse: collapse; width: {width}; margin-bottom: 20px;">']

    # Header row
    header_style = f'background-color: {COLORS["header_bg"]}; color: {COLORS["header_text"]};'
    html.append(f'  <tr style="{header_style}">')
    align_keywords = ["value", "rent", "units", "%", "sf", "occ", "ltl", "amount"]
    for header in headers:
        align = "right" if any(kw in header.lower() for kw in align_keywords) else "left"
        th_style = TH_STYLE.format(align=align)
        html.append(f'    <th style="{th_style}">{header}</th>')
    html.append("  </tr>")

    # Data rows
    for i, row in enumerate(rows):
        row_text = " ".join(row)
        classification = classify_row(row_text)
        row_style = get_row_style(classification, i)

        style_attr = f' style="{row_style}"' if row_style else ""
        html.append(f"  <tr{style_attr}>")

        for j, cell in enumerate(row):
            # Determine alignment
            is_numeric = bool(re.match(r"^[\$\-\d,\.%\+]+$", cell.replace("**", "").strip()))
            align = "right" if is_numeric or j > 0 else "left"

            # Apply bold/color for critical values
            cell_content = cell
            if classification == "critical" and is_numeric:
                cell_content = f'<strong style="color: {COLORS["critical_text"]};">{cell}</strong>'
            elif classification == "success" and is_numeric:
                cell_content = f'<strong style="color: {COLORS["success_text"]};">{cell}</strong>'
            elif "**" in cell:
                cell_content = cell.replace("**", "")
                cell_content = f"<strong>{cell_content}</strong>"

            td_style = TD_STYLE.format(border=COLORS["border"], align=align)
            html.append(f'    <td style="{td_style}">{cell_content}</td>')

        html.append("  </tr>")

    html.append("</table>")
    return "\n".join(html)


def process_alert_box(text: str) -> str:
    """Convert markdown alert/callout to styled HTML box."""
    text_lower = text.lower()

    # Determine color based on content
    if any(kw in text_lower for kw in CRITICAL_KEYWORDS):
        bg = COLORS["critical_bg"]
        border = COLORS["critical_border"]
    elif any(kw in text_lower for kw in WARNING_KEYWORDS):
        bg = COLORS["warning_bg"]
        border = COLORS["warning_border"]
    elif any(kw in text_lower for kw in SUCCESS_KEYWORDS):
        bg = COLORS["success_bg"]
        border = COLORS["success_border"]
    else:
        bg = COLORS["neutral_bg"]
        border = COLORS["border"]

    # Convert markdown bold
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)

    style = ALERT_STYLE.format(bg=bg, border=border)
    return f'<p style="{style}">{text}</p>'


def embed_image(image_path: str, base_dir: Path, alt_text: str = "") -> str:
    """Convert a markdown image reference to an embedded base64 <img> tag."""
    img_path = base_dir / image_path
    if not img_path.exists():
        return f'<p style="color: #999;">[Image not found: {image_path}]</p>'

    suffix = img_path.suffix.lower()
    mime_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp"}
    mime = mime_types.get(suffix, "image/png")

    data = base64.b64encode(img_path.read_bytes()).decode("ascii")
    return (
        f'<div style="margin: 16px 0; text-align: center;">'
        f'<img src="data:{mime};base64,{data}" alt="{alt_text}" '
        f'style="max-width: 100%; height: auto; border: 1px solid #ccc; border-radius: 4px;">'
        f"</div>"
    )


def md_to_html(md_content: str, base_dir: Path | None = None) -> str:
    """Convert full markdown document to styled HTML."""
    # Convert emoji shortcodes first
    md_content = convert_emoji_shortcodes(md_content)

    lines = md_content.split("\n")
    html_parts: list[str] = []

    # HTML header
    title_match = re.search(r"^#\s+(.+)$", md_content, re.MULTILINE)
    title = title_match.group(1) if title_match else "Report"

    html_parts.append(f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>{title}</title>
</head>
<body style="{BODY_STYLE}">
""")

    i = 0
    in_table = False
    in_code_block = False
    table_lines: list[str] = []
    code_lines: list[str] = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Handle code blocks (``` markers)
        if stripped.startswith("```"):
            if in_code_block:
                # End of code block - render as styled pre
                code_content = "\n".join(code_lines)
                pre_style = PRE_STYLE.format(border=COLORS["border"])
                html_parts.append(f'<pre style="{pre_style}">{code_content}</pre>')
                in_code_block = False
                code_lines = []
            else:
                # Start of code block
                in_code_block = True
                code_lines = []
            i += 1
            continue

        if in_code_block:
            code_lines.append(line.rstrip())
            i += 1
            continue

        # Handle tables
        if stripped.startswith("|"):
            if not in_table:
                in_table = True
                table_lines = []
            table_lines.append(stripped)
            i += 1
            continue
        elif in_table:
            # End of table
            html_parts.append(table_to_html(table_lines))
            in_table = False
            table_lines = []

        # H1 headers
        if stripped.startswith("# "):
            text = stripped[2:]
            h1_style = H1_STYLE.format(color=COLORS["header_bg"])
            html_parts.append(f'<h1 style="{h1_style}">{text}</h1>')

        # H2 headers
        elif stripped.startswith("## "):
            text = stripped[3:]
            html_parts.append(f'<h2 style="color: {COLORS["header_bg"]};">{text}</h2>')

        # H3 headers
        elif stripped.startswith("### "):
            text = stripped[4:]
            html_parts.append(f'<h3 style="color: #2c5282;">{text}</h3>')

        # H4 headers
        elif stripped.startswith("#### "):
            text = stripped[5:]
            html_parts.append(f'<h4 style="color: #2c5282;">{text}</h4>')

        # Horizontal rules
        elif stripped == "---" or stripped == "***":
            html_parts.append(
                '<hr style="border: none; border-top: 1px solid #ccc; margin: 20px 0;">'
            )

        # Images: ![alt text](path)
        elif re.match(r"^!\[([^\]]*)\]\(([^)]+)\)$", stripped):
            img_match = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)$", stripped)
            if img_match and base_dir:
                alt_text = img_match.group(1)
                img_src = img_match.group(2)
                html_parts.append(embed_image(img_src, base_dir, alt_text))

        # Alert boxes (paragraphs starting with **Bottom Line:** etc.)
        elif (
            stripped.startswith("**Bottom Line:**")
            or stripped.startswith("**Assessment:**")
            or stripped.startswith("**Recommendation:**")
        ):
            html_parts.append(process_alert_box(stripped))

        # Regular paragraphs with bold text
        elif stripped and not stripped.startswith("-"):
            # Convert markdown bold
            text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", stripped)
            html_parts.append(f"<p>{text}</p>")

        # List items
        elif stripped.startswith("- "):
            # Collect list items
            list_items = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                item = lines[i].strip()[2:]
                item = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", item)
                list_items.append(f"<li>{item}</li>")
                i += 1
            html_parts.append(
                '<ul style="margin: 10px 0; padding-left: 20px;">' + "\n".join(list_items) + "</ul>"
            )
            continue

        # Numbered list items
        elif re.match(r"^\d+\.\s", stripped):
            list_items = []
            while i < len(lines) and re.match(r"^\d+\.\s", lines[i].strip()):
                item = re.sub(r"^\d+\.\s", "", lines[i].strip())
                item = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", item)
                list_items.append(f"<li>{item}</li>")
                i += 1
            html_parts.append(
                '<ol style="margin: 10px 0; padding-left: 20px;">' + "\n".join(list_items) + "</ol>"
            )
            continue

        i += 1

    # Handle any remaining table
    if in_table and table_lines:
        html_parts.append(table_to_html(table_lines))

    # Handle any unclosed code block
    if in_code_block and code_lines:
        code_content = "\n".join(code_lines)
        pre_style = PRE_STYLE.format(border=COLORS["border"])
        html_parts.append(f'<pre style="{pre_style}">{code_content}</pre>')

    # Close HTML
    html_parts.append("""
</body>
</html>""")

    return "\n".join(html_parts)


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Convert markdown reports to styled HTML with Forest Hills color scheme"
    )
    parser.add_argument("input", help="Input markdown file")
    parser.add_argument(
        "--output", "-o", help="Output HTML file (default: same name with .html extension)"
    )

    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}")
        return 1

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_suffix(".html")

    # Read and convert
    md_content = input_path.read_text(encoding="utf-8")
    html_content = md_to_html(md_content, base_dir=input_path.parent)

    # Write output
    output_path.write_text(html_content, encoding="utf-8")
    print(f"Generated: {output_path}")
    print(f"  Size: {len(html_content):,} characters")

    return 0


if __name__ == "__main__":
    exit(main())
