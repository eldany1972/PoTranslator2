# PoTranslator 2

Desktop app for maintaining a source `.po` catalog and producing several localizations in a single operation.

## What it includes

- Proprietary `.potranslator` projects, saved as versioned, portable ZIP containers.
- Incremental base-PO import: keeps existing translations, detects new, modified and removed messages, and flags whatever changed for review.
- Simultaneous translation into every checked language, with configurable model, quality profile, batch size and character limit.
- General project context, per-language context, and optional per-line context.
- Line selection, multi-select, ranges, tags, states and filters.
- Manual editing and reprocessing of a single line, the current selection, only new messages, only pending ones, or everything.
- Import of already-translated PO files, and selective or full export to `PO_Files/<locale>.po`.
- PO support for `msgctxt`, plurals, comments, references, flags, multiline strings and UTF-8.
- Automatic validation of placeholders, variables, tags and escape sequences.
- Semantic deduplication: strings with the same plural form and context are translated once and the result is reused everywhere they occur.
- A brevity rule for short UI strings (buttons, tabs, titles), so translations don't overflow fixed-width controls.
- Responses API with Structured Outputs. The API key is encrypted with Windows DPAPI and never stored inside the project.

## Running it

Requires Python 3.11 or later. No external packages are needed to run the app.

```powershell
python PoTranslator.py
```

It can also be installed in editable mode:

```powershell
python -m pip install -e .
potranslator
```

## Recommended workflow

1. Press **+ New** and choose the project's working folder.
2. Press **Import base** and pick the English `.po`/`.pot` file.
3. Enter the general context and check the target languages under **Selected**.
4. Configure the API key and use **Translate selected languages**.
5. Review the blue/amber/red messages, add context, or correct entries by hand.
6. Press **Export selected languages**; the results land in `PO_Files`.
7. When a new version of the English PO arrives, use **Import base** again. Existing work is kept, and only what's new or changed is flagged.

## Project format

A `.potranslator` file contains:

- `project.json`: settings, languages, states, context, tags, translations and history.
- `source.po`: a readable snapshot of the source catalog.
- `format.txt`: container identification and version.

Credentials are never stored there. They are encrypted for the current Windows user under `%LOCALAPPDATA%\Ultraton\PoTranslator`.

## Testing

```powershell
python -m unittest discover -s tests -v
```

## Building the executable

```powershell
.\build.ps1
```

The result lands in `dist\PoTranslator.exe`.
