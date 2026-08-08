from pathlib import Path
import tempfile
import unittest
import zipfile

from potranslator.domain import LANGUAGES, Project, TranslationRecord
from potranslator.po_catalog import parse_po
from potranslator.project_store import (
    export_language_po,
    import_translation_po,
    load_project,
    merge_base_catalog,
    save_project,
)


BASE_V1 = '''msgid ""
msgstr ""
"Language: en\\n"

#: ui/menu.cpp:10
msgctxt "menu.play"
msgid "PLAY"
msgstr ""

#: ui/score.cpp:20
msgctxt "score.goal"
msgid "GOAL"
msgstr ""
'''

BASE_V2 = '''msgid ""
msgstr ""
"Language: en\\n"

#: ui/menu.cpp:12
msgctxt "menu.play"
msgid "PLAY NOW"
msgstr ""

#: ui/new.cpp:2
msgctxt "menu.quit"
msgid "QUIT"
msgstr ""
'''

SPANISH = '''msgid ""
msgstr ""
"Language: es\\n"

#: ui/menu.cpp:10
msgctxt "menu.play"
msgid "PLAY"
msgstr "JUGAR"

#: ui/score.cpp:20
msgctxt "score.goal"
msgid "GOAL"
msgstr "GOL"
'''


class ProjectStoreTests(unittest.TestCase):
    def test_language_list_matches_original_application(self):
        self.assertEqual(
            [name for _code, name, _plural in LANGUAGES],
            [
                "English",
                "Spanish",
                "Chinese (Simplified)",
                "German",
                "French",
                "Portuguese",
                "Russian",
                "Japanese",
                "Korean",
                "Italian",
                "Polish",
                "Turkish",
                "Arabic",
                "Dutch",
                "Thai",
                "Hindi",
                "Vietnamese",
                "Ukrainian",
                "Czech",
                "Indonesian",
            ],
        )
        self.assertNotIn("pt-BR", {code for code, _name, _plural in LANGUAGES})

    def test_incremental_merge_preserves_and_marks(self):
        project = Project("Game")
        first = merge_base_catalog(project, parse_po(BASE_V1), "English.po")
        self.assertEqual(first, {"new": 2, "modified": 0, "unchanged": 0, "removed": 0})
        play = next(entry for entry in project.entries if entry.msgctxt == "menu.play")
        play.translations["es"] = TranslationRecord(text="JUGAR", status="translated", source_hash=play.source_hash)

        second = merge_base_catalog(project, parse_po(BASE_V2), "English.po")
        self.assertEqual(second, {"new": 1, "modified": 1, "unchanged": 0, "removed": 1})
        updated = next(entry for entry in project.entries if entry.msgctxt == "menu.play")
        self.assertEqual(updated.msgid, "PLAY NOW")
        self.assertEqual(updated.translations["es"].text, "JUGAR")
        self.assertEqual(updated.translations["es"].status, "review")
        self.assertFalse(next(entry for entry in project.entries if entry.msgctxt == "score.goal").active)

    def test_project_round_trip_is_zip_and_excludes_credentials(self):
        project = Project("Game")
        merge_base_catalog(project, parse_po(BASE_V1), "English.po")
        with tempfile.TemporaryDirectory() as temp:
            path = save_project(project, Path(temp) / "Game.potranslator")
            self.assertTrue(zipfile.is_zipfile(path))
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(set(archive.namelist()), {"format.txt", "project.json", "source.po"})
                self.assertNotIn("api_key", archive.read("project.json").decode("utf-8").casefold())
            restored = load_project(path)
            self.assertEqual(restored.name, "Game")
            self.assertEqual(len(restored.entries), 2)

    def test_import_and_export_translation(self):
        project = Project("Game")
        merge_base_catalog(project, parse_po(BASE_V1), "English.po")
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "es.po"
            source.write_text(SPANISH, encoding="utf-8")
            counts = import_translation_po(project, "es", source)
            self.assertEqual(counts["imported"], 2)
            destination = Path(temp) / "PO_Files" / "es.po"
            export_language_po(project, "es", destination)
            exported = parse_po(destination.read_text(encoding="utf-8"))
            self.assertEqual(exported.header["Language"], "es")
            self.assertEqual(exported.entries[0].msgstr, "JUGAR")
            self.assertEqual(exported.entries[1].msgstr, "GOL")


if __name__ == "__main__":
    unittest.main()
