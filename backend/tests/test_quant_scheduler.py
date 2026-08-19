"""Pure-logic tests for the shadow-report financial-expert commentary."""

from __future__ import annotations

import unittest
from unittest import mock

from app.services import quant_scheduler as qs


class CommentaryPromptTest(unittest.TestCase):
    def test_prompt_embeds_report_and_title(self):
        prompt = qs._build_commentary_prompt("# FQA 影子模式日报\n\n- 最新净值: 1.02")
        self.assertIn("FQA 影子模式日报", prompt)
        self.assertIn("# 金融专家点评", prompt)

    def test_prompt_includes_advice_instruction(self):
        prompt = qs._build_commentary_prompt("报告")
        self.assertIn("改进建议", prompt)


class GenerateShadowCommentaryTest(unittest.TestCase):
    def test_returns_empty_on_llm_failure(self):
        with mock.patch.object(qs, "_commentary_chat", side_effect=RuntimeError("boom")):
            self.assertEqual(qs._generate_shadow_commentary("报告"), "")

    def test_returns_content_on_success(self):
        async def _fake_chat(prompt: str) -> str:
            return "# 金融专家点评\n\n今日表现稳健。"
        with mock.patch.object(qs, "_commentary_chat", new=_fake_chat):
            self.assertEqual(
                qs._generate_shadow_commentary("报告"),
                "# 金融专家点评\n\n今日表现稳健。",
            )


if __name__ == "__main__":
    unittest.main()
