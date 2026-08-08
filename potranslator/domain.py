from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import re
import uuid
from typing import Any, Iterable


APP_NAME = "PoTranslator"
APP_VERSION = "2.0"
APP_AUTHOR = "Daniel Perdomo"

PROJECT_SCHEMA_VERSION = 1

# Sources this short are almost always buttons, tabs, or titles drawn inside a
# fixed-width control, so they are translated under a brevity rule. 0 disables it.
DEFAULT_MAX_UI_WORDS = 4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_digest(*parts: str) -> str:
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def same_language(first: str, second: str) -> bool:
    """Compare BCP-47-ish tags loosely, so a base like "en" matches "en_US"."""
    left = (first or "").strip().casefold().replace("_", "-")
    right = (second or "").strip().casefold().replace("_", "-")
    if not left or not right:
        return False
    return left == right or left.split("-")[0] == right.split("-")[0]


LANGUAGES: tuple[tuple[str, str, str], ...] = (
    ("en", "English", "nplurals=2; plural=(n != 1);"),
    ("es", "Spanish", "nplurals=2; plural=(n != 1);"),
    ("zh-CN", "Chinese (Simplified)", "nplurals=1; plural=0;"),
    ("de", "German", "nplurals=2; plural=(n != 1);"),
    ("fr", "French", "nplurals=2; plural=(n > 1);"),
    ("pt", "Portuguese", "nplurals=2; plural=(n != 1);"),
    ("ru", "Russian", "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);"),
    ("ja", "Japanese", "nplurals=1; plural=0;"),
    ("ko", "Korean", "nplurals=1; plural=0;"),
    ("it", "Italian", "nplurals=2; plural=(n != 1);"),
    ("pl", "Polish", "nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);"),
    ("tr", "Turkish", "nplurals=2; plural=(n > 1);"),
    ("ar", "Arabic", "nplurals=6; plural=(n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : n%100>=3 && n%100<=10 ? 3 : n%100>=11 && n%100<=99 ? 4 : 5);"),
    ("nl", "Dutch", "nplurals=2; plural=(n != 1);"),
    ("th", "Thai", "nplurals=1; plural=0;"),
    ("hi", "Hindi", "nplurals=2; plural=(n != 1);"),
    ("vi", "Vietnamese", "nplurals=1; plural=0;"),
    ("uk", "Ukrainian", "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);"),
    ("cs", "Czech", "nplurals=3; plural=(n==1) ? 0 : (n>=2 && n<=4) ? 1 : 2;"),
    ("id", "Indonesian", "nplurals=1; plural=0;"),
)

SOURCE_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("auto", "<AUTO>"),
    ("en", "English"),
    ("es", "Spanish"),
    ("pt", "Portuguese"),
    ("fr", "French"),
    ("de", "German"),
    ("it", "Italian"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("zh-CN", "Chinese (Simplified)"),
)


@dataclass(slots=True)
class TranslationRecord:
    text: str = ""
    plurals: dict[str, str] = field(default_factory=dict)
    status: str = "pending"  # pending, translated, review, manual, error
    needs_review: bool = False
    error: str = ""
    updated_at: str = ""
    source_hash: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranslationRecord":
        allowed = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass(slots=True)
class SourceEntry:
    uid: str
    msgid: str
    msgctxt: str = ""
    msgid_plural: str = ""
    translator_comments: list[str] = field(default_factory=list)
    extracted_comments: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    previous: list[str] = field(default_factory=list)
    line_context: str = ""
    tags: list[str] = field(default_factory=list)
    state: str = "new"  # new, unchanged, modified, removed
    active: bool = True
    source_hash: str = ""
    translations: dict[str, TranslationRecord] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_hash:
            self.source_hash = self.calculate_source_hash()

    def calculate_source_hash(self) -> str:
        return stable_digest(self.msgctxt, self.msgid, self.msgid_plural)

    @property
    def key(self) -> str:
        return stable_digest(self.msgctxt, self.msgid, self.msgid_plural)

    def anchors(self) -> set[str]:
        anchors: set[str] = set()
        if self.msgctxt:
            anchors.add(f"context:{self.msgctxt.strip().casefold()}")
        for reference in self.references:
            normalized = re.sub(r":\d+(?::\d+)?$", "", reference.strip()).casefold()
            if normalized:
                anchors.add(f"reference:{normalized}")
        for comment in self.extracted_comments + self.translator_comments:
            match = re.match(r"\s*(?:key|id)\s*[:=]\s*(.+)", comment, re.IGNORECASE)
            if match:
                anchors.add(f"comment-key:{match.group(1).strip().casefold()}")
        return anchors

    @classmethod
    def create(cls, msgid: str, **kwargs: Any) -> "SourceEntry":
        return cls(uid=str(uuid.uuid4()), msgid=msgid, **kwargs)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceEntry":
        raw = dict(data)
        raw["translations"] = {
            code: TranslationRecord.from_dict(record)
            for code, record in raw.get("translations", {}).items()
        }
        allowed = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in raw.items() if key in allowed})


@dataclass(slots=True)
class LanguageConfig:
    code: str
    name: str
    plural_forms: str
    enabled: bool = False
    export_enabled: bool = False
    context: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LanguageConfig":
        allowed = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass(slots=True)
class ProjectSettings:
    source_language: str = "auto"
    detected_source_language: str = "en"
    provider: str = "openai"
    model: str = "gpt-5.6-luna"
    batch_size: int = 30
    max_batch_chars: int = 12000
    quality_profile: str = "balanced"
    global_context: str = ""
    preserve_case: bool = True
    keep_concise: bool = True
    max_ui_words: int = DEFAULT_MAX_UI_WORDS

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectSettings":
        allowed = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass(slots=True)
class Project:
    name: str
    project_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: int = PROJECT_SCHEMA_VERSION
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    source_file: str = ""
    source_header: dict[str, str] = field(default_factory=dict)
    source_preamble_comments: list[str] = field(default_factory=list)
    last_import_hash: str = ""
    settings: ProjectSettings = field(default_factory=ProjectSettings)
    languages: list[LanguageConfig] = field(default_factory=list)
    entries: list[SourceEntry] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.languages:
            self.languages = [
                LanguageConfig(code, name, plural_forms)
                for code, name, plural_forms in LANGUAGES
            ]
        else:
            existing = {language.code: language for language in self.languages}
            normalized: list[LanguageConfig] = []
            for code, name, plural_forms in LANGUAGES:
                language = existing.get(code, LanguageConfig(code, name, plural_forms))
                language.name = name
                language.plural_forms = plural_forms
                normalized.append(language)
            self.languages = normalized

    def touch(self) -> None:
        self.updated_at = utc_now()

    def language(self, code: str) -> LanguageConfig | None:
        return next((lang for lang in self.languages if lang.code == code), None)

    def source_language_code(self) -> str:
        code = self.settings.source_language
        if code == "auto":
            code = self.settings.detected_source_language
        return code or "en"

    def target_languages(self) -> list[LanguageConfig]:
        """Every language except the source; translating a catalog into its own
        language is never wanted, so it is hidden rather than merely discouraged."""
        source = self.source_language_code()
        return [language for language in self.languages if not same_language(language.code, source)]

    def active_entries(self) -> list[SourceEntry]:
        return [entry for entry in self.entries if entry.active]

    def ensure_translation_records(self) -> None:
        allowed_codes = {language.code for language in self.languages}
        for entry in self.entries:
            entry.translations = {
                code: record for code, record in entry.translations.items() if code in allowed_codes
            }
            for language in self.languages:
                entry.translations.setdefault(language.code, TranslationRecord())

    def stats(self, language_code: str | None = None) -> dict[str, int]:
        active = self.active_entries()
        result = {
            "total": len(active),
            "new": sum(entry.state == "new" for entry in active),
            "modified": sum(entry.state == "modified" for entry in active),
            "removed": sum(not entry.active for entry in self.entries),
            "translated": 0,
            "pending": 0,
            "review": 0,
            "error": 0,
        }
        if language_code:
            result["new"] = 0
            result["modified"] = 0
            for entry in active:
                state = entry_language_state(entry, language_code)
                if state in {"new", "modified"}:
                    result[state] += 1
                record = entry.translations.get(language_code, TranslationRecord())
                bucket = record.status if record.status in result else "pending"
                if bucket == "manual":
                    bucket = "translated"
                result[bucket] += 1
        return result

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        schema_version = int(data.get("schema_version", 0))
        if schema_version > PROJECT_SCHEMA_VERSION:
            raise ValueError(
                f"This project uses format {schema_version}; this version supports up to {PROJECT_SCHEMA_VERSION}."
            )
        raw = dict(data)
        raw["settings"] = ProjectSettings.from_dict(raw.get("settings", {}))
        raw["languages"] = [LanguageConfig.from_dict(item) for item in raw.get("languages", [])]
        raw["entries"] = [SourceEntry.from_dict(item) for item in raw.get("entries", [])]
        allowed = {name for name in cls.__dataclass_fields__}
        project = cls(**{key: value for key, value in raw.items() if key in allowed})
        project.ensure_translation_records()
        return project


def entry_language_state(entry: SourceEntry, language_code: str) -> str:
    """Change state of an entry as seen from a single target language.

    Removal is a property of the catalog, so it stays global. Everything else is
    local: "new" means this language has nothing yet, and "modified" means it
    carries a hand-written correction or a translation made against an older
    source text. Correcting Spanish therefore never marks Portuguese.
    """
    if not entry.active:
        return "removed"
    record = entry.translations.get(language_code)
    if record is None or not (record.text or record.plurals):
        return "new"
    if record.status == "manual" or record.needs_review:
        return "modified"
    if record.source_hash and record.source_hash != entry.source_hash:
        return "modified"
    return "unchanged"


def mark_records_for_source_change(entry: SourceEntry) -> None:
    for record in entry.translations.values():
        if record.text or record.plurals:
            record.status = "review"
            record.needs_review = True
            record.error = "The source text changed; review or reprocess it."


def select_entries(
    entries: Iterable[SourceEntry],
    mode: str,
    selected_uids: set[str] | None = None,
    language_codes: Iterable[str] = (),
) -> list[SourceEntry]:
    active = [entry for entry in entries if entry.active and entry.msgid]
    selected_uids = selected_uids or set()
    codes = tuple(language_codes)
    if mode == "selected":
        return [entry for entry in active if entry.uid in selected_uids]
    if mode == "new":
        return [entry for entry in active if entry.state == "new"]
    if mode == "pending":
        return [
            entry
            for entry in active
            if any(entry.translations.get(code, TranslationRecord()).status in {"pending", "review", "error"} for code in codes)
        ]
    if mode.startswith("tag:"):
        tag = mode.split(":", 1)[1].casefold()
        return [entry for entry in active if tag in {item.casefold() for item in entry.tags}]
    return active
