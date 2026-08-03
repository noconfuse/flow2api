"""根据 prompt 文档生成批量任务 Excel。

6 个 OMNI PROMPT 分组，每组一条任务。图片列引用 tests/assets/batch_web_assets 下的素材：
- @image_1 -> image1.png  (主角肌肉黑人男性)
- @image_2 -> image2.png  (沉默瘦弱黑人男性)
- @image_3 -> image3.png  (产品：可调节握力训练器)
- @image_4 -> 留空，由用户手动插入上一组的 clip_XX_final.png（asset 中没有）
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Optional

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation


REPO_ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = REPO_ROOT / "tests" / "assets" / "batch_web_assets"
OUTPUT_PATH = REPO_ROOT / "tests" / "omni_prompts_batch.xlsx"

EXPECTED_COLUMNS = [
    "task_type",
    "model",
    "duration",
    "aspect_ratio",
    "prompt",
    "image_1",
    "image_2",
    "image_3",
    "image_4",
    "image_5",
    "video_1",
    "video_2",
    "image_source",
]

TASKS_SHEET_NAME = "Tasks"
OPTIONS_SHEET_NAME = "Options"


def _model_label() -> str:
    """读取后端 _get_batch_option_catalog 中第一个 t2v 模型的中文展示名。"""
    sys.path.insert(0, str(REPO_ROOT))
    from src.api.batch import _get_batch_option_catalog  # noqa: WPS433

    for item in _get_batch_option_catalog():
        if not isinstance(item, dict):
            continue
        if "t2v" in (item.get("task_types") or []):
            label = str(item.get("label") or "").strip()
            if label:
                return label
    return "Veo 3.1 Fast"


def _build_prompt_text(
    prompt_intro: str,
    dialogue_lines: list[str],
    negative_lines: list[str],
) -> str:
    parts = [prompt_intro.strip(), "", "## Spoken script"]
    parts.extend(f'- "{line}"' for line in dialogue_lines)
    parts.extend(["", "## Negative constraints"])
    parts.extend(f"- {line}" for line in negative_lines)
    return "\n".join(parts).strip()


def _embed_image(ws, cell_ref: str, image_path: Path) -> None:
    if not image_path.exists():
        return
    img = XLImage(str(image_path))
    # 控制单元格内图片尺寸，便于阅读
    img.width = 140
    img.height = 200
    img.anchor = cell_ref
    ws.add_image(img)


def _attach_image_source(ws, row: int, clip_name: str, upstream_excel_row: int) -> None:
    """image_source 列写入 row:<system_row_key>。

    system_row_key 由后端基于 logical_row_index 生成：第 1 条任务 -> row_0001。
    Excel 数据行 N 对应 logical_row_index = N - 1。
    """
    logical_index = upstream_excel_row - 1
    ws.cell(
        row=row,
        column=13,
        value=f"row:row_{logical_index:04d}",
    )


def _set_row_dimensions(ws) -> None:
    ws.row_dimensions[1].height = 28
    # 每个任务行的图片锚定区域行高
    for row_index in range(2, 8):
        ws.row_dimensions[row_index].height = 140


def build_workbook() -> Workbook:
    workbook = Workbook()
    tasks_sheet = workbook.active
    tasks_sheet.title = TASKS_SHEET_NAME
    options_sheet = workbook.create_sheet(OPTIONS_SHEET_NAME)

    model_label = _model_label()

    # 表头
    for col_index, header in enumerate(EXPECTED_COLUMNS, start=1):
        cell = tasks_sheet.cell(row=1, column=col_index, value=header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(fill_type="solid", fgColor="1E3A8A")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # 列宽
    widths = {
        "A": 12, "B": 24, "C": 10, "D": 12, "E": 56,
        "F": 22, "G": 22, "H": 22, "I": 22, "J": 22, "K": 22, "L": 22, "M": 24,
    }
    for col_name, width in widths.items():
        tasks_sheet.column_dimensions[col_name].width = width

    duration = "10s"
    aspect_ratio = "9:16"

    tasks: list[dict] = [
        {
            "row": 2,
            "duration": "10s",
            "image_source_upstream_row": None,
            "image_source_clip_name": None,
            "prompt_intro": (
                "OMNI PROMPT 1 — Random-Squeeze Comparison Hook.\n"
                "Create a 10-second vertical 9:16 realistic UGC video.\n"
                "@image_1 = muscular main speaker (deep brown skin, compact short locs, shirtless, "
                "black tapered sweatpants, white sneakers, black chest lavalier).\n"
                "@image_2 = slim silent man (high textured afro, fitted black compression shirt, "
                "silver cross necklace, black sweatpants with white drawstrings, white sneakers).\n"
                "Same narrow sunny high-rise balcony throughout; main foreground screen-right, "
                "slim man screen-left; no product visible; raw handheld vertical iPhone, 24-28mm lens."
            ),
            "dialogue": [
                "This is what squeezing the same basic gripper will do.",
                "And this is what it won't do.",
                "This is what squeezing the same basic gripper will do.",
                "And this is what it won't do.",
            ],
            "negative": [
                "no product, captions, text overlays, arrows, or graphics.",
                "no identity drift, wardrobe drift, or scene relocation.",
                "no camera-axis flip; only the explicitly timed hard cuts.",
                "no finger travel after a shot begins; mid-shot target switching forbidden.",
            ],
        },
        {
            "row": 3,
            "duration": "6s",
            "image_source_upstream_row": 2,
            "image_source_clip_name": "clip_01_final.png",
            "prompt_intro": (
                "OMNI PROMPT 2 — Random-Squeeze Warning and Question.\n"
                "Create a 6-second vertical 9:16 realistic UGC video.\n"
                "@image_1 = muscular main speaker identity.\n"
                "@image_2 = slim silent man identity.\n"
                "@image_3 = exact compact dark blue-charcoal adjustable grip trainer "
                "(orange horizontal spring, ridged adjustment knob, black knurled wheel, "
                "curved finger handle, diagonal linkages, triangular cutouts, mechanical counter).\n"
                "@image_source = the previously accepted clip_01_final.png (empty-handed two-shot opener) "
                "serves as the continuity anchor: the first frame must match the open pose, balcony framing, "
                "and relative positions of both characters from that reference frame.\n"
                "Same sunny balcony; main screen-right/center; silent man screen-left/back."
            ),
            "dialogue": [
                "Only doing random squeezes is where your grip training falls apart.",
                "Like, why does every set still feel exactly the same?",
            ],
            "negative": [
                "no product before 0.15s; after the cut exactly one same-SKU product.",
                "no generic/recolored gripper, wrong spring, missing knob, missing counter, digital display.",
                "no simultaneous squeezing and adjusting with the same hand.",
                "no identity drift, wardrobe drift, scene relocation, captions, or text overlays.",
            ],
        },
        {
            "row": 4,
            "duration": "10s",
            "image_source_upstream_row": 3,
            "image_source_clip_name": "clip_02_final.png",
            "prompt_intro": (
                "OMNI PROMPT 3 — Fixed-Challenge Explanation.\n"
                "Create a 10-second vertical 9:16 realistic UGC video.\n"
                "@image_1 = muscular main speaker identity.\n"
                "@image_2 = slim silent man identity.\n"
                "@image_3 = exact palm-sized grip trainer SKU.\n"
                "@image_source = the previously accepted clip_02_final.png is the continuity anchor: "
                "the first frame must match the wide two-shot composition, lighting direction, and "
                "product-ready hand position from that reference frame.\n"
                "Same balcony; main holds the product open and stable beside his chest."
            ),
            "dialogue": [
                "That's because with one fixed challenge, you can't adjust the resistance, "
                "and it's easy to lose track of your reps.",
                "When you should be able to change the challenge and count every set.",
            ],
            "negative": [
                "no adjustment or squeezing during the first sentence.",
                "no product redesign, wrong spring, missing knob, missing counter, digital display.",
                "no sentence-spanning cut, captions, text overlays, or wardrobe drift.",
            ],
        },
        {
            "row": 5,
            "duration": "6s",
            "image_source_upstream_row": 4,
            "image_source_clip_name": "clip_03_final.png",
            "prompt_intro": (
                "OMNI PROMPT 4 — Progression Logic.\n"
                "Create a 6-second vertical 9:16 realistic UGC video.\n"
                "@image_1 = muscular main speaker identity.\n"
                "@image_2 = slim silent man identity.\n"
                "@image_3 = exact palm-sized grip trainer SKU.\n"
                "@image_source = the previously accepted clip_03_final.png is the continuity anchor: "
                "the first frame must match the chest-and-hands framing and the product-open pose "
                "from that reference frame.\n"
                "One continuous chest-and-hands view; product open and stable at start."
            ),
            "dialogue": [
                "Because once you can adjust the tension and track your reps, "
                "you can train with an actual progression plan.",
            ],
            "negative": [
                "no turning the knob while squeezing; one adjustment then one squeeze.",
                "no repeated adjustment, repeated squeeze, or action-order reversal.",
                "no second product, digital counter, missing knob, or floating linkage.",
            ],
        },
        {
            "row": 6,
            "duration": "10s",
            "image_source_upstream_row": 5,
            "image_source_clip_name": "clip_04_final.png",
            "prompt_intro": (
                "OMNI PROMPT 5 — Warm-Up, Working-Set, and Counter Demo.\n"
                "Create a 10-second vertical 9:16 realistic UGC video.\n"
                "@image_1 = muscular main speaker identity.\n"
                "@image_2 = slim silent man identity.\n"
                "@image_3 = exact compact grip trainer SKU (orange spring, knob, knurled wheel, "
                "linkages, cutouts, mechanical counter).\n"
                "@image_source = the previously accepted clip_04_final.png is the continuity anchor: "
                "the first frame must match the close-up hands-and-product framing and the "
                "ready-to-adjust hand position from that reference frame.\n"
                "Left hand changes the control; right hand squeezes only after release."
            ),
            "dialogue": [
                "And if you wanna set up different parts of your grip workout, "
                "turn it lighter for warm-ups,",
                "move it up for working sets,",
                "and use the mechanical counter to track your reps.",
            ],
            "negative": [
                "no invented numeric resistance, setting labels, or digital readout.",
                "no second product, changed silhouette, missing orange spring, missing counter.",
                "no turning the control while squeezing; mechanical counter only.",
            ],
        },
        {
            "row": 7,
            "duration": "10s",
            "image_source_upstream_row": 6,
            "image_source_clip_name": "clip_05_final.png",
            "prompt_intro": (
                "OMNI PROMPT 6 — Compact-Use CTA and Caution.\n"
                "Create a 10-second vertical 9:16 realistic UGC video.\n"
                "@image_1 = muscular main speaker identity.\n"
                "@image_2 = slim silent man identity.\n"
                "@image_3 = exact palm-sized grip trainer SKU.\n"
                "@image_source = the previously accepted clip_05_final.png is the continuity anchor: "
                "the first frame must match the wide hero-shot framing, lighting, and product "
                "presentation pose from that reference frame.\n"
                "Same balcony; do not relocate to a desk, office, or home interior — "
                "the spoken 'at your desk or at home' is use-case language only."
            ),
            "dialogue": [
                "If you want a compact, one-hand grip trainer you can use at your desk or at home, "
                "check the product details below.",
                "But before you choose one, check the settings and product details for yourself.",
            ],
            "negative": [
                "no desk, office, or indoor cutaway; spoken use-case language only.",
                "no price, discount, scarcity, guarantee, medical claim, or strength promise.",
                "no second product, altered silhouette, missing orange spring, digital screen, "
                "floating component, or product-hand intersection.",
            ],
        },
    ]

    for task in tasks:
        row = task["row"]
        # A: task_type, B: model, C: duration, D: aspect_ratio, E: prompt
        tasks_sheet.cell(row=row, column=1, value="视频生成")
        tasks_sheet.cell(row=row, column=2, value=model_label)
        tasks_sheet.cell(row=row, column=3, value=task["duration"])
        tasks_sheet.cell(row=row, column=4, value=aspect_ratio)

        prompt_text = _build_prompt_text(
            task["prompt_intro"], task["dialogue"], task["negative"]
        )
        prompt_cell = tasks_sheet.cell(row=row, column=5, value=prompt_text)
        prompt_cell.alignment = Alignment(wrap_text=True, vertical="top")

        # F: image_1 -> main_man.png (prompt 中 @image_1 = 主角肌肉男性)
        # G: image_2 -> silent_man.png (prompt 中 @image_2 = 沉默瘦弱男性)
        # H: image_3 -> product.png (prompt 中 @image_3 = 可调节握力训练器)
        _embed_image(tasks_sheet, f"F{row}", ASSET_DIR / "main_man.png")
        _embed_image(tasks_sheet, f"G{row}", ASSET_DIR / "silent_man.png")
        _embed_image(tasks_sheet, f"H{row}", ASSET_DIR / "product.png")
        # M: image_source - 引用上游视频尾帧
        if task.get("image_source_upstream_row"):
            _attach_image_source(
                tasks_sheet,
                row,
                clip_name=task["image_source_clip_name"],
                upstream_excel_row=task["image_source_upstream_row"],
            )

        # 清空 image_5 / video_1 / video_2（依赖行已写过的除外）
        for col_index in (10, 11, 12):
            tasks_sheet.cell(row=row, column=col_index, value=None)
        # image_source 列（13）由依赖循环单独处理

    # Options sheet: 给 A/B/C/D 列添加数据验证下拉
    option_columns = {
        "A": ["task_type", "视频生成", "视频编辑", "文生图"],
        "B": ["model", model_label],
        "C": ["duration", "4s", "6s", "8s", "10s"],
        "D": ["aspect_ratio", "16:9", "9:16"],
    }
    for column_name, values in option_columns.items():
        for row_index, value in enumerate(values, start=1):
            options_sheet[f"{column_name}{row_index}"] = value
    options_sheet.sheet_state = "hidden"

    validations = {
        "A": f"={OPTIONS_SHEET_NAME}!$A$2:$A$4",
        "B": f"={OPTIONS_SHEET_NAME}!$B$2:$B$3",
        "C": f"={OPTIONS_SHEET_NAME}!$C$2:$C$6",
        "D": f"={OPTIONS_SHEET_NAME}!$D$2:$D$3",
    }
    for column_name, formula in validations.items():
        validation = DataValidation(type="list", formula1=formula, allow_blank=True)
        tasks_sheet.add_data_validation(validation)
        validation.add(f"{column_name}2:{column_name}{1 + len(tasks)}")

    # 说明
    options_sheet["F1"] = "说明"
    options_sheet["F2"] = (
        "image_4 列需要由用户手动插入上一组的 clip_N-1_final.png；"
        "PROMPT 1 没有上一组，image_4 留空。"
    )

    tasks_sheet.freeze_panes = "A2"
    _set_row_dimensions(tasks_sheet)
    return workbook


def main() -> Path:
    workbook = build_workbook()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(OUTPUT_PATH)
    return OUTPUT_PATH


if __name__ == "__main__":
    output = main()
    print(f"Generated: {output}")