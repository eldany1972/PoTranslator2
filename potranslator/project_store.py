from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import zipfile

from .domain import (
    APP_NAME,
    APP_VERSION,
    Project,
    SourceEntry,
    TranslationRecord,
    mark_records_for_source_change,
    stable_digest,
    utc_now,
)
from .po_catalog import POCatalog, POEntry, load_po, render_po


PROJECT_EXTENSION = ".potranslator"


class ProjectStoreError(ValueError):
    pass


def _catalog_hash(catalog: POCatalog) -> str:
    canonical = [
        (entry.msgctxt, entry.msgid, entry.msgid_plural, entry.references)
        for entry in catalog.entries
    ]
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _source_from_po(entry: POEntry) -> SourceEntry:
    return SourceEntry.create(
        msgid=entry.msgid,
        msgctxt=entry.msgctxt,
        msgid_plural=entry.msgid_plural,
        translator_comments=list(entry.translator_comments),
        extracted_comments=list(entry.extracted_comments),
        references=list(entry.references),
        flags=list(entry.flags),
        previous=list(entry.previous),
    )


def _anchor_index(entries: list[SourceEntry]) -> dict[str, list[SourceEntry]]:
    result: dict[str, list[SourceEntry]] = {}
    for entry in entries:
        for anchor in entry.anchors():
            result.setdefault(anchor, []).append(entry)
    return result


def merge_base_catalog(project: Project, catalog: POCatalog, source_path: str = "") -> dict[str, int]:
    previous_active = [entry for entry in project.entries if entry.active]
    by_key = {entry.key: entry for entry in previous_active}
    anchors = _anchor_index(previous_active)
    used: set[str] = set()
    merged: list[SourceEntry] = []
    counts = {"new": 0, "modified": 0, "unchanged": 0, "removed": 0}

    for po_entry in catalog.entries:
        candidate = _source_from_po(po_entry)
        existing = by_key.get(candidate.key)
        modified = False
        if existing is None:
            possible: dict[str, SourceEntry] = {}
            for anchor in candidate.anchors():
                matches = [entry for entry in anchors.get(anchor, []) if entry.uid not in used]
                if len(matches) == 1:
                    possible[matches[0].uid] = matches[0]
            if len(possible) == 1:
                existing = next(iter(possible.values()))
                modified = True

        if existing is None or existing.uid in used:
            candidate.state = "new"
            project.ensure_translation_records()
            merged.append(candidate)
            counts["new"] += 1
            continue

        used.add(existing.uid)
        old_hash = existing.source_hash
        existing.msgid = candidate.msgid
        existing.msgctxt = candidate.msgctxt
        existing.msgid_plural = candidate.msgid_plural
        existing.translator_comments = candidate.translator_comments
        existing.extracted_comments = candidate.extracted_comments
        existing.references = candidate.references
        existing.flags = candidate.flags
        existing.previous = candidate.previous
        existing.active = True
        existing.source_hash = existing.calculate_source_hash()
        if modified or old_hash != existing.source_hash:
            existing.state = "modified"
            mark_records_for_source_change(existing)
            counts["modified"] += 1
        else:
            existing.state = "unchanged"
            counts["unchanged"] += 1
        merged.append(existing)

    removed = [entry for entry in previous_active if entry.uid not in used]
    for entry in removed:
        entry.active = False
        entry.state = "removed"
        merged.append(entry)
        counts["removed"] += 1

    already_removed = [entry for entry in project.entries if not entry.active and entry not in removed]
    project.entries = merged + already_removed
    project.source_header = dict(catalog.header)
    project.source_preamble_comments = list(catalog.preamble_comments)
    project.source_file = source_path
    project.last_import_hash = _catalog_hash(catalog)
    project.ensure_translation_records()
    project.settings.detected_source_language = detect_language(catalog, source_path)
    project.history.append({"at": utc_now(), "action": "import_base", "counts": counts, "source": source_path})
    project.touch()
    return counts


def import_base_po(project: Project, path: str | Path) -> dict[str, int]:
    file_path = Path(path)
    return merge_base_catalog(project, load_po(file_path), str(file_path))


def detect_language(catalog: POCatalog, path: str = "") -> str:
    header_language = catalog.header.get("Language", "").strip().replace("_", "-")
    if header_language:
        return header_language
    filename = Path(path).stem.casefold()
    known = ("en", "english", "en-us", "en-gb")
    if filename in known or any(filename.endswith(f".{item}") or filename.endswith(f"_{item}") for item in known):
        return "en"
    return "en"


def import_translation_po(project: Project, language_code: str, path: str | Path) -> dict[str, int]:
    catalog = load_po(path)
    by_key = {
        stable_digest(entry.msgctxt, entry.msgid, entry.msgid_plural): entry
        for entry in project.active_entries()
    }
    counts = {"imported": 0, "missing": 0, "unmatched": 0}
    matched: set[str] = set()
    for po_entry in catalog.entries:
        source = by_key.get(stable_digest(po_entry.msgctxt, po_entry.msgid, po_entry.msgid_plural))
        if source is None:
            counts["unmatched"] += 1
            continue
        matched.add(source.uid)
        has_text = bool(po_entry.msgstr or any(po_entry.msgstr_plural.values()))
        if not has_text:
            continue
        source.translations[language_code] = TranslationRecord(
            text=po_entry.msgstr,
            plurals=dict(po_entry.msgstr_plural),
            status="review" if "fuzzy" in po_entry.flags else "translated",
            needs_review="fuzzy" in po_entry.flags,
            updated_at=utc_now(),
            source_hash=source.source_hash,
            model="imported",
        )
        counts["imported"] += 1
    counts["missing"] = len(project.active_entries()) - len(matched)
    project.history.append(
        {"at": utc_now(), "action": "import_translation", "language": language_code, "counts": counts, "source": str(path)}
    )
    project.touch()
    return counts


def project_to_source_catalog(project: Project) -> POCatalog:
    return POCatalog(
        header=dict(project.source_header),
        preamble_comments=list(project.source_preamble_comments),
        entries=[
            POEntry(
                msgid=entry.msgid,
                msgctxt=entry.msgctxt,
                msgid_plural=entry.msgid_plural,
                translator_comments=list(entry.translator_comments),
                extracted_comments=list(entry.extracted_comments),
                references=list(entry.references),
                flags=list(entry.flags),
                previous=list(entry.previous),
            )
            for entry in project.active_entries()
        ],
    )


def export_language_po(project: Project, language_code: str, path: str | Path) -> None:
    language = project.language(language_code)
    if not language:
        raise ProjectStoreError(f"Language is not configured: {language_code}")
    header = dict(project.source_header)
    header.update(
        {
            "Language": language.code,
            "Content-Type": "text/plain; charset=UTF-8",
            "Content-Transfer-Encoding": "8bit",
            "Plural-Forms": language.plural_forms,
            "X-Generator": f"{APP_NAME} {APP_VERSION}",
        }
    )
    catalog = POCatalog(header=header, preamble_comments=list(project.source_preamble_comments))
    for entry in project.active_entries():
        record = entry.translations.get(language_code, TranslationRecord())
        plurals = dict(record.plurals)
        if entry.msgid_plural and not plurals:
            match = re.search(r"nplurals\s*=\s*(\d+)", language.plural_forms)
            count = int(match.group(1)) if match else 2
            plurals = {str(index): "" for index in range(count)}
        flags = list(entry.flags)
        if record.needs_review and "fuzzy" not in flags:
            flags.append("fuzzy")
        catalog.entries.append(
            POEntry(
                msgid=entry.msgid,
                msgstr=record.text,
                msgctxt=entry.msgctxt,
                msgid_plural=entry.msgid_plural,
                msgstr_plural=plurals,
                translator_comments=list(entry.translator_comments),
                extracted_comments=list(entry.extracted_comments),
                references=list(entry.references),
                flags=flags,
                previous=list(entry.previous),
            )
        )
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_po(catalog), encoding="utf-8", newline="\n")


def save_project(project: Project, path: str | Path) -> Path:
    destination = Path(path)
    if destination.suffix.casefold() != PROJECT_EXTENSION:
        destination = destination.with_suffix(PROJECT_EXTENSION)
    destination.parent.mkdir(parents=True, exist_ok=True)
    project.touch()
    source_po = render_po(project_to_source_catalog(project))
    payload = json.dumps(project.to_dict(), ensure_ascii=False, indent=2)
    fd, temp_name = tempfile.mkstemp(prefix=destination.stem + "-", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("format.txt", "PoTranslator Project\nSchema: 1\n")
            archive.writestr("project.json", payload)
            archive.writestr("source.po", source_po)
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return destination


def load_project(path: str | Path) -> Project:
    source = Path(path)
    try:
        with zipfile.ZipFile(source, "r") as archive:
            raw = json.loads(archive.read("project.json").decode("utf-8"))
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise ProjectStoreError(f"Could not open project: {exc}") from exc
    return Project.from_dict(raw)
