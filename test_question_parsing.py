"""Regression coverage for preserving question meaning and option identifiers."""

import unittest
from unittest import mock

import ykt_core as core


class QuestionParsingTests(unittest.IsolatedAsyncioTestCase):
    def test_math_comparisons_survive_plain_text_and_html(self):
        cases = (
            ("0 < x < 1, y > 2", "0 < x < 1, y > 2"),
            ("x<y>z", "x<y>z"),
            ("x <b and y >c", "x <b and y >c"),
            ("<p>0 &lt; x &lt; 1, y &gt; 2</p>", "0 < x < 1, y > 2"),
            ("<b>2 + 2</b><br> = &amp;", "2 + 2 = &"),
            ("<span class='math'>a &lt; b</span>", "a < b"),
            ("<p>Question without closing tag", "Question without closing tag"),
            ("&lt;p&gt;literal markup&lt;/p&gt;", "<p>literal markup</p>"),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(core._plain_text(source), expected)

        problem = core.extract_problems({"problems": [{
            "prob": "inequality", "type": 1,
            "body": "<p>0 &lt; x &lt; 1, y &gt; 2</p>",
            "options": ["a < b < c, d > e", "a >= b"],
        }]})["inequality"]
        prompt = core.build_prompt(problem)
        self.assertIn("0 < x < 1, y > 2", prompt)
        self.assertIn("a < b < c, d > e", prompt)

    def test_dictionary_option_keys_are_preserved_in_prompt_and_result(self):
        problem = core.extract_problems({"problems": [{
            "prob": "mapping", "type": 1, "body": "Select the second option",
            "options": {"B": "first", "D": "second"},
        }]})["mapping"]

        self.assertEqual(problem["options"], [("B", "first"), ("D", "second")])
        self.assertIn("  D. second", core.build_prompt(problem))
        self.assertEqual(
            core._validate_llm_result(problem, {"answers": ["d"]})["result"], ["D"]
        )
        with self.assertRaisesRegex(ValueError, "非法选项"):
            core._validate_llm_result(problem, {"answers": ["A"]})

    def test_dictionary_choices_and_image_labels_use_the_same_key(self):
        image_url = "https://cdn.example.edu/option-d.png"
        problem = core.extract_problems({"problems": [{
            "prob": "mapping-images", "type": 1, "body": "Select the picture",
            "choices": {
                "B": {"content": "first"},
                "D": {"value": f'<img src="{image_url}">'},
            },
        }]})["mapping-images"]

        self.assertEqual([key for key, _ in problem["options"]], ["B", "D"])
        images = [item for item in problem["image_items"] if item["url"] == image_url]
        self.assertEqual(len(images), 1)
        self.assertIn("D", images[0]["label"])

    def test_explicit_list_option_keys_and_zero_are_preserved(self):
        problem = core.extract_problems({"problems": [{
            "prob": "explicit", "type": 2, "body": "Select identifiers",
            "options": [{"key": 0, "value": "zero"}, {"key": "Z", "value": "last"}],
        }]})["explicit"]
        self.assertEqual(problem["options"], [("0", "zero"), ("Z", "last")])

    async def test_malformed_options_are_marked_without_losing_other_questions(self):
        for malformed in (1, False, "A, B"):
            with self.subTest(options=malformed):
                problems = core.extract_problems({"slides": [{"problems": [
                    {"prob": "bad", "type": 1, "body": "Bad", "options": malformed},
                    {"prob": "good", "type": 1, "body": "Good", "options": ["one", "two"]},
                ]}]})
                self.assertEqual(set(problems), {"bad", "good"})
                self.assertIn("parse_error", problems["bad"])
                self.assertEqual(problems["bad"]["options"], [])
                self.assertNotIn("parse_error", problems["good"])
                self.assertEqual(
                    core._validate_llm_result(problems["good"], {"answers": ["B"]})["result"],
                    ["B"],
                )
                with self.assertRaisesRegex(ValueError, "选项格式无效"):
                    core._validate_llm_result(problems["bad"], {"answers": ["A"]})
                http = mock.MagicMock()
                solver = core.LLMSolver({"llm": {"base_url": "https://api.example.test/v1"}}, http)
                with self.assertRaisesRegex(ValueError, "选项格式无效"):
                    await solver.solve(problems["bad"])
                http.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
