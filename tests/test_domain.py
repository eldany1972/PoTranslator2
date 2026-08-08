import unittest

from potranslator.domain import (
    LANGUAGES,
    Project,
    SourceEntry,
    TranslationRecord,
    entry_language_state,
    mark_records_for_source_change,
)


def _translated(entry: SourceEntry, text: str, status: str = "translated") -> TranslationRecord:
    return TranslationRecord(text=text, status=status, source_hash=entry.source_hash)


class EntryLanguageStateTests(unittest.TestCase):
    def _project_with_entry(self) -> tuple[Project, SourceEntry]:
        project = Project("Game")
        entry = SourceEntry.create(msgid="Kick", msgctxt="action.kick")
        entry.state = "unchanged"
        project.entries = [entry]
        project.ensure_translation_records()
        return project, entry

    def test_untranslated_language_reads_as_new(self):
        _project, entry = self._project_with_entry()
        self.assertEqual(entry_language_state(entry, "es"), "new")

    def test_manual_correction_marks_only_that_language(self):
        _project, entry = self._project_with_entry()
        entry.translations["es"] = _translated(entry, "chutar")
        entry.translations["pt"] = _translated(entry, "chutar")
        self.assertEqual(entry_language_state(entry, "es"), "unchanged")

        # The user fixes the Spanish wording by hand.
        entry.translations["es"] = _translated(entry, "patear", status="manual")

        self.assertEqual(entry_language_state(entry, "es"), "modified")
        self.assertEqual(entry_language_state(entry, "pt"), "unchanged")

    def test_source_change_marks_every_translated_language_then_clears_one_by_one(self):
        _project, entry = self._project_with_entry()
        entry.translations["es"] = _translated(entry, "patear")
        entry.translations["pt"] = _translated(entry, "chutar")

        entry.msgid = "Kick hard"
        entry.source_hash = entry.calculate_source_hash()
        mark_records_for_source_change(entry)

        self.assertEqual(entry_language_state(entry, "es"), "modified")
        self.assertEqual(entry_language_state(entry, "pt"), "modified")

        # Re-translating Spanish clears Spanish only.
        entry.translations["es"] = _translated(entry, "patear fuerte")

        self.assertEqual(entry_language_state(entry, "es"), "unchanged")
        self.assertEqual(entry_language_state(entry, "pt"), "modified")

    def test_removed_entries_stay_global(self):
        _project, entry = self._project_with_entry()
        entry.active = False
        entry.translations["es"] = _translated(entry, "patear", status="manual")
        self.assertEqual(entry_language_state(entry, "es"), "removed")

    def test_stats_count_changes_per_language(self):
        project, entry = self._project_with_entry()
        entry.translations["es"] = _translated(entry, "patear", status="manual")
        entry.translations["pt"] = _translated(entry, "chutar")

        self.assertEqual(project.stats("es")["modified"], 1)
        self.assertEqual(project.stats("pt")["modified"], 0)
        self.assertEqual(project.stats("de")["new"], 1)


class TargetLanguageTests(unittest.TestCase):
    def _codes(self, project: Project) -> list[str]:
        return [language.code for language in project.target_languages()]

    def test_english_is_offered_as_a_target(self):
        self.assertIn("en", [code for code, _name, _plural in LANGUAGES])

    def test_source_language_is_hidden_but_english_stays(self):
        project = Project("Game")
        project.settings.source_language = "es"

        codes = self._codes(project)

        self.assertNotIn("es", codes)
        self.assertIn("en", codes)
        self.assertIn("pt", codes)

    def test_auto_source_hides_the_detected_language(self):
        project = Project("Game")
        project.settings.source_language = "auto"
        project.settings.detected_source_language = "en"
        self.assertNotIn("en", self._codes(project))
        self.assertIn("es", self._codes(project))

    def test_regional_source_tags_still_hide_the_base_language(self):
        project = Project("Game")
        project.settings.source_language = "auto"
        project.settings.detected_source_language = "en-US"
        self.assertNotIn("en", self._codes(project))

    def test_every_language_is_a_target_when_the_source_is_not_in_the_list(self):
        project = Project("Game")
        project.settings.source_language = "sv"
        self.assertEqual(len(project.target_languages()), len(LANGUAGES))


class LanguageContextTests(unittest.TestCase):
    def test_language_context_survives_a_round_trip(self):
        project = Project("Game")
        project.language("es").context = "kick = patear, never chutar"
        restored = Project.from_dict(project.to_dict())
        self.assertEqual(restored.language("es").context, "kick = patear, never chutar")
        self.assertEqual(restored.language("pt").context, "")

    def test_projects_saved_before_language_context_still_load(self):
        payload = Project("Game").to_dict()
        for language in payload["languages"]:
            language.pop("context")
        self.assertEqual(Project.from_dict(payload).language("es").context, "")


if __name__ == "__main__":
    unittest.main()
