"""Ответы «идеальной» модели на задачи lite eval — общая часть тестовых фейков.

Задания генерируются из seed прогона, поэтому фейковый клиент не может держать
захардкоженные ответы: он разбирает промпт и отвечает так, как ответила бы
модель, набравшая полный балл.
"""

from __future__ import annotations

import json
import re

_CODE_UNIT_CONDITIONS = {
    "even": "value % 2 == 0",
    "odd": "value % 2 != 0",
    "positive": "value > 0",
}
_CODE_UNIT_POWERS = {"squares": 2, "cubes": 3}


def perfect_lite_eval_answer(prompt: str) -> str:
    if "Return exactly 4 lines" in prompt:
        return _instruction_following_answer(prompt)
    if "Available tools" in prompt and "create_ticket" in prompt:
        return _tool_call_answer(prompt)
    if "Return only JSON with one key" in prompt:
        return _code_unit_answer(prompt)
    if "A notebook has" in prompt:
        return _symbolic_math_answer(prompt)
    if "Use only the facts above" in prompt:
        return _grounded_qa_answer(prompt)
    return ""


def _instruction_following_answer(prompt: str) -> str:
    first_line = re.search(r"Line 1 must be exactly: (.+)", prompt).group(1).strip()
    marker, repeats = re.search(
        r"Line 2 must contain the word (\w+) exactly (\d+) times",
        prompt,
    ).groups()
    mode = re.search(r'"mode" must be "(\w+)"', prompt).group(1)
    count = int(re.search(r'"count" must be (\d+)', prompt).group(1))
    last_line = re.search(r"Line 4 must be exactly: (.+)", prompt).group(1).strip()
    marker_line = " ".join([marker] * int(repeats))
    json_line = json.dumps({"mode": mode, "count": count})
    return f"{first_line}\n{marker_line}\n{json_line}\n{last_line}"


def _tool_call_answer(prompt: str) -> str:
    priority = re.search(r"Create a (\w+)-priority", prompt).group(1)
    title = re.search(r'titled "([^"]+)"', prompt).group(1)
    assignee = re.search(r"assigned to (\w+)", prompt).group(1)
    due_date = re.search(r"due (\d{4}-\d{2}-\d{2})", prompt).group(1)
    return json.dumps({
        "tool": "create_ticket",
        "arguments": {
            "title": title,
            "priority": priority,
            "assignee": assignee,
            "due_date": due_date,
        },
    })


def _code_unit_answer(prompt: str) -> str:
    function_name = re.search(r"def (\w+)\(nums", prompt).group(1)
    _, filter_name, transform_name = function_name.split("_")
    condition = _CODE_UNIT_CONDITIONS[filter_name]
    power = _CODE_UNIT_POWERS[transform_name]
    code = (
        f"def {function_name}(nums: list[int]) -> int:\n"
        "    total = 0\n"
        "    for value in nums:\n"
        f"        if {condition}:\n"
        f"            total += value ** {power}\n"
        "    return total\n"
    )
    return json.dumps({"code": code})


def _symbolic_math_answer(prompt: str) -> str:
    numbers = [int(value) for value in re.findall(r"\d+", prompt)]
    total_pages, weekday_pages, weeks, weekend_pages = numbers[:4]
    return str(total_pages - weeks * 5 * weekday_pages - weeks * 2 * weekend_pages)


def _grounded_qa_answer(prompt: str) -> str:
    asked_code = re.search(
        r"Line 1: how many pallets did warehouse (\S+) ship",
        prompt,
    ).group(1)
    pallets = re.search(
        rf"Warehouse {re.escape(asked_code)} shipped (\d+) pallets",
        prompt,
    ).group(1)
    return f"{pallets}\nUNKNOWN"
