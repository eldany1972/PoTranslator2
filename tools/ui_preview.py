"""Launch an in-memory populated project for visual QA."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from potranslator.app import PoTranslatorApp
from potranslator.domain import Project, TranslationRecord
from potranslator.po_catalog import parse_po
from potranslator.project_store import merge_base_catalog


SAMPLE = '''msgid ""
msgstr ""
"Project-Id-Version: Stadium Pro\\n"
"Language: en\\n"

#. Primary navigation action
#: ui/main_menu.cpp:12
msgctxt "menu.play"
msgid "PLAY MATCH"
msgstr ""

#: ui/main_menu.cpp:18
msgctxt "menu.settings"
msgid "SETTINGS"
msgstr ""

#: ui/scoreboard.cpp:44
msgctxt "scoreboard.goal"
msgid "GOAL"
msgstr ""

#: ui/scoreboard.cpp:51
msgctxt "scoreboard.shots"
msgid "%d SHOTS ON TARGET"
msgstr ""

#: ui/match.cpp:90
msgctxt "match.overtime"
msgid "OVERTIME"
msgstr ""

#: ui/club.cpp:22
msgctxt "club.name"
msgid "CLUB NAME"
msgstr ""

#: ui/common.cpp:11
msgctxt "common.back"
msgid "BACK"
msgstr ""
'''


def main() -> None:
    app = PoTranslatorApp()
    app.attributes("-topmost", True)
    project = Project("Stadium Pro Localization")
    project.settings.global_context = (
        "Competitive football esports game. Keep HUD and menu terminology concise; GOAL means a scored goal."
    )
    merge_base_catalog(project, parse_po(SAMPLE), "English.po")
    for code in ("es", "pt", "fr", "de"):
        language = project.language(code)
        language.enabled = True
        language.export_enabled = True
    goal = next(entry for entry in project.entries if entry.msgctxt == "scoreboard.goal")
    goal.line_context = "A scored-goal event; do not translate it as an objective or goal frame."
    goal.tags = ["HUD", "review"]
    play = next(entry for entry in project.entries if entry.msgctxt == "menu.play")
    play.translations["es"] = TranslationRecord(
        text="JUGAR PARTIDO", status="translated", source_hash=play.source_hash, model="gpt-5.6-luna"
    )
    settings = next(entry for entry in project.entries if entry.msgctxt == "menu.settings")
    settings.translations["es"] = TranslationRecord(
        text="AJUSTES", status="manual", source_hash=settings.source_hash, model="manual"
    )
    overtime = next(entry for entry in project.entries if entry.msgctxt == "match.overtime")
    overtime.state = "modified"
    overtime.translations["es"] = TranslationRecord(
        text="TIEMPO EXTRA", status="review", needs_review=True, source_hash="old"
    )
    app.project = project
    app.project_path = Path.cwd() / "Stadium Pro.potranslator"
    app.dirty = False
    app._load_project_into_ui()
    app.detail_language_var.set(app._language_display("es"))
    app._refresh_tree()
    app.after(250, lambda: app.tree.selection_set(goal.uid))
    app.after(300, lambda: app._show_detail(goal.uid))
    app.mainloop()


if __name__ == "__main__":
    main()
