import unittest

from potranslator.po_catalog import POParseError, parse_po, render_po


SAMPLE_PO = '''# Demo catalog
msgid ""
msgstr ""
"Project-Id-Version: Demo 1.0\\n"
"Language: en\\n"
"Content-Type: text/plain; charset=UTF-8\\n"

#. Main scoreboard label
#: ui/scoreboard.cpp:42
#, python-format
msgctxt "scoreboard"
msgid "GOAL {player}"
msgstr ""

#: ui/matches.cpp:11
msgid "One match"
msgid_plural "%d matches"
msgstr[0] ""
msgstr[1] ""

#. Multiline source
msgid ""
"First line\\n"
"Second \\"quoted\\" line"
msgstr ""
'''


class POCatalogTests(unittest.TestCase):
    def test_parse_standard_features(self):
        catalog = parse_po(SAMPLE_PO)
        self.assertEqual(catalog.header["Language"], "en")
        self.assertEqual(len(catalog.entries), 3)
        self.assertEqual(catalog.entries[0].msgctxt, "scoreboard")
        self.assertIn("python-format", catalog.entries[0].flags)
        self.assertEqual(catalog.entries[1].msgid_plural, "%d matches")
        self.assertEqual(catalog.entries[2].msgid, 'First line\nSecond "quoted" line')

    def test_render_round_trip(self):
        original = parse_po(SAMPLE_PO)
        rendered = render_po(original)
        restored = parse_po(rendered)
        self.assertEqual(restored.header, original.header)
        self.assertEqual(restored.entries, original.entries)

    def test_rejects_unrecognized_syntax(self):
        with self.assertRaises(POParseError):
            parse_po('msgid "x"\nthis is not po\nmsgstr ""\n')


if __name__ == "__main__":
    unittest.main()

