from __future__ import annotations

from collections import Counter
import json
import re
import threading
import time
from typing import Any, Callable, Hashable, Iterable
from urllib import error, request

from .domain import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_MAX_UI_WORDS,
    LanguageConfig,
    Project,
    SourceEntry,
    TranslationRecord,
    utc_now,
)


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_TOOL_NAME = "submit_translations"
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"


class TranslationError(RuntimeError):
    pass


class IncompleteResponseError(TranslationError):
    def __init__(self, message: str, reason: str = ""):
        super().__init__(message)
        self.reason = reason


PLACEHOLDER_RE = re.compile(
    r"(?:"
    r"\{[^{}]+\}"
    r"|%\([^)]+\)[#0\- +]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa]"
    r"|%(?:\d+\$)?[#0\- +]*\d*(?:\.\d+)?[bcdeEfFgGnosxXa%]"
    r"|<\/?[A-Za-z][^>]*>"
    r"|\\[nrt]"
    r"|\$\{?[A-Za-z_][A-Za-z0-9_.-]*\}?"
    r")"
)


def placeholders(text: str) -> Counter[str]:
    return Counter(PLACEHOLDER_RE.findall(text))


def validate_placeholders(source: str, target: str) -> tuple[bool, str]:
    expected = placeholders(source)
    received = placeholders(target)
    if expected == received:
        return True, ""
    missing = list((expected - received).elements())
    extra = list((received - expected).elements())
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if extra:
        details.append("extra " + ", ".join(extra))
    return False, "Incompatible placeholders: " + "; ".join(details)


def translation_key(entry: SourceEntry) -> tuple[Hashable, ...]:
    """Return every entry field that can influence the model's translation."""
    return (
        entry.msgid,
        entry.msgid_plural,
        entry.msgctxt,
        entry.line_context,
        tuple(entry.extracted_comments),
    )


def group_duplicate_entries(
    entries: Iterable[SourceEntry],
) -> tuple[list[SourceEntry], dict[str, list[SourceEntry]]]:
    """Keep one representative per semantic duplicate group, preserving order."""
    representatives: list[SourceEntry] = []
    groups_by_key: dict[tuple[Hashable, ...], list[SourceEntry]] = {}
    groups_by_uid: dict[str, list[SourceEntry]] = {}
    for entry in entries:
        key = translation_key(entry)
        group = groups_by_key.get(key)
        if group is None:
            group = []
            groups_by_key[key] = group
            representatives.append(entry)
            groups_by_uid[entry.uid] = group
        group.append(entry)
    return representatives, groups_by_uid


def estimate_tokens(entries: Iterable[SourceEntry], global_context: str = "") -> int:
    characters = len(global_context)
    unique_entries, _groups = group_duplicate_entries(entries)
    for entry in unique_entries:
        characters += (
            len(entry.msgid)
            + len(entry.msgid_plural)
            + len(entry.msgctxt)
            + len(entry.line_context)
            + sum(len(comment) for comment in entry.extracted_comments)
            + 80
        )
    return max(1, characters // 4)


def split_batches(entries: list[SourceEntry], batch_size: int, max_chars: int) -> list[list[SourceEntry]]:
    size = max(1, int(batch_size))
    char_limit = max(500, int(max_chars))
    result: list[list[SourceEntry]] = []
    current: list[SourceEntry] = []
    current_chars = 0
    for entry in entries:
        item_chars = len(entry.msgid) + len(entry.msgid_plural) + len(entry.line_context) + 100
        if current and (len(current) >= size or current_chars + item_chars > char_limit):
            result.append(current)
            current = []
            current_chars = 0
        current.append(entry)
        current_chars += item_chars
    if current:
        result.append(current)
    return result


def output_token_budget(entries: Iterable[SourceEntry], target: LanguageConfig) -> int:
    """Leave room for JSON overhead, UUIDs, translated text, and plural forms."""
    plural_match = re.search(r"nplurals\s*=\s*(\d+)", target.plural_forms)
    plural_count = int(plural_match.group(1)) if plural_match else 2
    estimated = 512
    for entry in entries:
        forms = plural_count if entry.msgid_plural else 1
        source_chars = len(entry.msgid) + len(entry.msgid_plural)
        estimated += 96 + max(16, source_chars * 2 * forms)
    return max(4096, min(32000, estimated))


TRANSLATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "plurals": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "index": {"type": "integer", "minimum": 0},
                                "text": {"type": "string"},
                            },
                            "required": ["index", "text"],
                        },
                    },
                },
                "required": ["id", "text", "plurals"],
            },
        }
    },
    "required": ["translations"],
}


def _instructions(project: Project, target: LanguageConfig) -> str:
    source_code = (
        project.settings.detected_source_language
        if project.settings.source_language == "auto"
        else project.settings.source_language
    )
    profile = {
        "economy": "Prefer direct, efficient translations. Do not add stylistic elaboration.",
        "balanced": "Balance natural localization, terminology consistency, and concise UI fit.",
        "maximum": "Silently verify meaning, terminology, grammar, placeholders, and UI fit before returning each item.",
    }.get(project.settings.quality_profile, "Balance quality and concise UI fit.")
    concise = "Keep UI strings as concise as naturally possible." if project.settings.keep_concise else "Use a natural length."
    case = "Preserve capitalization style when natural in the target language." if project.settings.preserve_case else "Use natural target-language capitalization."
    context = project.settings.global_context.strip() or "No additional project context was supplied."
    try:
        ui_words = max(0, int(project.settings.max_ui_words))
    except (TypeError, ValueError):
        ui_words = DEFAULT_MAX_UI_WORDS
    # 0 means the project opted out of the short-string brevity rule entirely.
    ui_rule = (
        f"Treat every source string of {ui_words} words or fewer as a UI control label: a button, tab, "
        "menu entry, or title drawn inside a fixed-width control. Render these as a terse command or noun "
        "phrase, never a sentence. Drop articles, prepositions, and any word the user can infer from the "
        "screen, and prefer the shortest standard term the target platform already uses for that action. "
        "Keep the result close to the source length; a label that overflows its control is a defect, so "
        "never let a short source grow into a descriptive phrase. For example \"RESTORE DEFAULTS\" must "
        "stay a two-word button, not become \"restore to the predetermined values\".\n"
        if ui_words
        else ""
    )
    language_notes = target.context.strip()
    # Language rules come last so they win over the project-wide guidance above.
    language_block = (
        f"\n\n{target.name.upper()} LANGUAGE RULES (these override the project context on conflict):\n{language_notes}"
        if language_notes
        else ""
    )
    return f"""You are a senior software and video-game localization translator.
Translate every supplied item from {source_code} to {target.name} ({target.code}).
Return data that exactly matches the requested JSON schema and preserve every item id.
Never translate, remove, add, or reorder formatting placeholders, variables, markup tags, escape sequences, keyboard tokens, or product names.
Translate meaning rather than isolated words. Use each item's optional context to resolve ambiguity.
{ui_rule}{concise}
{case}
{profile}
Do not include commentary, confidence notes, alternatives, or source text in the translated value.
For singular entries, return text and an empty plurals array. For plural entries, return text as the singular translation and every plural form required by this target rule: {target.plural_forms}

PROJECT CONTEXT:
{context}{language_block}"""


def _input_items(entries: list[SourceEntry]) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "id": entry.uid,
                    "source": entry.msgid,
                    "source_plural": entry.msgid_plural,
                    "gettext_context": entry.msgctxt,
                    "translator_context": entry.line_context,
                    "developer_comments": entry.extracted_comments,
                }
                for entry in entries
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return content["text"]
            if content.get("type") == "refusal":
                raise TranslationError(content.get("refusal", "The model refused the request."))
    raise TranslationError("OpenAI did not return structured text.")


class OpenAITranslationClient:
    def __init__(self, api_key: str, endpoint: str = OPENAI_RESPONSES_URL, timeout: int = 120):
        if not api_key.strip():
            raise TranslationError("The OpenAI API key is not configured.")
        self.api_key = api_key.strip()
        self.endpoint = endpoint
        self.timeout = timeout

    def translate_batch(
        self,
        project: Project,
        target: LanguageConfig,
        entries: list[SourceEntry],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
        output_budget = output_token_budget(entries, target)
        reasoning_effort = "low" if project.settings.quality_profile == "maximum" else "none"
        body = {
            "model": project.settings.model,
            "instructions": _instructions(project, target),
            "input": _input_items(entries),
            "prompt_cache_key": f"potranslator-{project.project_id[:12]}-{target.code}",
            "max_output_tokens": output_budget,
            "reasoning": {"effort": reasoning_effort},
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "localization_batch",
                    "strict": True,
                    "schema": TRANSLATION_SCHEMA,
                }
            },
        }
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        http_request = request.Request(
            self.endpoint,
            data=raw,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with request.urlopen(http_request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            message = f"OpenAI returned HTTP {exc.code}."
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
                message = error_payload.get("error", {}).get("message", message)
            except (ValueError, UnicodeDecodeError):
                pass
            raise TranslationError(message) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise TranslationError(f"Could not connect to OpenAI: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise TranslationError("OpenAI returned an invalid JSON response.") from exc

        if payload.get("status") == "incomplete":
            details = payload.get("incomplete_details") or {}
            reason = str(details.get("reason", "unknown"))
            if reason == "max_output_tokens":
                message = (
                    f"OpenAI truncated a batch of {len(entries)} messages at the "
                    f"{output_budget:,}-token output limit. The batch will be split automatically."
                )
            else:
                message = f"OpenAI returned an incomplete response ({reason})."
            raise IncompleteResponseError(message, reason=reason)
        if payload.get("error"):
            api_error = payload.get("error") or {}
            raise TranslationError(str(api_error.get("message") or "OpenAI returned a response error."))

        try:
            structured = json.loads(_response_text(payload))
            rows = structured["translations"]
            if not isinstance(rows, list):
                raise TypeError("translations must be an array")
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            response_id = str(payload.get("id", "unknown"))
            status = str(payload.get("status", "unknown"))
            raise TranslationError(
                f"OpenAI returned invalid structured JSON (status: {status}, response: {response_id})."
            ) from exc
        mapped = {str(row["id"]): row for row in rows if isinstance(row, dict) and "id" in row}
        usage = payload.get("usage") or {}
        return mapped, {
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
        }


class AnthropicTranslationClient:
    """Talks to Claude's Messages API and forces structured output via a tool call,
    since Anthropic's tool-use + tool_choice mechanism is the equivalent of OpenAI's
    strict json_schema response format."""

    def __init__(self, api_key: str, endpoint: str = ANTHROPIC_MESSAGES_URL, timeout: int = 120):
        if not api_key.strip():
            raise TranslationError("The Claude (Anthropic) API key is not configured.")
        self.api_key = api_key.strip()
        self.endpoint = endpoint
        self.timeout = timeout

    def translate_batch(
        self,
        project: Project,
        target: LanguageConfig,
        entries: list[SourceEntry],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
        output_budget = output_token_budget(entries, target)
        body = {
            "model": project.settings.model,
            "system": _instructions(project, target),
            "max_tokens": output_budget,
            "messages": [{"role": "user", "content": _input_items(entries)}],
            "tools": [
                {
                    "name": ANTHROPIC_TOOL_NAME,
                    "description": "Submit the translated batch that exactly matches the requested items.",
                    "input_schema": TRANSLATION_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": ANTHROPIC_TOOL_NAME},
        }
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        http_request = request.Request(
            self.endpoint,
            data=raw,
            method="POST",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with request.urlopen(http_request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            message = f"Claude returned HTTP {exc.code}."
            try:
                error_payload = json.loads(exc.read().decode("utf-8"))
                message = error_payload.get("error", {}).get("message", message)
            except (ValueError, UnicodeDecodeError):
                pass
            raise TranslationError(message) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise TranslationError(f"Could not connect to Claude: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise TranslationError("Claude returned an invalid JSON response.") from exc

        if payload.get("type") == "error":
            api_error = payload.get("error") or {}
            raise TranslationError(str(api_error.get("message") or "Claude returned a response error."))

        stop_reason = payload.get("stop_reason")
        if stop_reason == "max_tokens":
            message = (
                f"Claude truncated a batch of {len(entries)} messages at the "
                f"{output_budget:,}-token output limit. The batch will be split automatically."
            )
            raise IncompleteResponseError(message, reason="max_output_tokens")

        tool_input: dict[str, Any] | None = None
        for block in payload.get("content", []) or []:
            if block.get("type") == "tool_use" and block.get("name") == ANTHROPIC_TOOL_NAME:
                tool_input = block.get("input")
                break
        if tool_input is None:
            response_id = str(payload.get("id", "unknown"))
            raise TranslationError(
                f"Claude did not return the expected structured tool call (response: {response_id})."
            )

        rows = tool_input.get("translations")
        if not isinstance(rows, list):
            raise TranslationError("Claude returned invalid structured data (missing a 'translations' array).")
        mapped = {str(row["id"]): row for row in rows if isinstance(row, dict) and "id" in row}
        usage = payload.get("usage") or {}
        return mapped, {
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
        }


EventCallback = Callable[[dict[str, Any]], None]


class TranslationRunner:
    def __init__(self, client: OpenAITranslationClient | AnthropicTranslationClient, max_retries: int = 2):
        self.client = client
        self.max_retries = max(0, max_retries)

    def run(
        self,
        project: Project,
        entries: list[SourceEntry],
        language_codes: list[str],
        cancel_event: threading.Event,
        on_event: EventCallback,
        skip_completed: bool = False,
    ) -> None:
        languages = [project.language(code) for code in language_codes]
        languages = [language for language in languages if language is not None]
        work_by_language: dict[str, list[SourceEntry]] = {}
        representatives_by_language: dict[str, list[SourceEntry]] = {}
        duplicate_groups_by_language: dict[str, dict[str, list[SourceEntry]]] = {}
        for language in languages:
            work_by_language[language.code] = [
                entry
                for entry in entries
                if not skip_completed
                or entry.translations.get(language.code, TranslationRecord()).status not in {"translated", "manual"}
                or entry.translations.get(language.code, TranslationRecord()).source_hash != entry.source_hash
            ]
            representatives, duplicate_groups = group_duplicate_entries(work_by_language[language.code])
            representatives_by_language[language.code] = representatives
            duplicate_groups_by_language[language.code] = duplicate_groups
        total = sum(len(items) for items in work_by_language.values())
        unique_total = sum(len(items) for items in representatives_by_language.values())
        completed = 0
        on_event(
            {
                "type": "started",
                "total": total,
                "languages": len(languages),
                "entries": len(entries),
                "unique_entries": unique_total,
                "duplicates_reused": total - unique_total,
            }
        )

        for language in languages:
            if cancel_event.is_set():
                break
            language_entries = representatives_by_language[language.code]
            duplicate_groups = duplicate_groups_by_language[language.code]
            pending_batches = split_batches(
                language_entries, project.settings.batch_size, project.settings.max_batch_chars
            )
            on_event({"type": "language", "code": language.code, "name": language.name})
            batch_index = 0
            while pending_batches:
                if cancel_event.is_set():
                    break
                batch = pending_batches.pop(0)
                batch_index += 1
                on_event(
                    {
                        "type": "batch",
                        "language": language.code,
                        "index": batch_index,
                        "count": batch_index + len(pending_batches),
                        "size": len(batch),
                        "occurrences": sum(len(duplicate_groups[entry.uid]) for entry in batch),
                    }
                )
                translated: dict[str, dict[str, Any]] | None = None
                usage = {"input_tokens": 0, "output_tokens": 0}
                failure = ""
                split_required = False
                for attempt in range(self.max_retries + 1):
                    if cancel_event.is_set():
                        break
                    try:
                        translated, usage = self.client.translate_batch(project, language, batch)
                        break
                    except IncompleteResponseError as exc:
                        failure = str(exc)
                        split_required = exc.reason == "max_output_tokens" and len(batch) > 1
                        break
                    except TranslationError as exc:
                        failure = str(exc)
                        on_event(
                            {
                                "type": "retry",
                                "language": language.code,
                                "attempt": attempt + 1,
                                "message": failure,
                            }
                        )
                        if attempt < self.max_retries:
                            cancel_event.wait(1.5 * (attempt + 1))
                if split_required:
                    midpoint = max(1, len(batch) // 2)
                    halves = [batch[:midpoint], batch[midpoint:]]
                    pending_batches[0:0] = [half for half in halves if half]
                    on_event(
                        {
                            "type": "split",
                            "language": language.code,
                            "size": len(batch),
                            "first": len(halves[0]),
                            "second": len(halves[1]),
                            "message": failure,
                        }
                    )
                    continue
                if translated is None:
                    for representative in batch:
                        for entry in duplicate_groups[representative.uid]:
                            record = entry.translations.setdefault(language.code, TranslationRecord())
                            record.status = "error"
                            record.needs_review = True
                            record.error = failure or "Translation cancelled."
                            completed += 1
                            on_event(
                                {
                                    "type": "entry",
                                    "uid": entry.uid,
                                    "language": language.code,
                                    "ok": False,
                                    "completed": completed,
                                    "total": total,
                                }
                            )
                    continue

                per_entry_input = usage["input_tokens"] // max(1, len(batch))
                per_entry_output = usage["output_tokens"] // max(1, len(batch))
                for representative in batch:
                    row = translated.get(representative.uid)
                    target_text = ""
                    target_plurals: dict[str, str] = {}
                    validation_error = ""
                    if row is None:
                        ok = False
                    else:
                        target_text = str(row.get("text", ""))
                        target_plurals = {
                            str(item.get("index")): str(item.get("text", ""))
                            for item in row.get("plurals", [])
                            if isinstance(item, dict) and "index" in item
                        }
                        ok, validation_error = validate_placeholders(representative.msgid, target_text)
                        if ok and representative.msgid_plural:
                            for plural_text in target_plurals.values():
                                ok, validation_error = validate_placeholders(representative.msgid_plural, plural_text)
                                if not ok:
                                    break
                    group = duplicate_groups[representative.uid]
                    for duplicate_index, entry in enumerate(group):
                        record = entry.translations.setdefault(language.code, TranslationRecord())
                        if row is None:
                            record.status = "error"
                            record.needs_review = True
                            record.error = "The model omitted this entry."
                        else:
                            record.text = target_text
                            record.plurals = dict(target_plurals)
                            record.status = "translated" if ok else "error"
                            record.needs_review = not ok
                            record.error = validation_error
                            record.updated_at = utc_now()
                            record.source_hash = entry.source_hash
                            record.model = project.settings.model
                            # Store the real API usage once per unique translation.
                            if duplicate_index == 0:
                                record.input_tokens += per_entry_input
                                record.output_tokens += per_entry_output
                        completed += 1
                        on_event(
                            {
                                "type": "entry",
                                "uid": entry.uid,
                                "language": language.code,
                                "ok": ok,
                                "completed": completed,
                                "total": total,
                                "input_tokens": per_entry_input if duplicate_index == 0 else 0,
                                "output_tokens": per_entry_output if duplicate_index == 0 else 0,
                                "deduplicated": duplicate_index > 0,
                            }
                        )
        project.touch()
        on_event({"type": "cancelled" if cancel_event.is_set() else "finished", "completed": completed, "total": total})
