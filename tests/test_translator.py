import json
import threading
import unittest
from unittest.mock import patch

from potranslator.domain import DEFAULT_MAX_UI_WORDS, Project, SourceEntry
from potranslator.translator import (
    IncompleteResponseError,
    OpenAITranslationClient,
    TranslationRunner,
    _input_items,
    _instructions,
    estimate_tokens,
    group_duplicate_entries,
    output_token_budget,
    placeholders,
    split_batches,
    validate_placeholders,
)


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class TranslatorTests(unittest.TestCase):
    def test_placeholder_validation(self):
        source = "GOAL {player} %d <b>\\n"
        self.assertEqual(placeholders(source)["{player}"], 1)
        self.assertTrue(validate_placeholders(source, "GOL {player} %d <b>\\n")[0])
        ok, message = validate_placeholders(source, "GOL %d")
        self.assertFalse(ok)
        self.assertIn("{player}", message)

    def test_batch_limits(self):
        entries = [SourceEntry.create(msgid="x" * 100) for _ in range(7)]
        batches = split_batches(entries, batch_size=3, max_chars=10000)
        self.assertEqual([len(batch) for batch in batches], [3, 3, 1])

    def test_output_budget_includes_structured_json_headroom(self):
        project = Project("Demo")
        entries = [SourceEntry.create(msgid=f"Menu item {index}") for index in range(30)]
        self.assertGreaterEqual(output_token_budget(entries, project.language("es")), 4096)

    def test_semantic_duplicates_are_grouped_but_context_is_respected(self):
        first = SourceEntry.create(msgid="5 GOALS", line_context="Score label")
        duplicate = SourceEntry.create(msgid="5 GOALS", line_context="Score label")
        different_context = SourceEntry.create(msgid="5 GOALS", line_context="Achievement title")
        representatives, groups = group_duplicate_entries([first, duplicate, different_context])
        self.assertEqual(representatives, [first, different_context])
        self.assertEqual(groups[first.uid], [first, duplicate])
        self.assertEqual(groups[different_context.uid], [different_context])
        self.assertLess(
            estimate_tokens([first, duplicate], "Game UI"),
            estimate_tokens([first, different_context], "Game UI"),
        )

    def test_short_ui_strings_rule_ships_with_the_program(self):
        # The rule lives in code, so it applies to every project without the user
        # having to type it into the project context.
        project = Project("Demo")
        self.assertEqual(project.settings.global_context, "")
        self.assertEqual(project.settings.max_ui_words, DEFAULT_MAX_UI_WORDS)

        instructions = _instructions(project, project.language("es"))

        self.assertIn(f"{DEFAULT_MAX_UI_WORDS} words or fewer", instructions)
        self.assertIn("RESTORE DEFAULTS", instructions)

    def test_max_ui_words_is_configurable_and_zero_disables_the_rule(self):
        project = Project("Demo")

        project.settings.max_ui_words = 7
        self.assertIn("7 words or fewer", _instructions(project, project.language("es")))

        project.settings.max_ui_words = 0
        disabled = _instructions(project, project.language("es"))
        self.assertNotIn("words or fewer", disabled)
        self.assertNotIn("RESTORE DEFAULTS", disabled)
        # Turning the rule off must not disturb the rest of the prompt.
        self.assertIn("PROJECT CONTEXT:", disabled)
        self.assertIn("senior software and video-game localization translator", disabled)

    def test_items_carry_no_per_entry_ui_metadata(self):
        payload = json.loads(_input_items([SourceEntry.create(msgid="RESTORE DEFAULTS")]))
        self.assertEqual(
            set(payload["items"][0]),
            {
                "id",
                "source",
                "source_plural",
                "gettext_context",
                "translator_context",
                "developer_comments",
            },
        )

    def test_language_context_reaches_only_its_own_prompt(self):
        project = Project("Demo")
        project.settings.global_context = "Arcade football game."
        project.language("es").context = "kick = patear, never chutar"

        spanish = _instructions(project, project.language("es"))
        portuguese = _instructions(project, project.language("pt"))

        self.assertIn("kick = patear, never chutar", spanish)
        self.assertIn("Arcade football game.", spanish)
        self.assertNotIn("patear", portuguese)
        self.assertIn("Arcade football game.", portuguese)
        # Language rules are stated after the project context so they take priority.
        self.assertGreater(spanish.index("patear"), spanish.index("Arcade football game."))

    def test_structured_response_and_runner(self):
        project = Project("Demo")
        language = project.language("es")
        language.enabled = True
        entry = SourceEntry.create(msgid="PLAY {name}")
        project.entries = [entry]
        project.ensure_translation_records()
        structured = {"translations": [{"id": entry.uid, "text": "JUGAR {name}", "plurals": []}]}
        response = {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(structured)}]}],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        events = []
        with patch("potranslator.translator.request.urlopen", return_value=_FakeResponse(response)) as urlopen:
            runner = TranslationRunner(OpenAITranslationClient("test-key"), max_retries=0)
            runner.run(project, [entry], ["es"], threading.Event(), events.append)
        request_body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(request_body["reasoning"]["effort"], "none")
        self.assertGreaterEqual(request_body["max_output_tokens"], 4096)
        record = entry.translations["es"]
        self.assertEqual(record.text, "JUGAR {name}")
        self.assertEqual(record.status, "translated")
        self.assertEqual(record.input_tokens, 100)
        self.assertEqual(events[-1]["type"], "finished")

    def test_client_reports_incomplete_output(self):
        project = Project("Demo")
        entry = SourceEntry.create(msgid="PLAY")
        response = {
            "id": "resp_test",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
        }
        with patch("potranslator.translator.request.urlopen", return_value=_FakeResponse(response)):
            with self.assertRaises(IncompleteResponseError) as caught:
                OpenAITranslationClient("test-key").translate_batch(
                    project, project.language("es"), [entry]
                )
        self.assertEqual(caught.exception.reason, "max_output_tokens")

    def test_runner_splits_truncated_batches(self):
        project = Project("Demo")
        first = SourceEntry.create(msgid="PLAY")
        second = SourceEntry.create(msgid="SETTINGS")
        project.entries = [first, second]
        project.ensure_translation_records()
        calls = []

        class SplittingClient:
            def translate_batch(self, _project, _language, entries):
                calls.append(len(entries))
                if len(entries) > 1:
                    raise IncompleteResponseError("truncated", reason="max_output_tokens")
                entry = entries[0]
                return (
                    {entry.uid: {"id": entry.uid, "text": f"ES:{entry.msgid}", "plurals": []}},
                    {"input_tokens": 10, "output_tokens": 5},
                )

        events = []
        TranslationRunner(SplittingClient(), max_retries=0).run(
            project, [first, second], ["es"], threading.Event(), events.append
        )
        self.assertEqual(calls, [2, 1, 1])
        self.assertEqual(first.translations["es"].text, "ES:PLAY")
        self.assertEqual(second.translations["es"].text, "ES:SETTINGS")
        self.assertIn("split", [event["type"] for event in events])

    def test_runner_translates_duplicate_once_and_copies_result(self):
        project = Project("Demo")
        first = SourceEntry.create(msgid="5 GOALS", line_context="Score label")
        duplicate = SourceEntry.create(msgid="5 GOALS", line_context="Score label")
        project.entries = [first, duplicate]
        project.ensure_translation_records()
        calls = []

        class FakeClient:
            def translate_batch(self, _project, _language, entries):
                calls.append(list(entries))
                return (
                    {entries[0].uid: {"id": entries[0].uid, "text": "5 GOLES", "plurals": []}},
                    {"input_tokens": 40, "output_tokens": 8},
                )

        events = []
        runner = TranslationRunner(FakeClient(), max_retries=0)
        runner.run(project, [first, duplicate], ["es"], threading.Event(), events.append)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], [first])
        self.assertEqual(first.translations["es"].text, "5 GOLES")
        self.assertEqual(duplicate.translations["es"].text, "5 GOLES")
        self.assertEqual(first.translations["es"].input_tokens, 40)
        self.assertEqual(duplicate.translations["es"].input_tokens, 0)
        self.assertEqual(events[0]["duplicates_reused"], 1)


if __name__ == "__main__":
    unittest.main()
