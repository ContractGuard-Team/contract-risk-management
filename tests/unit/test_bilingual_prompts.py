"""Static regression checks for bilingual Agent 1/2/3 prompt contracts.

These tests verify prompt files actually used by the repo; they do not claim
that a live LLM follows the prompts without further integration evaluation.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ("agent_1_classification", "agent_2_risk_scoring", "agent_3_recommendation")


class BilingualPromptTests(unittest.TestCase):
    def test_active_core_prompts_route_requested_output_language(self):
        for name in AGENTS:
            with self.subTest(agent=name):
                core = (ROOT / "agents" / name / "prompts" / "core.md").read_text(encoding="utf-8")
                self.assertIn("Quy tắc song ngữ Việt–Anh", core)
                self.assertIn("context.request.output_language", core)
                self.assertIn('`"vi"`', core)
                self.assertIn('`"en"`', core)
                self.assertIn("nguyên văn", core)
                self.assertIn("JSON", core)

    def test_role_and_tool_prompts_include_bilingual_policy(self):
        for name in AGENTS:
            with self.subTest(agent=name):
                soul = (ROOT / "agents" / name / "prompts" / "soul.md").read_text(encoding="utf-8")
                self.assertIn("output_language", soul)
        for name in AGENTS[1:]:
            with self.subTest(agent=name):
                ext = (ROOT / "agents" / name / "prompts" / "external.md").read_text(encoding="utf-8")
                self.assertIn("output_language", ext)
                self.assertIn("không dịch", ext)

    def test_agent_specific_immutability_rules(self):
        a1 = (ROOT / "agents" / AGENTS[0] / "prompts" / "core.md").read_text(encoding="utf-8")
        a2 = (ROOT / "agents" / AGENTS[1] / "prompts" / "core.md").read_text(encoding="utf-8")
        a3 = (ROOT / "agents" / AGENTS[2] / "prompts" / "core.md").read_text(encoding="utf-8")
        self.assertIn("`mixed`", a1)
        self.assertIn("`attribute_name`", a1)
        self.assertIn("`evidence_quote`", a2)
        self.assertIn("orchestration/scoring.py", a2)
        self.assertIn("`proposed_text`", a3)
        self.assertIn("ngôn ngữ của điều khoản gốc", a3)

    def test_request_schema_already_restricts_languages(self):
        inbound = (ROOT / "common/schemas/business/inbound.py").read_text(encoding="utf-8")
        self.assertIn('output_language: Literal["vi", "en"]', inbound)


if __name__ == "__main__":
    unittest.main()
