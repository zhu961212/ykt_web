"""Random timeout answers must use only the question's validated choice keys."""

import copy
import json
import unittest
from unittest import mock

import ykt_core as core


class RandomChoiceTests(unittest.TestCase):
    def setUp(self):
        self.problem = {
            "problem_id": "p1", "type": 1, "title": "Choose an option",
            "options": [("A", "First"), ("b", "Second"), ("D", "Third")],
        }

    def test_single_vote_and_judgment_can_choose_a_nonfirst_real_key(self):
        for problem_type in (1, 3, 6):
            with self.subTest(problem_type=problem_type):
                problem = {**self.problem, "type": problem_type}
                with mock.patch.object(core.secrets, "choice", return_value="D") as choose, \
                        mock.patch.object(core.secrets, "randbelow") as subset:
                    result = core.random_choice_result(problem)

                choose.assert_called_once_with(["A", "b", "D"])
                subset.assert_not_called()
                self.assertEqual(result["result"], ["D"])
                self.assertEqual(json.loads(result["display"]), ["D"])
                self.assertEqual(result["source"], "random_timeout")

    def test_multiple_choice_can_draw_every_nonempty_subset_in_original_order(self):
        problem = {**self.problem, "type": 2}
        all_results = set()
        for draw in range(7):
            with self.subTest(draw=draw), \
                    mock.patch.object(core.secrets, "randbelow", return_value=draw) as choose, \
                    mock.patch.object(core.secrets, "choice") as single:
                result = core.random_choice_result(problem)

            choose.assert_called_once_with(7)
            single.assert_not_called()
            selected = result["result"]
            self.assertTrue(selected)
            self.assertEqual(len(selected), len(set(selected)))
            self.assertEqual(selected, [key for key, _ in problem["options"] if key in selected])
            self.assertEqual(
                core._validate_llm_result(problem, {"answers": selected})["result"], selected,
            )
            all_results.add(tuple(selected))

        self.assertEqual(len(all_results), 7)
        self.assertIn(("b",), all_results)
        self.assertIn(("A", "b", "D"), all_results)

    def test_one_option_multiple_choice_is_always_nonempty(self):
        problem = {**self.problem, "type": 2, "options": [("only", "Only option")]}
        with mock.patch.object(core.secrets, "randbelow", return_value=0) as choose:
            result = core.random_choice_result(problem)
        choose.assert_called_once_with(1)
        self.assertEqual(result["result"], ["only"])

    def test_dictionary_option_identifiers_and_case_are_preserved(self):
        problem = core.extract_problems({"problems": [{
            "prob": "dictionary", "type": 1, "body": "Choose",
            "options": {"b": "First", "D": "Second"},
        }]})["dictionary"]
        original = copy.deepcopy(problem)
        with mock.patch.object(core.secrets, "choice", return_value="b") as choose:
            result = core.random_choice_result(problem)
        choose.assert_called_once_with(["b", "D"])
        self.assertEqual(result["result"], ["b"])
        self.assertEqual(problem, original)

    def test_bad_questions_and_nonchoice_types_never_draw_randomness(self):
        invalid_options = (
            None, [], {}, "A", [("A", "One"), ("a", "Duplicate")],
            [("", "Empty")], [("  ", "Blank")], [(None, "Invalid")],
            [(0, "Unnormalized")], [(True, "Invalid")], [(["A"], "Invalid")],
            ["A", "B"], [("A",)], [{"key": "A", "value": "Unnormalized"}],
        )
        problems = [None, {}, {**self.problem, "parse_error": "Invalid options"}]
        problems.extend({**self.problem, "type": problem_type} for problem_type in (0, 4, 5, 7, "1"))
        problems.extend({**self.problem, "options": options} for options in invalid_options)
        with mock.patch.object(core.secrets, "choice") as single, \
                mock.patch.object(core.secrets, "randbelow") as subset:
            for problem in problems:
                with self.subTest(problem=problem):
                    self.assertIsNone(core.random_choice_result(problem))
        single.assert_not_called()
        subset.assert_not_called()


if __name__ == "__main__":
    unittest.main()
