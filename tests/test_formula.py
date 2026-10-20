import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from civconvert.formula import SheetEvaluator, col_index, evaluate, format_number, format_value, recalculate

CASES = json.loads((Path(__file__).parent / "formula_cases.json").read_text(encoding="utf-8"))


def _rows(cells: dict[str, str], header_row: int) -> list[dict]:
    rows: list[dict] = []
    for ref, text in cells.items():
        col, row = re.fullmatch(r"([A-Z]+)(\d+)", ref).groups()
        r = int(row) - header_row - 1
        while len(rows) <= r:
            rows.append({"cells": {}})
        rows[r]["cells"][str(col_index(col))] = {"f": text} if text.startswith("=") else {"v": text}
    return rows


ROWS = _rows(CASES["cells"], CASES["header_row"])


@pytest.mark.parametrize("formula, expected", CASES["cases"])
def test_formula(formula, expected):
    sheet = SheetEvaluator(ROWS, CASES["header_row"], CASES["headers"])
    assert format_value(evaluate(formula, sheet.cell)) == expected


@pytest.mark.parametrize("x, expected", CASES["format"])
def test_number_format(x, expected):
    assert format_number(float(x)) == expected


def test_recalculate_stores_values_and_reports_errors():
    rows = [
        {"cells": {"6": {"v": "50"}, "7": {"v": "7.5"}, "8": {"f": "=ROUND(G2*H2,2)", "v": "stale"}}},
        {"cells": {"8": {"f": "=1/0"}}},
    ]
    errors = recalculate(rows)
    assert rows[0]["cells"]["8"]["v"] == "375"
    assert rows[1]["cells"]["8"]["v"] == "#DIV/0!"
    assert errors == [(1, 8, "#DIV/0!")]


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_browser_engine_gives_the_same_results():
    script = Path(__file__).parent / "formula_check.js"
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
