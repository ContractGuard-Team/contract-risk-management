"""Agent core: three prompt types, long-term lessons (propose -> review -> prompt) and reflection after a run."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agents.memory import LONG_TERM_PROMPT, long_term
from agents.memory.long_term import LessonStore, main, parse_lessons
from common.schemas.agent_messages import AgentRequest, ModelResult
from test_agent_base import configured_agents, test_pipeline


class LessonProvider:
    """Answers runs with text and reflections with a lesson list (told apart by the reflection prompt)."""
    def __init__(self):
        self.calls = []

    def generate(self, messages, *, inference_config=None):
        self.calls.append(deepcopy(messages))
        if messages[0]["content"] == "REFLECT":
            agent = json.loads(messages[1]["content"])["agent_id"]
            return ModelResult(json.dumps({"lessons": [f"Bài học của {agent}", f"Bài học của {agent}"]}), {}, "stop")
        return ModelResult("result", {}, "stop")


class LessonStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = LessonStore(":memory:")

    def test_propose_review_and_prompt_section(self):
        first = self.store.propose("a1", ["Kiểm tra phụ lục trước", "kiểm tra  PHỤ LỤC trước", "Đọc điều khoản phạt"])
        self.assertEqual(len(first), 2)  # normalised duplicate skipped
        self.assertEqual(self.store.prompt_section("a1"), "")  # pending lessons never reach the prompt
        self.store.review([first[0]], "approved", "tester")
        self.store.review([first[1]], "rejected", "tester")
        section = self.store.prompt_section("a1")
        self.assertIn(f"[L{first[0]}] Kiểm tra phụ lục trước", section)
        self.assertNotIn("Đọc điều khoản phạt", section)
        self.assertEqual(self.store.prompt_section("a2"), "")  # per agent
        self.assertEqual(self.store.propose("a1", ["Đọc điều khoản phạt"]), [])  # rejected stays rejected
        with self.assertRaises(ValueError):
            self.store.review(first, "pending")

    def test_prompt_budget_keeps_newest(self):
        ids = self.store.propose("a1", [f"Bài học {i} " + "x" * 900 for i in range(20)], status="approved")
        section = self.store.prompt_section("a1")
        self.assertIn(f"[L{ids[-1]}]", section)
        self.assertNotIn(f"[L{ids[0]}]", section)
        self.assertLessEqual(len(section), long_term.MAX_PROMPT_CHARS + 100)

    def test_parse_lessons(self):
        self.assertEqual(parse_lessons('```json\n{"lessons": ["A", {"text": "B"}, "", 3]}\n```'), ["A", "B"])
        self.assertEqual(parse_lessons('["A"]'), ["A"])
        self.assertEqual(parse_lessons("không phải JSON"), [])
        self.assertEqual(len(parse_lessons(json.dumps([str(i) for i in range(9)]))), long_term.MAX_LESSONS_PER_RUN)

    def test_review_cli(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
                "LONG_TERM_MEMORY_DIR": directory, "AGENT_ID": "agent_2_risk_scoring"}):
            store = LessonStore(Path(directory) / "agent_2_risk_scoring.sqlite3")
            pending = store.propose("agent_2_risk_scoring", ["Đối chiếu luật còn hiệu lực"])
            store.close()
            for argv in (["approve", str(pending[0])], ["add", "Bài học do người viết"], ["list", "--status", "approved"]):
                out = io.StringIO()
                with redirect_stdout(out):
                    main(argv)
            rows = [json.loads(line) for line in out.getvalue().splitlines()]
            self.assertEqual([r["text"] for r in rows], ["Đối chiếu luật còn hiệu lực", "Bài học do người viết"])


class PerAgentMemoryTests(unittest.TestCase):
    def test_each_agent_has_its_own_long_term_file_and_memory_prompts(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {"LONG_TERM_MEMORY_DIR": directory}):
            from agents.hermes.agent_factory import create_agents
            agents = create_agents(provider=LessonProvider())
            files = {agent_id: Path(agent.lessons.path).name for agent_id, agent in agents.items()}
            agents["agent_1_classification"].lessons.propose("agent_1_classification", ["chỉ của agent 1"], status="approved")
            self.assertEqual(agents["agent_2_risk_scoring"].lessons.list(), [])
            for agent in agents.values():
                agent.lessons.close()
        self.assertEqual(files, {a: f"{a}.sqlite3" for a in agents})
        for agent in agents.values():  # memory prompts live in each agent's folder, not a shared one
            self.assertTrue((agent.directory / LONG_TERM_PROMPT).is_file())
            self.assertTrue((agent.directory / "memory/short_term_summary.md").is_file())


class PromptAndReflectionTests(unittest.TestCase):
    def test_examples_and_approved_lessons_are_separate_system_messages(self):
        provider = LessonProvider()
        with configured_agents(provider) as agents:
            agent = agents["agent_2_risk_scoring"]
            examples = agent.directory / "prompts/examples"
            examples.mkdir(parents=True)
            (examples / "example_1.md").write_text("Ví dụ một", encoding="utf-8")
            (examples / "example_2.md").write_text("", encoding="utf-8")  # empty example ignored
            agent.lessons = LessonStore(":memory:")
            agent.lessons.propose(agent.agent_id, ["Đã duyệt"], status="approved")
            agent.lessons.propose(agent.agent_id, ["Chờ duyệt"])
            messages = agent.build_messages(AgentRequest("contract"))
        system = [m["content"] for m in messages if m["role"] == "system"]
        self.assertEqual(system[:3], ["Test instructions for prompts/soul.md", "Test instructions for prompts/core.md",
                                      "Test instructions for prompts/external.md"])
        self.assertEqual(system[3], "# Examples\n\n## Example 1\nVí dụ một")
        self.assertIn("Đã duyệt", system[4])
        self.assertNotIn("Chờ duyệt", json.dumps(messages, ensure_ascii=False))

    def test_reflection_after_run_proposes_pending_lessons_used_after_approval(self):
        provider = LessonProvider()
        with configured_agents(provider) as agents:
            for agent in agents.values():
                path = agent.directory / LONG_TERM_PROMPT
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("REFLECT", encoding="utf-8")
                agent.lessons = LessonStore(":memory:")  # each agent its own store
            test_pipeline(agents).run("Hợp đồng A")
            for agent_id, agent in agents.items():
                pending = agent.lessons.list(status="pending")
                self.assertEqual([r["text"] for r in pending], [f"Bài học của {agent_id}"])  # own lesson only, dup dropped
                self.assertEqual(json.loads(pending[0]["source"])["contract_id"], "inline")
            third = agents["agent_3_recommendation"].lessons
            third.review([r["id"] for r in third.list(status="pending")], "approved")
            provider.calls.clear()
            test_pipeline(agents).run("Hợp đồng B")
            runs = [c for c in provider.calls if c[0]["content"] != "REFLECT"]
            self.assertIn("Bài học của agent_3_recommendation", json.dumps(runs[2], ensure_ascii=False))
            self.assertNotIn("Bài học của agent_1", json.dumps(runs[0], ensure_ascii=False))

    def test_empty_reflection_prompt_or_failure_never_fails_the_contract(self):
        provider = LessonProvider()
        with configured_agents(provider) as agents:
            for agent in agents.values():
                agent.lessons = LessonStore(":memory:")
            self.assertEqual(agents["agent_1_classification"].reflect({}, "x")["skipped"][:4], "TODO")  # no prompt yet
            with patch.object(long_term, "reflect", side_effect=RuntimeError("model down")):
                result = test_pipeline(agents).run("Hợp đồng")
            self.assertEqual(set(result), set(agents))
            self.assertTrue(all(agent.lessons.list() == [] for agent in agents.values()))
            agents["agent_1_classification"].lessons = None
            self.assertIn("not configured", agents["agent_1_classification"].reflect({}, "x")["skipped"])


if __name__ == "__main__":
    unittest.main()
