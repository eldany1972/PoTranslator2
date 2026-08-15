from __future__ import annotations

from pathlib import Path
import queue
import re
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable

from .credentials import CredentialError, delete_api_key, load_api_key, save_api_key
from .domain import (
    APP_AUTHOR,
    APP_NAME as PRODUCT_NAME,
    APP_VERSION,
    DEFAULT_MAX_UI_WORDS,
    LANGUAGES,
    SOURCE_LANGUAGES,
    Project,
    SourceEntry,
    TranslationRecord,
    entry_language_state,
    select_entries,
    utc_now,
)
from .po_catalog import POParseError
from .project_store import (
    PROJECT_EXTENSION,
    ProjectStoreError,
    export_language_po,
    import_base_po,
    import_translation_po,
    load_project as read_project_file,
    save_project as write_project_file,
)
from .translator import (
    AnthropicTranslationClient,
    OpenAITranslationClient,
    TranslationError,
    TranslationRunner,
    estimate_tokens,
)


APP_NAME = f"{PRODUCT_NAME} {APP_VERSION}"

# Display name -> internal provider key (also used for credential storage and API dispatch).
PROVIDER_KEYS: dict[str, str] = {"OpenAI": "openai", "Claude": "anthropic"}
PROVIDER_NAMES: dict[str, str] = {key: label for label, key in PROVIDER_KEYS.items()}

# Model choices per provider. Claude model IDs per Anthropic's current lineup:
# Fable 5 (top-end), Opus 5 (recommended for complex/agentic coding-style work),
# Sonnet 5 (fast, balanced), Haiku 4.5 (fastest/cheapest).
PROVIDER_MODELS: dict[str, tuple[str, ...]] = {
    "openai": ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6", "gpt-5.4-mini", "gpt-5.4"),
    "anthropic": ("claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"),
}
MODELS = PROVIDER_MODELS["openai"]  # kept for backward compatibility with any external callers
MODEL_TO_PROVIDER: dict[str, str] = {
    model: provider for provider, models in PROVIDER_MODELS.items() for model in models
}
QUALITY_LABELS = {
    "Economy": "economy",
    "Balanced": "balanced",
    "Maximum quality": "maximum",
}
QUALITY_CODES = {value: key for key, value in QUALITY_LABELS.items()}
SOURCE_LABELS = {label: code for code, label in SOURCE_LANGUAGES}
SOURCE_CODES = {code: label for code, label in SOURCE_LANGUAGES}


COLORS = {
    "window": "#0A0F1E",
    "panel": "#111827",
    "surface": "#182235",
    "surface_alt": "#202B40",
    "border": "#2B3852",
    "text": "#F4F7FB",
    "muted": "#93A4BD",
    "accent": "#7168F8",
    "accent_hover": "#8179FF",
    "green": "#36D399",
    "amber": "#F8B84E",
    "red": "#FA6B7B",
    "blue": "#5AB5F7",
}


class Tooltip:
    """Hover help bubble. Tk ships no tooltip widget, so this is the minimum that
    behaves well: it appears after a short dwell and disappears on leave or click."""

    def __init__(self, widget: tk.Misc, text: str, delay: int = 450) -> None:
        self.widget = widget
        self.text = text
        self.delay = delay
        self.job: str | None = None
        self.window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")
        widget.bind("<Destroy>", self.hide, add="+")

    def _schedule(self, _event: tk.Event | None = None) -> None:
        self._cancel()
        self.job = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None

    def _show(self) -> None:
        self.job = None
        if self.window or not self.text or not self.widget.winfo_viewable():
            return
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.attributes("-topmost", True)
        tk.Label(
            self.window,
            text=self.text,
            justify="left",
            wraplength=320,
            bg=COLORS["surface_alt"],
            fg=COLORS["text"],
            highlightthickness=1,
            highlightbackground=COLORS["accent"],
            relief="flat",
            padx=10,
            pady=8,
            font=("Segoe UI", 9),
        ).pack()
        self.window.wm_geometry(
            f"+{self.widget.winfo_rootx() + 14}"
            f"+{self.widget.winfo_rooty() + self.widget.winfo_height() + 6}"
        )

    def hide(self, _event: tk.Event | None = None) -> None:
        self._cancel()
        if self.window:
            self.window.destroy()
            self.window = None


class PoTranslatorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1500x900")
        self.minsize(1180, 720)
        self.configure(bg=COLORS["window"])
        resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
        icon_path = resource_root / "assets" / "ultraton.ico"
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except tk.TclError:
                pass
        try:
            self.state("zoomed")
        except tk.TclError:
            pass
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.project: Project | None = None
        self.project_path: Path | None = None
        self.dirty = False
        self.selected_uids: set[str] = set()
        self.current_uid = ""
        self.current_number = 0
        self.visible_uids: list[str] = []
        self.language_enabled_vars: dict[str, tk.BooleanVar] = {}
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.worker_events: queue.Queue[dict] = queue.Queue()
        self.autosave_job: str | None = None
        # Language whose context is currently loaded in the editor, so the box can be
        # flushed to the right language after the picker has already moved on.
        self.language_context_code = ""

        self._build_style()
        self._build_variables()
        self._build_menu()
        self._build_layout()
        self._set_empty_state()
        self.after(120, self._poll_worker_events)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background=COLORS["panel"], foreground=COLORS["text"])
        style.configure("TFrame", background=COLORS["panel"])
        style.configure("Window.TFrame", background=COLORS["window"])
        style.configure("Surface.TFrame", background=COLORS["surface"])
        style.configure("TLabel", background=COLORS["panel"], foreground=COLORS["text"])
        style.configure("Muted.TLabel", background=COLORS["panel"], foreground=COLORS["muted"])
        style.configure("Surface.TLabel", background=COLORS["surface"], foreground=COLORS["text"])
        style.configure("SurfaceMuted.TLabel", background=COLORS["surface"], foreground=COLORS["muted"])
        style.configure("Title.TLabel", background=COLORS["window"], foreground=COLORS["text"], font=("Segoe UI Semibold", 17))
        style.configure("Subtitle.TLabel", background=COLORS["window"], foreground=COLORS["muted"], font=("Segoe UI", 9))
        style.configure("Section.TLabel", background=COLORS["panel"], foreground=COLORS["text"], font=("Segoe UI Semibold", 11))
        style.configure("BigNumber.TLabel", background=COLORS["surface"], foreground=COLORS["text"], font=("Segoe UI Semibold", 16))
        style.configure("TButton", background=COLORS["surface_alt"], foreground=COLORS["text"], bordercolor=COLORS["border"], padding=(12, 7))
        style.map("TButton", background=[("active", COLORS["border"]), ("disabled", COLORS["surface"])], foreground=[("disabled", COLORS["muted"])])
        style.configure("Accent.TButton", background=COLORS["accent"], foreground="#FFFFFF", bordercolor=COLORS["accent"], padding=(14, 8), font=("Segoe UI Semibold", 10))
        style.map("Accent.TButton", background=[("active", COLORS["accent_hover"]), ("disabled", COLORS["border"])])
        style.configure("Danger.TButton", background="#462431", foreground="#FFBAC3", bordercolor="#653143")
        style.map("Danger.TButton", background=[("active", "#5A2A3A")])
        style.configure("TEntry", fieldbackground=COLORS["surface_alt"], foreground=COLORS["text"], insertcolor=COLORS["text"], bordercolor=COLORS["border"], padding=6)
        style.configure("TCombobox", fieldbackground=COLORS["surface_alt"], background=COLORS["surface_alt"], foreground=COLORS["text"], arrowcolor=COLORS["muted"], bordercolor=COLORS["border"], padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", COLORS["surface_alt"])], foreground=[("readonly", COLORS["text"])])
        style.configure("TSpinbox", fieldbackground=COLORS["surface_alt"], foreground=COLORS["text"], arrowcolor=COLORS["muted"], bordercolor=COLORS["border"], padding=5)
        style.configure("Treeview", background=COLORS["panel"], fieldbackground=COLORS["panel"], foreground=COLORS["text"], bordercolor=COLORS["border"], rowheight=31, font=("Segoe UI", 10))
        style.map("Treeview", background=[("selected", "#354166")], foreground=[("selected", "#FFFFFF")])
        style.configure("Treeview.Heading", background=COLORS["surface"], foreground=COLORS["muted"], bordercolor=COLORS["border"], padding=(7, 8), font=("Segoe UI Semibold", 9))
        style.map("Treeview.Heading", background=[("active", COLORS["surface_alt"])])
        style.configure("Horizontal.TProgressbar", background=COLORS["accent"], troughcolor=COLORS["surface_alt"], bordercolor=COLORS["surface_alt"])
        style.configure("TPanedwindow", background=COLORS["window"])
        style.configure("TSeparator", background=COLORS["border"])
        self.option_add("*TCombobox*Listbox.background", COLORS["surface_alt"])
        self.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", COLORS["accent"])
        self.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")

    def _build_variables(self) -> None:
        self.project_name_var = tk.StringVar(value="No project")
        self.source_language_var = tk.StringVar(value="<AUTO>")
        self.provider_var = tk.StringVar(value="OpenAI")
        self.model_var = tk.StringVar(value=PROVIDER_MODELS["openai"][0])
        self.batch_size_var = tk.IntVar(value=30)
        self.max_chars_var = tk.IntVar(value=12000)
        self.max_ui_words_var = tk.IntVar(value=DEFAULT_MAX_UI_WORDS)
        self.quality_var = tk.StringVar(value="Balanced")
        self.process_scope_var = tk.StringVar(value="New messages")
        self.search_var = tk.StringVar()
        self.status_filter_var = tk.StringVar(value="All")
        self.tag_filter_var = tk.StringVar()
        self.range_from_var = tk.StringVar(value="1")
        self.range_to_var = tk.StringVar(value="1")
        self.detail_language_var = tk.StringVar()
        self.detail_status_var = tk.StringVar(value="—")
        self.language_context_label_var = tk.StringVar(value="LANGUAGE CONTEXT")
        self.tags_var = tk.StringVar()
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_text_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Ready")
        self.summary_var = tk.StringVar(value="0 messages")
        self.token_estimate_var = tk.StringVar(value="≈ 0 tokens")
        self.search_var.trace_add("write", lambda *_: self._refresh_tree())
        self.tag_filter_var.trace_add("write", lambda *_: self._refresh_tree())

    def _build_menu(self) -> None:
        menu = tk.Menu(self, bg=COLORS["panel"], fg=COLORS["text"], activebackground=COLORS["accent"], activeforeground="#FFFFFF", tearoff=False)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="New project…", accelerator="Ctrl+N", command=self.new_project)
        file_menu.add_command(label="Open project…", accelerator="Ctrl+O", command=self.open_project)
        file_menu.add_command(label="Save", accelerator="Ctrl+S", command=self.save_project)
        file_menu.add_command(label="Save as…", command=self.save_project_as)
        file_menu.add_separator()
        file_menu.add_command(label="Import/update base PO…", command=self.import_base)
        file_menu.add_command(label="Import translated PO…", command=self.import_translation)
        file_menu.add_separator()
        file_menu.add_command(label="Export selected languages", command=lambda: self.export_languages(False))
        file_menu.add_command(label="Export all languages", command=lambda: self.export_languages(True))
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        settings_menu = tk.Menu(menu, tearoff=False)
        settings_menu.add_command(label="Configure OpenAI API key…", command=lambda: self.configure_api_key("openai"))
        settings_menu.add_command(label="Remove OpenAI API key", command=lambda: self.remove_api_key("openai"))
        settings_menu.add_separator()
        settings_menu.add_command(label="Configure Claude API key…", command=lambda: self.configure_api_key("anthropic"))
        settings_menu.add_command(label="Remove Claude API key", command=lambda: self.remove_api_key("anthropic"))
        menu.add_cascade(label="File", menu=file_menu)
        menu.add_cascade(label="Settings", menu=settings_menu)
        self.configure(menu=menu)
        self.bind_all("<Control-n>", lambda _event: self.new_project())
        self.bind_all("<Control-o>", lambda _event: self.open_project())
        self.bind_all("<Control-s>", lambda _event: self.save_project())

    def _build_layout(self) -> None:
        root = ttk.Frame(self, style="Window.TFrame", padding=(16, 12, 16, 10))
        root.pack(fill="both", expand=True)
        self._build_toolbar(root)
        # Claim the status strip before the panes expand, otherwise the progress bar
        # is the first thing squeezed off the window.
        self._build_status_bar(root)
        content = ttk.Panedwindow(root, orient="horizontal")
        content.pack(fill="both", expand=True, pady=(12, 8))
        left = ttk.Frame(content, width=285, padding=(12, 12))
        center = ttk.Frame(content, padding=(10, 0))
        right = ttk.Frame(content, width=365, padding=(12, 12))
        content.add(left, weight=0)
        content.add(center, weight=1)
        content.add(right, weight=0)
        self._build_left_panel(left)
        self._build_center_panel(center)
        self._build_detail_panel(right)

    def _build_toolbar(self, parent: ttk.Frame) -> None:
        toolbar = ttk.Frame(parent, style="Window.TFrame")
        toolbar.pack(fill="x")
        # Buttons claim their width first; on a narrow window the decorative title is
        # what gets clipped, never a control the user needs to click.
        buttons = ttk.Frame(toolbar, style="Window.TFrame")
        buttons.pack(side="right")
        title_box = ttk.Frame(toolbar, style="Window.TFrame")
        title_box.pack(side="left")
        ttk.Label(title_box, text="PO TRANSLATOR", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title_box,
            text=f"Multi-language localization workspace  ·  V{APP_VERSION}  ·  Created by {APP_AUTHOR}",
            style="Subtitle.TLabel",
        ).pack(anchor="w")
        ttk.Button(buttons, text="＋ New", command=self.new_project).pack(side="left", padx=3)
        ttk.Button(buttons, text="Open", command=self.open_project).pack(side="left", padx=3)
        ttk.Button(buttons, text="Save", command=self.save_project).pack(side="left", padx=3)
        ttk.Button(buttons, text="Import base", command=self.import_base).pack(side="left", padx=3)
        ttk.Button(buttons, text="Import translation", command=self.import_translation).pack(side="left", padx=3)
        ttk.Button(buttons, text="Export selected languages", style="Accent.TButton", command=lambda: self.export_languages(False)).pack(side="left", padx=(8, 3))
        ttk.Button(buttons, text="Export all languages", command=lambda: self.export_languages(True)).pack(side="left", padx=(3, 0))

    def _build_left_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, textvariable=self.project_name_var, style="Section.TLabel").pack(anchor="w")
        ttk.Label(parent, text="Active project", style="Muted.TLabel").pack(anchor="w", pady=(0, 12))

        ttk.Label(parent, text="SOURCE LANGUAGE", style="Muted.TLabel").pack(anchor="w")
        source_combo = ttk.Combobox(parent, textvariable=self.source_language_var, values=[label for _, label in SOURCE_LANGUAGES], state="readonly")
        source_combo.pack(fill="x", pady=(4, 11))
        source_combo.bind("<<ComboboxSelected>>", self._on_source_language_changed)

        general_label = ttk.Label(parent, text="GENERAL CONTEXT", style="Muted.TLabel")
        general_label.pack(anchor="w")
        self.global_context_text = self._text_widget(parent, height=3, wrap="word")
        self.global_context_text.pack(fill="x", pady=(4, 9))
        self.global_context_text.bind("<KeyRelease>", lambda _event: self._settings_changed())
        general_tip = (
            "Background sent with every translation, in every language. Describe what the "
            "product is, its tone, and any term that must never be translated.\n\n"
            'Example: "Arcade football game, informal tone. Keep GOAL, COMBO and the team '
            'names in English."'
        )
        Tooltip(general_label, general_tip)
        Tooltip(self.global_context_text, general_tip)

        # Glossary-style notes for one target language; follows the language picked
        # in the details panel, which is also the language the table is showing.
        language_label = ttk.Label(parent, textvariable=self.language_context_label_var, style="Muted.TLabel")
        language_label.pack(anchor="w")
        self.language_context_text = self._text_widget(parent, height=3, wrap="word")
        self.language_context_text.pack(fill="x", pady=(4, 9))
        self.language_context_text.bind("<KeyRelease>", lambda _event: self._settings_changed())
        language_tip = (
            "Notes for the language shown above only — the one selected in the Translation "
            "picker on the right. Use it for wording rules that apply to that language and "
            "no other. It overrides the general context when they disagree.\n\n"
            'Example for Spanish: "kick = patear, never chutar. Address the player as tú."'
        )
        Tooltip(language_label, language_tip)
        Tooltip(self.language_context_text, language_tip)

        provider_row = ttk.Frame(parent)
        provider_row.pack(fill="x")
        ttk.Label(provider_row, text="PROVIDER", style="Muted.TLabel").pack(side="left")
        ttk.Button(provider_row, text="API key", command=lambda: self.configure_api_key()).pack(side="right")
        provider_combo = ttk.Combobox(
            parent, textvariable=self.provider_var, values=list(PROVIDER_KEYS), state="readonly"
        )
        provider_combo.pack(fill="x", pady=(4, 9))
        provider_combo.bind("<<ComboboxSelected>>", self._on_provider_changed)

        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Label(row, text="MODEL", style="Muted.TLabel").pack(side="left")
        self.model_combo = ttk.Combobox(
            parent, textvariable=self.model_var, values=PROVIDER_MODELS[self._current_provider_key()]
        )
        self.model_combo.pack(fill="x", pady=(4, 9))
        self.model_combo.bind("<<ComboboxSelected>>", lambda _event: self._settings_changed())
        self.model_combo.bind("<FocusOut>", lambda _event: self._settings_changed())

        settings_row = ttk.Frame(parent)
        settings_row.pack(fill="x", pady=(0, 10))
        quality_box = ttk.Frame(settings_row)
        quality_box.pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Label(quality_box, text="PROFILE", style="Muted.TLabel").pack(anchor="w")
        quality = ttk.Combobox(quality_box, textvariable=self.quality_var, values=list(QUALITY_LABELS), state="readonly", width=11)
        quality.pack(fill="x", pady=(4, 0))
        quality.bind("<<ComboboxSelected>>", lambda _event: self._settings_changed())
        batch_box = ttk.Frame(settings_row)
        batch_box.pack(side="left", padx=(4, 0))
        ttk.Label(batch_box, text="BATCH", style="Muted.TLabel").pack(anchor="w")
        batch = ttk.Spinbox(batch_box, from_=1, to=100, textvariable=self.batch_size_var, width=5, command=self._settings_changed)
        batch.pack(pady=(4, 0))
        batch.bind("<FocusOut>", lambda _event: self._settings_changed())
        ui_box = ttk.Frame(settings_row)
        ui_box.pack(side="left", padx=(6, 0))
        ui_label = ttk.Label(ui_box, text="UI WORDS", style="Muted.TLabel")
        ui_label.pack(anchor="w")
        ui_words = ttk.Spinbox(
            ui_box, from_=0, to=20, textvariable=self.max_ui_words_var, width=5, command=self._settings_changed
        )
        ui_words.pack(pady=(4, 0))
        ui_words.bind("<FocusOut>", lambda _event: self._settings_changed())
        ui_tip = (
            "MAX UI TEXT — the longest a source string can be and still count as a UI label.\n\n"
            "Any source string with this many words or fewer is treated as a UI control "
            "label — a button, tab, menu entry, or title — and is translated as briefly as "
            "possible so it still fits the control.\n\n"
            'Example with 4: "RESTORE DEFAULTS" stays a short button instead of expanding '
            'into "RESTAURAR A LOS VALORES PREDETERMINADOS", which would overflow.\n\n'
            "Raise it if your interface has roomy controls, lower it if space is tight.\n"
            "Set it to 0 to switch the rule off and translate every string at natural length."
        )
        Tooltip(ui_label, ui_tip)
        Tooltip(ui_words, ui_tip)

        # Compensate for the canvas scrollbar so the header is centered over the checkboxes.
        language_header = ttk.Frame(parent, padding=(0, 0, 24, 0))
        language_header.pack(fill="x", pady=(2, 4))
        language_header.columnconfigure(0, weight=1)
        language_header.columnconfigure(1, minsize=70)
        ttk.Label(language_header, text="LANGUAGES", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(language_header, text="Selected", style="Muted.TLabel").grid(row=0, column=1)

        language_outer = tk.Frame(parent, bg=COLORS["surface"], highlightthickness=1, highlightbackground=COLORS["border"])
        language_outer.pack(fill="both", expand=True)
        self.language_canvas = tk.Canvas(language_outer, bg=COLORS["surface"], highlightthickness=0, width=250, height=150)
        language_scroll = ttk.Scrollbar(language_outer, orient="vertical", command=self.language_canvas.yview)
        self.language_canvas.configure(yscrollcommand=language_scroll.set)
        language_scroll.pack(side="right", fill="y")
        self.language_canvas.pack(side="left", fill="both", expand=True)
        self.language_frame = tk.Frame(self.language_canvas, bg=COLORS["surface"])
        self.language_frame.columnconfigure(0, weight=1)
        self.language_window = self.language_canvas.create_window((0, 0), window=self.language_frame, anchor="nw")
        self.language_frame.bind("<Configure>", lambda _event: self.language_canvas.configure(scrollregion=self.language_canvas.bbox("all")))
        self.language_canvas.bind("<Configure>", lambda event: self.language_canvas.itemconfigure(self.language_window, width=event.width))
        self._rebuild_language_list()

    def _build_center_panel(self, parent: ttk.Frame) -> None:
        filter_row = ttk.Frame(parent)
        filter_row.pack(fill="x", pady=(0, 8))
        search = ttk.Entry(filter_row, textvariable=self.search_var, width=30)
        search.pack(side="left", fill="x", expand=True)
        search.insert(0, "")
        status = ttk.Combobox(filter_row, textvariable=self.status_filter_var, values=("All", "New", "Modified", "Pending", "Review", "Errors", "Ready"), state="readonly", width=13)
        status.pack(side="left", padx=(6, 0))
        status.bind("<<ComboboxSelected>>", lambda _event: self._refresh_tree())
        ttk.Entry(filter_row, textvariable=self.tag_filter_var, width=14).pack(side="left", padx=(6, 0))

        range_row = ttk.Frame(parent)
        range_row.pack(fill="x", pady=(0, 8))
        ttk.Label(range_row, text="Range", style="Muted.TLabel").pack(side="left")
        ttk.Entry(range_row, textvariable=self.range_from_var, width=6).pack(side="left", padx=(5, 3))
        ttk.Label(range_row, text="to", style="Muted.TLabel").pack(side="left")
        ttk.Entry(range_row, textvariable=self.range_to_var, width=6).pack(side="left", padx=3)
        ttk.Button(range_row, text="Check range", command=self.select_range).pack(side="left", padx=(3, 8))
        ttk.Button(range_row, text="All", command=self.select_all_visible).pack(side="left", padx=2)
        ttk.Button(range_row, text="None", command=self.clear_selection).pack(side="left", padx=2)
        ttk.Button(range_row, text="Tag", command=self.tag_selected).pack(side="right", padx=2)
        ttk.Button(range_row, text="Mark for reprocessing", command=self.mark_selected_pending).pack(side="right", padx=2)

        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill="both", expand=True)
        columns = ("mark", "line", "source", "translation", "state", "progress")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        self.tree.heading("mark", text="✓")
        self.tree.heading("line", text="#")
        self.tree.heading("source", text="Source text")
        self.tree.heading("translation", text="Translation")
        self.tree.heading("state", text="Change")
        self.tree.heading("progress", text="Status")
        self.tree.column("mark", width=38, stretch=False, anchor="center")
        self.tree.column("line", width=54, stretch=False, anchor="center")
        self.tree.column("source", width=430, minwidth=180)
        self.tree.column("translation", width=360, minwidth=140)
        self.tree.column("state", width=92, stretch=False, anchor="center")
        self.tree.column("progress", width=108, stretch=False, anchor="center")
        y_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree.tag_configure("new", background="#173143", foreground="#C8EDFF")
        self.tree.tag_configure("modified", background="#392E20", foreground="#FFE2AD")
        self.tree.tag_configure("error", background="#3C222D", foreground="#FFC2C9")
        self.tree.tag_configure("removed", foreground="#64748B")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_selection)
        self.tree.bind("<Button-1>", self._on_tree_click, add="+")
        self.tree.bind("<Double-1>", lambda _event: self.start_translation("selected"))

        footer = ttk.Frame(parent)
        footer.pack(fill="x", pady=(7, 0))
        ttk.Label(footer, textvariable=self.summary_var, style="Muted.TLabel").pack(side="left")
        ttk.Label(
            footer,
            text="Per selected language — Blue: new   Amber: modified   Red: error",
            style="Muted.TLabel",
        ).pack(side="right")

    def _build_detail_panel(self, parent: ttk.Frame) -> None:
        # Bulk translation sits with the other action buttons, pinned to the bottom edge
        # first so it stays reachable however tall the per-message fields above it grow.
        bulk = ttk.Frame(parent)
        bulk.pack(side="bottom", fill="x")
        ttk.Button(bulk, text="Translate selected languages", style="Accent.TButton", command=lambda: self.start_translation()).pack(fill="x", pady=2)
        ttk.Button(bulk, text="Translate all languages", command=lambda: self.start_translation(all_languages=True)).pack(fill="x", pady=2)
        scope = ttk.Combobox(
            parent,
            textvariable=self.process_scope_var,
            values=("New messages", "Checked messages", "Pending / review / errors", "All messages"),
            state="readonly",
        )
        scope.pack(side="bottom", fill="x", pady=(4, 6))
        scope_row = ttk.Frame(parent)
        scope_row.pack(side="bottom", fill="x")
        ttk.Label(scope_row, text="MESSAGE SCOPE", style="Muted.TLabel").pack(side="left")
        ttk.Label(scope_row, textvariable=self.token_estimate_var, style="Muted.TLabel").pack(side="right")
        ttk.Label(parent, text="TRANSLATE", style="Section.TLabel").pack(side="bottom", anchor="w", pady=(0, 6))
        ttk.Separator(parent).pack(side="bottom", fill="x", pady=(12, 10))

        ttk.Label(parent, text="MESSAGE DETAILS", style="Section.TLabel").pack(anchor="w")
        self.detail_number_label = ttk.Label(parent, text="Select a line", style="Muted.TLabel")
        self.detail_number_label.pack(anchor="w", pady=(0, 10))

        ttk.Label(parent, text="SOURCE TEXT", style="Muted.TLabel").pack(anchor="w")
        self.source_text = self._text_widget(parent, height=3, wrap="word")
        self.source_text.pack(fill="x", pady=(4, 9))
        self.source_text.configure(state="disabled")

        ttk.Label(parent, text="LINE CONTEXT", style="Muted.TLabel").pack(anchor="w")
        self.line_context_text = self._text_widget(parent, height=2, wrap="word")
        self.line_context_text.pack(fill="x", pady=(4, 9))

        ttk.Label(parent, text="TAGS (comma separated)", style="Muted.TLabel").pack(anchor="w")
        ttk.Entry(parent, textvariable=self.tags_var).pack(fill="x", pady=(4, 10))

        detail_language = ttk.Frame(parent)
        detail_language.pack(fill="x")
        ttk.Label(detail_language, text="TRANSLATION", style="Muted.TLabel").pack(side="left")
        self.detail_language_combo = ttk.Combobox(detail_language, textvariable=self.detail_language_var, state="readonly", width=21)
        self.detail_language_combo.pack(side="right")
        self.detail_language_combo.bind("<<ComboboxSelected>>", self._on_detail_language_changed)

        # Takes whatever vertical slack is left, so a tall window gives the field you
        # actually type in the extra room instead of leaving a gap.
        self.translation_text = self._text_widget(parent, height=4, wrap="word")
        self.translation_text.pack(fill="both", expand=True, pady=(4, 7))
        ttk.Label(parent, text="PLURALS (index=translation, one per line)", style="Muted.TLabel").pack(anchor="w")
        self.plural_text = self._text_widget(parent, height=2, wrap="word")
        self.plural_text.pack(fill="x", pady=(4, 8))

        status_row = ttk.Frame(parent)
        status_row.pack(fill="x", pady=(0, 8))
        ttk.Label(status_row, text="Status", style="Muted.TLabel").pack(side="left")
        ttk.Label(status_row, textvariable=self.detail_status_var).pack(side="right")

        ttk.Button(parent, text="Save manual correction", style="Accent.TButton", command=self.save_detail).pack(fill="x", pady=2)
        ttk.Button(parent, text="Reprocess this line", command=self.process_current_line).pack(fill="x", pady=2)
        ttk.Button(parent, text="Copy source to translation", command=self.copy_source_to_translation).pack(fill="x", pady=2)

    def _build_status_bar(self, parent: ttk.Frame) -> None:
        status = ttk.Frame(parent, style="Window.TFrame")
        status.pack(side="bottom", fill="x")
        ttk.Label(status, textvariable=self.status_var, style="Subtitle.TLabel").pack(side="left")
        self.cancel_button = ttk.Button(status, text="Cancel", style="Danger.TButton", command=self.cancel_translation)
        self.progress = ttk.Progressbar(status, variable=self.progress_var, maximum=100, length=280)
        self.progress.pack(side="right", padx=(8, 0))
        ttk.Label(status, textvariable=self.progress_text_var, style="Subtitle.TLabel").pack(side="right", padx=(10, 0))

    def _text_widget(self, parent: tk.Misc, **kwargs) -> tk.Text:
        kwargs.setdefault("width", 1)
        return tk.Text(
            parent,
            bg=COLORS["surface_alt"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            selectbackground=COLORS["accent"],
            selectforeground="#FFFFFF",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
            relief="flat",
            padx=8,
            pady=7,
            font=("Segoe UI", 10),
            undo=True,
            **kwargs,
        )

    def _target_languages(self) -> list:
        return self.project.target_languages() if self.project else []

    def _rebuild_language_list(self) -> None:
        for child in self.language_frame.winfo_children():
            child.destroy()
        self.language_enabled_vars.clear()
        # Without a project every language is offered; otherwise the source is hidden.
        available = (
            [(language.code, language.name) for language in self._target_languages()]
            if self.project
            else [(code, name) for code, name, _plural in LANGUAGES]
        )
        configured = {language.code: language for language in self.project.languages} if self.project else {}
        for row_index, (code, name) in enumerate(available):
            language = configured.get(code)
            enabled = tk.BooleanVar(value=(language.enabled or language.export_enabled) if language else False)
            self.language_enabled_vars[code] = enabled
            row = tk.Frame(self.language_frame, bg=COLORS["surface"], padx=7, pady=2)
            row.grid(row=row_index, column=0, sticky="ew")
            row.columnconfigure(0, weight=1)
            row.columnconfigure(1, minsize=70)
            label = tk.Label(row, text=f"{name}  ·  {code}", bg=COLORS["surface"], fg=COLORS["text"], font=("Segoe UI", 9), anchor="w")
            label.grid(row=0, column=0, sticky="ew")
            checkbox = tk.Checkbutton(
                row,
                variable=enabled,
                command=self._language_settings_changed,
                bg=COLORS["surface"],
                activebackground=COLORS["surface"],
                selectcolor=COLORS["surface_alt"],
                fg=COLORS["accent"],
                activeforeground=COLORS["accent"],
                highlightthickness=0,
                bd=0,
            )
            checkbox.grid(row=0, column=1)

    def _set_empty_state(self) -> None:
        self.project_name_var.set("No project")
        self.source_language_var.set("<AUTO>")
        self.provider_var.set("OpenAI")
        if hasattr(self, "model_combo"):
            self.model_combo.configure(values=PROVIDER_MODELS["openai"])
        self.model_var.set(PROVIDER_MODELS["openai"][0])
        self.quality_var.set("Balanced")
        self.batch_size_var.set(30)
        self.max_ui_words_var.set(DEFAULT_MAX_UI_WORDS)
        self.global_context_text.delete("1.0", "end")
        self.language_context_code = ""
        self.language_context_label_var.set("LANGUAGE CONTEXT")
        self.language_context_text.delete("1.0", "end")
        self._rebuild_language_list()
        self._refresh_tree()
        self._clear_detail()
        self.status_var.set("Create a project or open an existing one to begin.")

    def _load_project_into_ui(self) -> None:
        if not self.project:
            self._set_empty_state()
            return
        settings = self.project.settings
        self.project_name_var.set(self.project.name)
        self.source_language_var.set(SOURCE_CODES.get(settings.source_language, settings.source_language))
        provider_key = settings.provider if settings.provider in PROVIDER_MODELS else "openai"
        self.provider_var.set(PROVIDER_NAMES.get(provider_key, "OpenAI"))
        if hasattr(self, "model_combo"):
            self.model_combo.configure(values=PROVIDER_MODELS[provider_key])
        self.model_var.set(settings.model)
        self.batch_size_var.set(settings.batch_size)
        self.max_chars_var.set(settings.max_batch_chars)
        self.max_ui_words_var.set(settings.max_ui_words)
        self.quality_var.set(QUALITY_CODES.get(settings.quality_profile, "Balanced"))
        self.global_context_text.delete("1.0", "end")
        self.global_context_text.insert("1.0", settings.global_context)
        self._rebuild_language_list()
        targets = self._target_languages()
        enabled = [language for language in targets if language.enabled]
        chosen = enabled[0] if enabled else targets[0]
        self.language_context_code = ""
        self.detail_language_var.set(self._language_display(chosen.code))
        self._load_language_context()
        self.detail_language_combo.configure(values=[self._language_display(language.code) for language in targets])
        self.selected_uids.clear()
        self.current_uid = ""
        self.range_to_var.set(str(max(1, len(self.project.active_entries()))))
        self._refresh_tree()
        self._clear_detail()
        self._update_token_estimate()
        self.status_var.set(f"Project loaded: {self.project.name}")

    def _language_display(self, code: str) -> str:
        if self.project:
            language = self.project.language(code)
            if language:
                return f"{language.name} [{language.code}]"
        for item_code, name, _plural in LANGUAGES:
            if item_code == code:
                return f"{name} [{code}]"
        return code

    def _detail_language_code(self) -> str:
        value = self.detail_language_var.get()
        match = re.search(r"\[([^\]]+)\]\s*$", value)
        return match.group(1) if match else value

    def _flush_language_context(self) -> None:
        if not self.project or not self.language_context_code:
            return
        language = self.project.language(self.language_context_code)
        if language:
            language.context = self.language_context_text.get("1.0", "end-1c").strip()

    def _load_language_context(self) -> None:
        code = self._detail_language_code()
        self.language_context_code = code
        language = self.project.language(code) if self.project else None
        self.language_context_label_var.set(
            f"CONTEXT · {language.name.upper()}" if language else "LANGUAGE CONTEXT"
        )
        self.language_context_text.delete("1.0", "end")
        if language:
            self.language_context_text.insert("1.0", language.context)

    def _on_source_language_changed(self, _event: tk.Event | None = None) -> None:
        if self.project:
            # Apply it before rebuilding, since the list is filtered by this value.
            self.project.settings.source_language = SOURCE_LABELS.get(
                self.source_language_var.get(), self.source_language_var.get()
            )
            self._rebuild_language_list()
            self._retarget_detail_language()
            self._refresh_tree()
        self._settings_changed()

    def _retarget_detail_language(self) -> None:
        """Move the details panel off the source language when it becomes the source."""
        targets = self._target_languages()
        if not targets or any(language.code == self._detail_language_code() for language in targets):
            return
        preferred = next((language for language in targets if language.enabled), targets[0])
        self._flush_language_context()
        self.detail_language_var.set(self._language_display(preferred.code))
        self.detail_language_combo.configure(
            values=[self._language_display(language.code) for language in targets]
        )
        self._load_language_context()
        self._load_detail_translation(refresh_tree=False)

    def _language_settings_changed(self) -> None:
        self._settings_changed()
        if self.project:
            enabled = [code for code, var in self.language_enabled_vars.items() if var.get()]
            if enabled and self._detail_language_code() not in enabled:
                self._flush_language_context()
                self.detail_language_var.set(self._language_display(enabled[0]))
                self._load_language_context()
                self._load_detail_translation()
            self._refresh_tree()

    def _current_provider_key(self) -> str:
        return PROVIDER_KEYS.get(self.provider_var.get(), "openai")

    def _on_provider_changed(self, _event: object = None) -> None:
        provider_key = self._current_provider_key()
        models = PROVIDER_MODELS.get(provider_key, ())
        if hasattr(self, "model_combo"):
            self.model_combo.configure(values=models)
        if models and self.model_var.get() not in models:
            self.model_var.set(models[0])
        self._settings_changed()

    def _settings_changed(self) -> None:
        if not self.project:
            return
        self._mark_dirty()
        self._update_token_estimate()

    def _sync_ui_to_project(self) -> None:
        if not self.project:
            return
        settings = self.project.settings
        settings.source_language = SOURCE_LABELS.get(self.source_language_var.get(), self.source_language_var.get())
        settings.provider = self._current_provider_key()
        settings.model = self.model_var.get().strip() or PROVIDER_MODELS[settings.provider][0]
        try:
            settings.batch_size = max(1, min(100, int(self.batch_size_var.get())))
        except (ValueError, tk.TclError):
            settings.batch_size = 30
            self.batch_size_var.set(30)
        try:
            settings.max_batch_chars = max(500, int(self.max_chars_var.get()))
        except (ValueError, tk.TclError):
            settings.max_batch_chars = 12000
        try:
            settings.max_ui_words = max(0, min(20, int(self.max_ui_words_var.get())))
        except (ValueError, tk.TclError):
            settings.max_ui_words = DEFAULT_MAX_UI_WORDS
            self.max_ui_words_var.set(DEFAULT_MAX_UI_WORDS)
        settings.quality_profile = QUALITY_LABELS.get(self.quality_var.get(), "balanced")
        settings.global_context = self.global_context_text.get("1.0", "end-1c").strip()
        self._flush_language_context()
        for language in self.project.languages:
            variable = self.language_enabled_vars.get(language.code)
            # A language hidden for being the source must not stay queued for work.
            language.enabled = variable.get() if variable else False
            language.export_enabled = language.enabled
        self.project.touch()

    def _mark_dirty(self) -> None:
        if not self.project:
            return
        self.dirty = True
        title = f"{APP_NAME} — {self.project.name} •"
        self.title(title)
        if self.autosave_job:
            self.after_cancel(self.autosave_job)
        self.autosave_job = self.after(1500, self._autosave)

    def _autosave(self) -> None:
        self.autosave_job = None
        if self.dirty and self.project and self.project_path and not self._is_busy():
            self.save_project(silent=True)

    def _is_busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _can_switch_project(self) -> bool:
        if self._is_busy():
            messagebox.showwarning(APP_NAME, "Cancel or wait for the current translation to finish.", parent=self)
            return False
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel("Switch project", "There are unsaved changes. Do you want to save them?", parent=self)
        if answer is None:
            return False
        if answer:
            return self.save_project()
        return True

    def new_project(self) -> None:
        if not self._can_switch_project():
            return
        folder = filedialog.askdirectory(title="Select the project workspace folder", parent=self)
        if not folder:
            return
        name = simpledialog.askstring("New project", "Project name:", initialvalue=Path(folder).name, parent=self)
        if not name or not name.strip():
            return
        clean_name = re.sub(r"[^\w .-]+", "_", name.strip(), flags=re.UNICODE).strip(" .") or "Localization"
        self.project = Project(name=name.strip())
        self.project_path = Path(folder) / f"{clean_name}{PROJECT_EXTENSION}"
        (Path(folder) / "PO_Files").mkdir(parents=True, exist_ok=True)
        self.dirty = True
        self._load_project_into_ui()
        self.save_project(silent=True)
        self.status_var.set("Project created. Now import the English base PO.")

    def open_project(self) -> None:
        if not self._can_switch_project():
            return
        path = filedialog.askopenfilename(
            title="Open PoTranslator project",
            filetypes=[("PoTranslator project", f"*{PROJECT_EXTENSION}"), ("All files", "*.*")],
            parent=self,
        )
        if not path:
            return
        try:
            project = read_project_file(path)
        except ProjectStoreError as exc:
            messagebox.showerror("Could not open project", str(exc), parent=self)
            return
        self.project = project
        self.project_path = Path(path)
        self.dirty = False
        self.title(f"{APP_NAME} — {project.name}")
        self._load_project_into_ui()

    def save_project(self, silent: bool = False) -> bool:
        if not self.project:
            if not silent:
                messagebox.showinfo(APP_NAME, "There is no project to save.", parent=self)
            return False
        if not self.project_path:
            return self.save_project_as()
        try:
            self._sync_ui_to_project()
            self.project_path = write_project_file(self.project, self.project_path)
        except (OSError, ValueError) as exc:
            if not silent:
                messagebox.showerror("Save error", str(exc), parent=self)
            return False
        self.dirty = False
        self.title(f"{APP_NAME} — {self.project.name}")
        if not silent:
            self.status_var.set(f"Saved: {self.project_path.name}")
        return True

    def save_project_as(self) -> bool:
        if not self.project:
            return False
        path = filedialog.asksaveasfilename(
            title="Save project as",
            defaultextension=PROJECT_EXTENSION,
            initialfile=f"{self.project.name}{PROJECT_EXTENSION}",
            filetypes=[("PoTranslator project", f"*{PROJECT_EXTENSION}")],
            parent=self,
        )
        if not path:
            return False
        self.project_path = Path(path)
        self.dirty = True
        return self.save_project()

    def import_base(self) -> None:
        path = filedialog.askopenfilename(
            title="Import or update base catalog",
            filetypes=[("Portable Object", "*.po *.pot"), ("All files", "*.*")],
            parent=self,
        )
        if not path:
            return
        if not self.project:
            folder = filedialog.askdirectory(
                title="Select the folder where the project will be saved",
                initialdir=str(Path(path).parent),
                parent=self,
            )
            if not folder:
                return
            name = Path(path).stem
            self.project = Project(name=name)
            self.project_path = Path(folder) / f"{name}{PROJECT_EXTENSION}"
            (Path(folder) / "PO_Files").mkdir(parents=True, exist_ok=True)
        try:
            counts = import_base_po(self.project, path)
        except (OSError, POParseError, ValueError) as exc:
            messagebox.showerror("Invalid PO", str(exc), parent=self)
            return
        self.dirty = True
        self._load_project_into_ui()
        self.save_project(silent=True)
        messagebox.showinfo(
            "Base imported",
            f"New: {counts['new']}\nModified: {counts['modified']}\nUnchanged: {counts['unchanged']}\nRemoved: {counts['removed']}",
            parent=self,
        )

    def import_translation(self) -> None:
        if not self.project or not self.project.active_entries():
            messagebox.showinfo(APP_NAME, "Import the base PO first.", parent=self)
            return
        code = self._choose_language("Translated PO language")
        if not code:
            return
        path = filedialog.askopenfilename(
            title=f"Import {code} translation",
            filetypes=[("Portable Object", "*.po"), ("All files", "*.*")],
            parent=self,
        )
        if not path:
            return
        try:
            counts = import_translation_po(self.project, code, path)
        except (OSError, POParseError, ValueError) as exc:
            messagebox.showerror("Could not import translation", str(exc), parent=self)
            return
        language = self.project.language(code)
        if language:
            language.enabled = True
            language.export_enabled = True
        self.dirty = True
        self._load_project_into_ui()
        self.detail_language_var.set(self._language_display(code))
        self._refresh_tree()
        self.save_project(silent=True)
        messagebox.showinfo(
            "Translation imported",
            f"Imported: {counts['imported']}\nUnmatched: {counts['unmatched']}\nMissing: {counts['missing']}",
            parent=self,
        )

    def _choose_language(self, title: str) -> str:
        if not self.project:
            return ""
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.configure(bg=COLORS["panel"])
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)
        targets = self._target_languages()
        value = tk.StringVar(value=self._language_display(targets[0].code))
        result = {"code": ""}
        ttk.Label(dialog, text="Select the language:", style="Section.TLabel").pack(anchor="w", padx=18, pady=(16, 7))
        combo = ttk.Combobox(dialog, textvariable=value, values=[self._language_display(language.code) for language in targets], state="readonly", width=34)
        combo.pack(padx=18, pady=(0, 13))
        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=18, pady=(0, 16))

        def accept() -> None:
            match = re.search(r"\[([^\]]+)\]\s*$", value.get())
            result["code"] = match.group(1) if match else value.get()
            dialog.destroy()

        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="OK", style="Accent.TButton", command=accept).pack(side="right", padx=(0, 6))
        dialog.bind("<Return>", lambda _event: accept())
        self.wait_window(dialog)
        return result["code"]

    def export_languages(self, export_all: bool = False) -> None:
        if not self.project or not self.project_path:
            messagebox.showinfo(APP_NAME, "Open or create a project before exporting.", parent=self)
            return
        self._sync_ui_to_project()
        targets = self._target_languages()
        languages = targets if export_all else [language for language in targets if language.enabled]
        if not languages:
            messagebox.showwarning("Export", "Check at least one language in the Selected column.", parent=self)
            return
        output = self.project_path.parent / "PO_Files"
        errors: list[str] = []
        for language in languages:
            try:
                export_language_po(self.project, language.code, output / f"{language.code}.po")
            except (OSError, ValueError) as exc:
                errors.append(f"{language.code}: {exc}")
        self.project.history.append({"at": utc_now(), "action": "export", "languages": [language.code for language in languages]})
        self._mark_dirty()
        self.save_project(silent=True)
        if errors:
            messagebox.showerror("Partial export", "\n".join(errors), parent=self)
        else:
            messagebox.showinfo("Export complete", f"{len(languages)} file(s) created in:\n{output}", parent=self)

    def _entry_progress(self, entry: SourceEntry, code: str) -> str:
        record = entry.translations.get(code, TranslationRecord())
        return {
            "pending": "Pending",
            "translated": "Ready",
            "review": "Review",
            "manual": "Manual",
            "error": "Error",
        }.get(record.status, "Pending")

    def _entry_state_label(self, entry: SourceEntry, code: str) -> str:
        state = entry_language_state(entry, code)
        return {
            "new": "New",
            "modified": "Modified",
            "unchanged": "Unchanged",
            "removed": "Removed",
        }.get(state, state)

    def _entry_matches_filters(self, entry: SourceEntry, code: str) -> bool:
        query = self.search_var.get().strip().casefold()
        if query:
            record = entry.translations.get(code, TranslationRecord())
            haystack = "\n".join(
                (
                    entry.msgid,
                    entry.msgctxt,
                    entry.line_context,
                    " ".join(entry.tags),
                    record.text,
                    " ".join(record.plurals.values()),
                )
            ).casefold()
            if query not in haystack:
                return False
        tag_query = self.tag_filter_var.get().strip().casefold()
        if tag_query and tag_query not in {tag.casefold() for tag in entry.tags}:
            return False
        status_filter = self.status_filter_var.get()
        record = entry.translations.get(code, TranslationRecord())
        if status_filter in {"New", "Modified"} and entry_language_state(entry, code) != status_filter.casefold():
            return False
        if status_filter == "Pending" and record.status != "pending":
            return False
        if status_filter == "Review" and record.status != "review":
            return False
        if status_filter == "Errors" and record.status != "error":
            return False
        if status_filter == "Ready" and record.status not in {"translated", "manual"}:
            return False
        return True

    def _refresh_tree(self) -> None:
        if not hasattr(self, "tree"):
            return
        old_yview = self.tree.yview()
        self.tree.delete(*self.tree.get_children())
        self.visible_uids = []
        if not self.project:
            self.tree.heading("translation", text="Translation")
            self.summary_var.set("0 messages")
            return
        code = self._detail_language_code() or self._target_languages()[0].code
        language = self.project.language(code)
        language_name = language.name if language else code
        self.tree.heading("translation", text=f"Translation · {language_name}")
        active_number = 0
        for entry in self.project.entries:
            if not entry.active:
                continue
            active_number += 1
            if not self._entry_matches_filters(entry, code):
                continue
            self.visible_uids.append(entry.uid)
            record = entry.translations.get(code, TranslationRecord())
            row_tag = "error" if record.status == "error" else entry_language_state(entry, code)
            self.tree.insert(
                "",
                "end",
                iid=entry.uid,
                values=(
                    "●" if entry.uid in self.selected_uids else "○",
                    active_number,
                    entry.msgid.replace("\n", " ↵ "),
                    self._translation_preview(entry, code),
                    self._entry_state_label(entry, code),
                    self._entry_progress(entry, code),
                ),
                tags=(row_tag,),
            )
        stats = self.project.stats(code)
        shown = len(self.visible_uids)
        self.summary_var.set(
            f"{shown}/{stats['total']} messages  ·  {stats['translated']} ready  ·  {stats['pending']} pending  ·  {stats['review']} review  ·  {stats['error']} errors"
        )
        if old_yview and self.tree.get_children():
            self.tree.yview_moveto(old_yview[0])

    def _refresh_tree_row(self, uid: str) -> None:
        if not self.project or not self.tree.exists(uid):
            return
        entry = next((item for item in self.project.entries if item.uid == uid), None)
        if not entry:
            return
        code = self._detail_language_code() or self._target_languages()[0].code
        values = list(self.tree.item(uid, "values"))
        if len(values) >= 6:
            values[0] = "●" if uid in self.selected_uids else "○"
            values[3] = self._translation_preview(entry, code)
            values[4] = self._entry_state_label(entry, code)
            values[5] = self._entry_progress(entry, code)
            record = entry.translations.get(code, TranslationRecord())
            tag = "error" if record.status == "error" else entry_language_state(entry, code)
            self.tree.item(uid, values=values, tags=(tag,))

    def _on_tree_click(self, event: tk.Event) -> str | None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        row = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not row:
            return None
        if column == "#1":
            if row in self.selected_uids:
                self.selected_uids.remove(row)
            else:
                self.selected_uids.add(row)
            self.tree.selection_set(row)
            self.tree.focus(row)
            self._refresh_tree_row(row)
            self._show_detail(row)
            self._update_token_estimate()
            return "break"
        return None

    def _on_tree_selection(self, _event: tk.Event | None = None) -> None:
        focus = self.tree.focus()
        if not focus:
            selection = self.tree.selection()
            focus = selection[-1] if selection else ""
        if focus:
            self._show_detail(focus)
        self._update_token_estimate()

    def _entry_by_uid(self, uid: str) -> SourceEntry | None:
        if not self.project:
            return None
        return next((entry for entry in self.project.entries if entry.uid == uid), None)

    def _show_detail(self, uid: str) -> None:
        entry = self._entry_by_uid(uid)
        if not entry:
            return
        self.current_uid = uid
        try:
            self.current_number = self.project.active_entries().index(entry) + 1 if self.project else 0
        except ValueError:
            self.current_number = 0
        self.source_text.configure(state="normal")
        self.source_text.delete("1.0", "end")
        self.source_text.insert("1.0", entry.msgid + (f"\n\nPlural: {entry.msgid_plural}" if entry.msgid_plural else ""))
        self.source_text.configure(state="disabled")
        self.line_context_text.delete("1.0", "end")
        self.line_context_text.insert("1.0", entry.line_context)
        self.tags_var.set(", ".join(entry.tags))
        self._load_detail_translation()

    def _translation_preview(self, entry: SourceEntry, code: str) -> str:
        record = entry.translations.get(code, TranslationRecord())
        text = record.text or " / ".join(
            value for _index, value in sorted(record.plurals.items(), key=lambda item: int(item[0])) if value
        )
        return text.replace("\n", " ↵ ") if text else "—"

    def _on_detail_language_changed(self, _event: tk.Event | None = None) -> None:
        self._flush_language_context()
        self._load_language_context()
        self._load_detail_translation(refresh_tree=False)
        self._refresh_tree()

    def _load_detail_translation(self, refresh_tree: bool = True) -> None:
        entry = self._entry_by_uid(self.current_uid)
        if not entry:
            return
        code = self._detail_language_code()
        record = entry.translations.get(code, TranslationRecord())
        # The change state is language-specific, so it has to follow the picker too.
        self.detail_number_label.configure(
            text=f"Message #{self.current_number}  ·  {self._entry_state_label(entry, code).casefold()}"
        )
        self.translation_text.delete("1.0", "end")
        self.translation_text.insert("1.0", record.text)
        self.plural_text.delete("1.0", "end")
        self.plural_text.insert("1.0", "\n".join(f"{index}={text}" for index, text in sorted(record.plurals.items(), key=lambda item: int(item[0]))))
        status = self._entry_progress(entry, code)
        if record.error:
            status += f" · {record.error}"
        self.detail_status_var.set(status)
        if refresh_tree:
            self._refresh_tree()

    def _clear_detail(self) -> None:
        self.current_uid = ""
        self.current_number = 0
        self.detail_number_label.configure(text="Select a line")
        self.source_text.configure(state="normal")
        self.source_text.delete("1.0", "end")
        self.source_text.configure(state="disabled")
        self.line_context_text.delete("1.0", "end")
        self.translation_text.delete("1.0", "end")
        self.plural_text.delete("1.0", "end")
        self.tags_var.set("")
        self.detail_status_var.set("—")

    def save_detail(self, silent: bool = False) -> bool:
        entry = self._entry_by_uid(self.current_uid)
        if not entry:
            if not silent:
                messagebox.showinfo(APP_NAME, "Select a line.", parent=self)
            return False
        code = self._detail_language_code()
        if not code:
            return False
        entry.line_context = self.line_context_text.get("1.0", "end-1c").strip()
        entry.tags = list(dict.fromkeys(tag.strip() for tag in self.tags_var.get().split(",") if tag.strip()))
        text = self.translation_text.get("1.0", "end-1c")
        plurals: dict[str, str] = {}
        for line in self.plural_text.get("1.0", "end-1c").splitlines():
            if not line.strip():
                continue
            if "=" not in line:
                if not silent:
                    messagebox.showerror("Invalid plural", f"Use the index=translation format:\n{line}", parent=self)
                return False
            index, value = line.split("=", 1)
            if not index.strip().isdigit():
                if not silent:
                    messagebox.showerror("Invalid plural", f"Non-numeric index: {index}", parent=self)
                return False
            plurals[index.strip()] = value
        record = entry.translations.setdefault(code, TranslationRecord())
        record.text = text
        record.plurals = plurals
        record.status = "manual" if text or plurals else "pending"
        record.needs_review = False
        record.error = ""
        record.updated_at = utc_now()
        record.source_hash = entry.source_hash
        record.model = "manual"
        self._mark_dirty()
        self._refresh_tree_row(entry.uid)
        self.detail_number_label.configure(
            text=f"Message #{self.current_number}  ·  {self._entry_state_label(entry, code).casefold()}"
        )
        self.detail_status_var.set("Manual" if text or plurals else "Pending")
        if not silent:
            self.status_var.set("Correction, context, and tags saved.")
        return True

    def copy_source_to_translation(self) -> None:
        entry = self._entry_by_uid(self.current_uid)
        if not entry:
            return
        self.translation_text.delete("1.0", "end")
        self.translation_text.insert("1.0", entry.msgid)

    def _chosen_uids(self) -> set[str]:
        return self.selected_uids | set(self.tree.selection())

    def select_range(self) -> None:
        if not self.visible_uids:
            return
        try:
            start = int(self.range_from_var.get())
            end = int(self.range_to_var.get())
        except ValueError:
            messagebox.showerror("Invalid range", "From and To must be numbers.", parent=self)
            return
        if start < 1 or end < start or end > len(self.visible_uids):
            messagebox.showerror("Invalid range", f"Use a range between 1 and {len(self.visible_uids)}.", parent=self)
            return
        self.selected_uids.update(self.visible_uids[start - 1 : end])
        self._refresh_tree()
        self._update_token_estimate()

    def select_all_visible(self) -> None:
        self.selected_uids.update(self.visible_uids)
        self._refresh_tree()
        self._update_token_estimate()

    def clear_selection(self) -> None:
        self.selected_uids.clear()
        self.tree.selection_remove(*self.tree.selection())
        self._refresh_tree()
        self._update_token_estimate()

    def tag_selected(self) -> None:
        entries = [entry for uid in self._chosen_uids() if (entry := self._entry_by_uid(uid))]
        if not entries:
            messagebox.showinfo("Tag messages", "Check one or more lines.", parent=self)
            return
        tag = simpledialog.askstring("Tag messages", "Tag:", parent=self)
        if not tag or not tag.strip():
            return
        normalized = tag.strip()
        for entry in entries:
            if normalized.casefold() not in {item.casefold() for item in entry.tags}:
                entry.tags.append(normalized)
        self._mark_dirty()
        self._refresh_tree()

    def mark_selected_pending(self) -> None:
        entries = [entry for uid in self._chosen_uids() if (entry := self._entry_by_uid(uid))]
        if not entries:
            messagebox.showinfo("Reprocess", "Check one or more lines.", parent=self)
            return
        codes = [code for code, var in self.language_enabled_vars.items() if var.get()]
        if not codes:
            code = self._detail_language_code()
            codes = [code] if code else []
        for entry in entries:
            for code in codes:
                record = entry.translations.setdefault(code, TranslationRecord())
                record.status = "pending"
                record.needs_review = False
                record.error = ""
        self._mark_dirty()
        self._refresh_tree()
        self.status_var.set(f"{len(entries)} message(s) marked for reprocessing.")

    def _update_token_estimate(self) -> None:
        if not self.project:
            self.token_estimate_var.set("≈ 0 tokens")
            return
        chosen = self._chosen_uids()
        entries = [entry for entry in self.project.active_entries() if not chosen or entry.uid in chosen]
        languages = sum(variable.get() for variable in self.language_enabled_vars.values())
        estimate = estimate_tokens(entries, self.global_context_text.get("1.0", "end-1c")) * max(1, languages)
        self.token_estimate_var.set(f"≈ {estimate:,} input tokens")

    def process_current_line(self) -> None:
        entry = self._entry_by_uid(self.current_uid)
        if not entry:
            return
        code = self._detail_language_code()
        if not code:
            messagebox.showwarning(APP_NAME, "Select a language in the Translation panel first.", parent=self)
            return
        self.save_detail(silent=True)
        self._start_translation_entries([entry], skip_completed=False, language_codes=[code])

    def start_translation(self, mode: str | None = None, all_languages: bool = False) -> None:
        if not self.project:
            messagebox.showinfo(APP_NAME, "Create a project and import the base PO first.", parent=self)
            return
        if not self.project.active_entries():
            messagebox.showinfo(APP_NAME, "The project has no source messages.", parent=self)
            return
        scope_modes = {
            "New messages": "new",
            "Checked messages": "selected",
            "Pending / review / errors": "pending",
            "All messages": "all",
        }
        mode = mode or scope_modes.get(self.process_scope_var.get(), "new")
        codes = (
            [language.code for language in self._target_languages()]
            if all_languages
            else [code for code, variable in self.language_enabled_vars.items() if variable.get()]
        )
        if not codes:
            messagebox.showwarning("Languages", "Check at least one language in the Selected column.", parent=self)
            return
        selected = self._chosen_uids()
        entries = select_entries(self.project.entries, mode, selected, codes)
        if not entries:
            messagebox.showinfo("No work", "No messages match that criterion.", parent=self)
            return
        if mode == "all":
            estimate = estimate_tokens(entries, self.project.settings.global_context) * len(codes)
            if not messagebox.askyesno(
                "Reprocess all",
                f"{len(entries)} messages will be translated again into {len(codes)} language(s).\nEstimated input: {estimate:,} tokens.\n\nContinue?",
                parent=self,
            ):
                return
        self._start_translation_entries(
            entries,
            skip_completed=mode in {"new", "pending"},
            language_codes=codes,
        )

    def _start_translation_entries(
        self,
        entries: list[SourceEntry],
        skip_completed: bool,
        language_codes: list[str] | None = None,
    ) -> None:
        if self._is_busy():
            messagebox.showwarning(APP_NAME, "A translation is already running.", parent=self)
            return
        if not self.project:
            return
        self._sync_ui_to_project()
        codes = list(language_codes or [language.code for language in self._target_languages() if language.enabled])
        if not codes:
            current = self._detail_language_code()
            codes = [current] if current else []
        provider_key = self._current_provider_key()
        provider_label = PROVIDER_NAMES.get(provider_key, provider_key)
        api_key = load_api_key(provider_key)
        if not api_key:
            if not self.configure_api_key(provider_key):
                return
            api_key = load_api_key(provider_key)
        if not api_key:
            return
        try:
            if provider_key == "anthropic":
                client = AnthropicTranslationClient(api_key)
            else:
                client = OpenAITranslationClient(api_key)
        except TranslationError as exc:
            messagebox.showerror(provider_label, str(exc), parent=self)
            return
        self.save_project(silent=True)
        self.cancel_event.clear()
        self.progress_var.set(0)
        self.cancel_button.pack(side="right", padx=(8, 0))
        self.status_var.set("Preparing translations…")
        runner = TranslationRunner(client)

        def work() -> None:
            try:
                runner.run(
                    self.project,
                    entries,
                    codes,
                    self.cancel_event,
                    self.worker_events.put,
                    skip_completed=skip_completed,
                )
            except Exception as exc:  # Last-resort boundary for worker failures.
                self.worker_events.put({"type": "fatal", "message": str(exc)})

        self.worker = threading.Thread(target=work, name="PoTranslatorWorker", daemon=True)
        self.worker.start()

    def cancel_translation(self) -> None:
        if self._is_busy():
            self.cancel_event.set()
            self.status_var.set("Cancelling after the current request…")

    def _poll_worker_events(self) -> None:
        try:
            while True:
                event = self.worker_events.get_nowait()
                kind = event.get("type")
                if kind == "started":
                    total = max(1, event.get("total", 1))
                    self.progress.configure(maximum=total)
                    self.progress_var.set(0)
                    self.progress_text_var.set(f"0 / {total}")
                    reused = int(event.get("duplicates_reused", 0) or 0)
                    reuse_note = f"; reusing {reused} duplicate(s)" if reused else ""
                    self.status_var.set(
                        f"Translating {event.get('entries', 0)} messages into "
                        f"{event.get('languages', 0)} language(s){reuse_note}…"
                    )
                elif kind == "language":
                    self.status_var.set(f"Processing {event.get('name')}…")
                elif kind == "batch":
                    self.status_var.set(
                        f"{event.get('language')} · batch {event.get('index')}/{event.get('count')} · {event.get('size')} messages"
                    )
                elif kind == "retry":
                    self.status_var.set(f"Retry {event.get('attempt')}: {event.get('message')}")
                elif kind == "split":
                    self.status_var.set(
                        f"Batch too large; splitting {event.get('size')} messages into "
                        f"{event.get('first')} + {event.get('second')}…"
                    )
                elif kind == "entry":
                    completed = event.get("completed", 0)
                    total = event.get("total", 0)
                    self.progress_var.set(completed)
                    percent = round(100 * completed / total) if total else 0
                    self.progress_text_var.set(f"{completed} / {total}  ·  {percent}%")
                    self._refresh_tree_row(str(event.get("uid", "")))
                    if str(event.get("uid", "")) == self.current_uid and event.get("language") == self._detail_language_code():
                        self._load_detail_translation(refresh_tree=False)
                elif kind in {"finished", "cancelled"}:
                    self._finish_translation(cancelled=kind == "cancelled")
                elif kind == "fatal":
                    self._finish_translation(error=str(event.get("message", "Unknown error")))
        except queue.Empty:
            pass
        self.after(120, self._poll_worker_events)

    def _finish_translation(self, cancelled: bool = False, error: str = "") -> None:
        self.cancel_button.pack_forget()
        self.progress_text_var.set("")
        self.worker = None
        self._mark_dirty()
        self.save_project(silent=True)
        self._refresh_tree()
        self._load_detail_translation(refresh_tree=False)
        if error:
            self.status_var.set(f"Error: {error}")
            messagebox.showerror("Translation error", error, parent=self)
        elif cancelled:
            self.status_var.set("Translation cancelled; partial progress was saved.")
        else:
            self.status_var.set("Translation completed and project saved.")

    def configure_api_key(self, provider: str | None = None) -> bool:
        provider_key = provider or self._current_provider_key()
        provider_label = PROVIDER_NAMES.get(provider_key, provider_key)
        existing = load_api_key(provider_key)
        hint = f"A stored key ending in …{existing[-4:]} is available.\n\n" if existing else ""
        value = simpledialog.askstring(
            f"{provider_label} API key",
            hint + f"Enter a new {provider_label} API key. It will be encrypted for your Windows user and will never be stored in the project:",
            show="•",
            parent=self,
        )
        if value is None:
            return bool(existing)
        try:
            save_api_key(provider_key, value)
        except CredentialError as exc:
            messagebox.showerror("Could not save API key", str(exc), parent=self)
            return False
        self.status_var.set(f"{provider_label} API key stored with Windows DPAPI encryption.")
        return True

    def remove_api_key(self, provider: str | None = None) -> None:
        provider_key = provider or self._current_provider_key()
        provider_label = PROVIDER_NAMES.get(provider_key, provider_key)
        if messagebox.askyesno(
            "Remove API key",
            f"Remove the encrypted {provider_label} API key stored for this Windows user?",
            parent=self,
        ):
            try:
                delete_api_key(provider_key)
            except OSError as exc:
                messagebox.showerror("Could not remove API key", str(exc), parent=self)
                return
            self.status_var.set(f"{provider_label} API key removed.")

    def _on_close(self) -> None:
        if self._is_busy():
            if not messagebox.askyesno("Exit", "A translation is running. Cancel it and exit?", parent=self):
                return
            self.cancel_event.set()
        if self.dirty and self.project:
            answer = messagebox.askyesnocancel("Exit", "Save changes before exiting?", parent=self)
            if answer is None:
                return
            if answer and not self.save_project():
                return
        self.destroy()


def main() -> None:
    app = PoTranslatorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
