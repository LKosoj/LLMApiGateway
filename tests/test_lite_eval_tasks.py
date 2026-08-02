import json
import unittest

from llm_gateway_core.services.openrouter_free_models import (
    LITE_EVAL_RAW_OUTPUT_LIMIT,
    MAX_LITE_EVAL_POINTS,
    build_lite_eval_tasks,
)
from tests._async_compat import run_async
from tests.lite_eval_support import perfect_lite_eval_answer


def _specs(seed):
    return {spec.id: spec for spec in build_lite_eval_tasks(seed)}


class LiteEvalTaskSuiteTests(unittest.TestCase):
    def test_suite_keeps_task_ids_and_total_points(self):
        specs = build_lite_eval_tasks(1)

        self.assertEqual(
            [spec.id for spec in specs],
            [
                "instruction_following_lite",
                "tool_call_lite",
                "code_unit_lite",
                "symbolic_math_lite",
                "grounded_qa_lite",
            ],
        )
        self.assertEqual(sum(spec.max_points for spec in specs), MAX_LITE_EVAL_POINTS)

    def test_same_seed_builds_identical_prompts(self):
        first = [spec.prompt for spec in build_lite_eval_tasks(77)]
        second = [spec.prompt for spec in build_lite_eval_tasks(77)]

        self.assertEqual(first, second)

    def test_different_seeds_change_every_task(self):
        left = {spec.id: spec.prompt for spec in build_lite_eval_tasks(1)}
        right = {spec.id: spec.prompt for spec in build_lite_eval_tasks(2)}

        for task_id, prompt in left.items():
            self.assertNotEqual(prompt, right[task_id], task_id)

    def test_perfect_answers_score_full_points(self):
        for seed in (3, 40, 501):
            for spec in build_lite_eval_tasks(seed):
                result = run_async(spec.grade(perfect_lite_eval_answer(spec.prompt)))
                self.assertEqual(result.points, spec.max_points, f"{seed}/{spec.id}")
                self.assertEqual(result.status, "passed", f"{seed}/{spec.id}")

    def test_answer_built_for_another_seed_does_not_pass(self):
        stale = {spec.id: perfect_lite_eval_answer(spec.prompt) for spec in build_lite_eval_tasks(11)}

        for spec in build_lite_eval_tasks(12):
            result = run_async(spec.grade(stale[spec.id]))
            self.assertLess(result.points, spec.max_points, spec.id)

    def test_every_task_records_raw_output(self):
        for spec in build_lite_eval_tasks(5):
            result = run_async(spec.grade("x" * (LITE_EVAL_RAW_OUTPUT_LIMIT + 50)))
            self.assertIn("rawOutput", result.details, spec.id)
            self.assertEqual(len(result.details["rawOutput"]), LITE_EVAL_RAW_OUTPUT_LIMIT, spec.id)

    def test_error_result_keeps_task_weight(self):
        spec = _specs(5)["code_unit_lite"]

        result = spec.error_result(TimeoutError("slow"))

        self.assertEqual(result.status, "error")
        self.assertEqual(result.points, 0)
        self.assertEqual(result.max_points, spec.max_points)
        self.assertEqual(result.details, {"error": "TimeoutError"})


class GroundedQaTaskTests(unittest.TestCase):
    def test_grounded_answer_and_refusal_score_full_points(self):
        spec = _specs(9)["grounded_qa_lite"]

        result = run_async(spec.grade(perfect_lite_eval_answer(spec.prompt)))

        self.assertEqual(result.points, 50)
        self.assertTrue(result.details["groundedCorrect"])
        self.assertTrue(result.details["refusedUnknown"])
        self.assertIsNone(result.details["hallucinatedPallets"])

    def test_hallucinated_number_loses_half_of_the_points(self):
        spec = _specs(9)["grounded_qa_lite"]
        grounded_line = perfect_lite_eval_answer(spec.prompt).splitlines()[0]

        result = run_async(spec.grade(f"{grounded_line}\n64"))

        self.assertEqual(result.points, 25)
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.details["groundedCorrect"])
        self.assertFalse(result.details["refusedUnknown"])
        self.assertEqual(result.details["hallucinatedPallets"], 64)

    def test_missing_warehouse_is_absent_from_the_facts(self):
        spec = _specs(9)["grounded_qa_lite"]
        missing_code = spec.prompt.split("Line 2: how many pallets did warehouse ")[1].split(" ")[0]

        facts = spec.prompt.split("Use only the facts above")[0]

        self.assertNotIn(missing_code, facts)

    def test_wrong_grounded_answer_loses_half_of_the_points(self):
        spec = _specs(9)["grounded_qa_lite"]

        result = run_async(spec.grade("no idea\nUNKNOWN"))

        self.assertEqual(result.points, 25)
        self.assertFalse(result.details["groundedCorrect"])
        self.assertTrue(result.details["refusedUnknown"])


class CodeUnitTaskTests(unittest.TestCase):
    def test_unit_tests_follow_the_generated_function(self):
        spec = _specs(21)["code_unit_lite"]
        function_name = spec.prompt.split("def ")[1].split("(")[0]
        constant_code = json.dumps({"code": f"def {function_name}(nums):\n    return 0\n"})

        result = run_async(spec.grade(constant_code))

        self.assertEqual(result.details["function"], function_name)
        self.assertFalse(result.details["unitTestsPassed"])
        self.assertTrue(result.details["safeAst"])
        self.assertLess(result.points, 200)

    def test_unsafe_code_is_rejected_before_execution(self):
        spec = _specs(21)["code_unit_lite"]
        function_name = spec.prompt.split("def ")[1].split("(")[0]
        unsafe_code = json.dumps({
            "code": f"import os\n\n\ndef {function_name}(nums):\n    return 0\n",
        })

        result = run_async(spec.grade(unsafe_code))

        self.assertFalse(result.details["safeAst"])
        self.assertEqual(result.details["safetyError"], "unsafe_node:Import")
        self.assertFalse(result.details["unitTestsPassed"])


if __name__ == "__main__":
    unittest.main()
