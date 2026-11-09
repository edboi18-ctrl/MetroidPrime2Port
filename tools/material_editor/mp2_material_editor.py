#!/usr/bin/env python3
"""MP2 Material Editor: the port's per-material settings (PBR maps, how they're read, live reflections),
picked from the disc's own worlds, areas and models, saved to materials.json in the mods folder.

Run it beside the game (the Windows build ships mp2_material_editor.exe next to
metroid_prime2_port.exe): it reads the game's settings.cfg for the disc image and the mods folder.
Changes are saved as you make them, and the running game picks them up within a couple of seconds;
the 3D preview (viewer.py) shows them at once. Docs: docs/MATERIAL_EDITOR.md. The workings (disc,
keys, textures, files) are in material_core.py, the preview's geometry in geometry.py and preview.py.
"""

import argparse
import base64
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import material_core as mc  # noqa: E402
import preview  # noqa: E402
import viewer  # noqa: E402

TITLE = "MP2 Material Editor"
THUMB = 96
SAVE_DELAY_MS = 350


def game_folder():
    """The folder the editor runs from: the .exe's when frozen, else the repo's build output isn't
    known, so the current folder."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.getcwd()


class Editor(tk.Tk):
    def __init__(self, args):
        super().__init__()
        self.title(TITLE)
        width = min(1560, max(1100, self.winfo_screenwidth() - 80))
        height = min(960, max(680, self.winfo_screenheight() - 100))
        self.geometry("%dx%d" % (width, height))
        self.minsize(1050, 640)
        self.library = None
        self.overrides = None
        self.pack = None  # material_core.TexturePack: the folders the game reads its pack from
        self.selected = None  # (source label, [Material]) shown in the list
        self.material = None  # the Material being edited
        self.thumbs = []
        self.jobs = queue.Queue()
        self.save_after = None
        self.loading_fields = False
        self.clipboard = None
        self.view = None  # the 3D preview (None without OpenGL)
        self.preview_token = 0
        self.source = None  # (kind, id) of the area or model listed

        self.game = args.game or game_folder()
        self.user = args.user or mc.find_user_folder(self.game)
        self.settings = mc.read_settings(self.user)
        self.mods = mc.mods_folder(self.user)
        # The game's settings the preview follows (settings.cfg; the game's defaults otherwise).
        self.view_settings = {
            "pbr_maps": mc.setting_bool(self.settings.get("pbr_maps"), False),
            "live_reflections": mc.setting_bool(self.settings.get("live_reflections"), False),
            "pbr_highlight": preview.setting_percent(self.settings, "pbr_highlight", 100),
            "luminance": preview.setting_percent(self.settings, "luminance", 100),
        }
        self._build()
        self._set_overrides(args.output or mc.overrides_path(self.mods))
        disc = args.disc or mc.find_disc(self.settings, self.game)
        if disc:
            self.disc_var.set(disc)
            self.after(100, self.open_disc)
        else:
            self.status("Choose your Metroid Prime 2: Echoes disc image (.iso, .gcm or .ciso) with Browse.")
        self.after(100, self._poll_jobs)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ------------------------------------------------------------------------------------ layout
    def _build(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        top = ttk.Frame(self, padding=(8, 8, 8, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Disc image:").grid(row=0, column=0, sticky="w")
        self.disc_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.disc_var).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(top, text="Browse...", command=self.browse_disc).grid(row=0, column=2)
        ttk.Button(top, text="Open", command=self.open_disc).grid(row=0, column=3, padx=(4, 0))
        ttk.Label(top, text="Saving to:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.out_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.out_var, state="readonly").grid(row=1, column=1, sticky="ew", padx=4, pady=(4, 0))
        ttk.Button(top, text="Change...", command=self.choose_output).grid(row=1, column=2, pady=(4, 0))
        self.autosave = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Save as I change", variable=self.autosave).grid(row=1, column=3, padx=(4, 0), pady=(4, 0))
        top.columnconfigure(1, weight=1)
        self.warn_var = tk.StringVar()
        ttk.Label(top, textvariable=self.warn_var, foreground="#b35c00").grid(row=2, column=0, columnspan=4, sticky="w")

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=8, pady=4)
        self.panes = panes

        # Browser: worlds and areas, then models.
        left = ttk.Frame(panes)
        self.search_var = tk.StringVar()
        search = ttk.Entry(left, textvariable=self.search_var)
        search.pack(fill="x")
        search.bind("<KeyRelease>", lambda e: self._fill_browser())
        ttk.Label(left, text="Filter by name or id", foreground="#777").pack(anchor="w")
        self.browser = ttk.Treeview(left, show="tree", selectmode="browse")
        bscroll = ttk.Scrollbar(left, orient="vertical", command=self.browser.yview)
        self.browser.configure(yscrollcommand=bscroll.set)
        self.browser.pack(side="left", fill="both", expand=True)
        bscroll.pack(side="right", fill="y")
        self.browser.bind("<<TreeviewSelect>>", self._browse_pick)
        panes.add(left, weight=2)

        # The 3D preview over the materials of the chosen area or model.
        middle = ttk.PanedWindow(panes, orient="vertical")
        self._build_preview(middle)
        listing = ttk.Frame(middle)
        self.list_title = tk.StringVar(value="Pick an area or a model")
        ttk.Label(listing, textvariable=self.list_title, font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        cols = ("edited", "textures", "info")
        self.mats = ttk.Treeview(listing, columns=cols, show="tree headings", selectmode="browse")
        self.mats.heading("#0", text="Material")
        self.mats.heading("edited", text="Changed")
        self.mats.heading("textures", text="Textures")
        self.mats.heading("info", text="")
        self.mats.column("#0", width=110, stretch=False)
        self.mats.column("edited", width=70, stretch=False, anchor="center")
        self.mats.column("textures", width=170)
        self.mats.column("info", width=170)
        mscroll = ttk.Scrollbar(listing, orient="vertical", command=self.mats.yview)
        self.mats.configure(yscrollcommand=mscroll.set)
        self.mats.pack(side="left", fill="both", expand=True)
        mscroll.pack(side="right", fill="y")
        self.mats.bind("<<TreeviewSelect>>", self._material_pick)
        middle.add(listing, weight=2)
        panes.add(middle, weight=5)

        # The material's settings.
        right = ttk.Frame(panes, padding=(8, 0, 0, 0))
        self.detail_title = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.detail_title, font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        keyrow = ttk.Frame(right)
        keyrow.pack(fill="x")
        self.key_var = tk.StringVar()
        ttk.Entry(keyrow, textvariable=self.key_var, state="readonly", width=20).pack(side="left")
        ttk.Button(keyrow, text="Copy key", command=self._copy_key).pack(side="left", padx=4)
        self.thumb_frame = ttk.Frame(right)
        self.thumb_frame.pack(fill="x", pady=6)
        form = ttk.Frame(right)
        form.pack(fill="x")
        self.field_widgets = {}
        row = 0
        for name, label, kind, lo, hi, default, help_text in mc.FIELDS:
            if name == "reflection":
                ttk.Separator(form).grid(row=row, column=0, columnspan=4, sticky="ew", pady=6)
                row += 1
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w")
            if kind == "bool":
                var = tk.StringVar(value="Game's choice")
                box = ttk.Combobox(form, textvariable=var, state="readonly", width=14,
                                   values=("Game's choice", "On", "Off"))
                box.grid(row=row, column=1, columnspan=3, sticky="w")
                box.bind("<<ComboboxSelected>>", lambda e: self._changed())
                self.field_widgets[name] = (kind, var, None, box)
                self._tip(box, help_text)
            else:
                on = tk.BooleanVar(value=False)
                val = tk.DoubleVar(value=default)
                check = ttk.Checkbutton(form, variable=on, command=self._changed)
                check.grid(row=row, column=1, sticky="w")
                scale = ttk.Scale(form, from_=lo, to=hi, variable=val, length=200,
                                  command=lambda v, n=name: self._slid(n))
                scale.grid(row=row, column=2, sticky="ew", padx=4)
                text = tk.StringVar()
                entry = ttk.Entry(form, textvariable=text, width=7)
                entry.grid(row=row, column=3, sticky="w")
                entry.bind("<Return>", lambda e, n=name: self._typed(n))
                entry.bind("<FocusOut>", lambda e, n=name: self._typed(n))
                self.field_widgets[name] = (kind, on, val, (scale, text, entry))
                self._tip(scale, help_text)
                self._tip(check, "Ticked: this material uses the value here. Unticked: the game's own.")
            row += 1
        ttk.Label(form, text="Note").grid(row=row, column=0, sticky="w", pady=(6, 0))
        self.note_var = tk.StringVar()
        note = ttk.Entry(form, textvariable=self.note_var)
        note.grid(row=row, column=1, columnspan=3, sticky="ew", pady=(6, 0))
        note.bind("<KeyRelease>", lambda e: self._changed())
        form.columnconfigure(2, weight=1)
        buttons = ttk.Frame(right)
        buttons.pack(fill="x", pady=8)
        ttk.Button(buttons, text="Save", command=self.save).pack(side="left")
        ttk.Button(buttons, text="Reset material", command=self.reset_material).pack(side="left", padx=4)
        ttk.Button(buttons, text="Copy settings", command=self.copy_settings).pack(side="left")
        ttk.Button(buttons, text="Paste settings", command=self.paste_settings).pack(side="left", padx=4)
        same = ttk.Frame(right)
        same.pack(fill="x")
        ttk.Button(same, text="Apply to every listed material with texture", command=self.apply_same_texture).pack(side="left")
        self.same_var = tk.StringVar()
        self.same_box = ttk.Combobox(same, textvariable=self.same_var, state="readonly", width=12)
        self.same_box.pack(side="left", padx=4)
        self.help_var = tk.StringVar(value="Hover over a setting for what it does.")
        ttk.Label(right, textvariable=self.help_var, wraplength=380, foreground="#555", justify="left").pack(
            fill="x", pady=(8, 0))
        panes.add(right, weight=3)
        self._enable_fields(False)

        self.status_var = tk.StringVar()
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w", padding=(6, 2)).pack(fill="x", side="bottom")
        self.bind_all("<Control-s>", lambda e: self.save())
        self.sashes_placed = False
        self.panes.bind("<Configure>", self._place_sashes, add="+")

    def _place_sashes(self, _event=None):
        """Once the window has its size: the browser and the settings at a useful width, the rest for
        the preview and the list."""
        if self.sashes_placed:
            return
        try:
            width = self.panes.winfo_width()
            if width > 900:
                self.panes.sashpos(0, 270)
                self.panes.sashpos(1, width - 420)
                self.sashes_placed = True
        except tk.TclError:
            pass

    def _build_preview(self, parent):
        box = ttk.Frame(parent)
        parent.add(box, weight=3)
        bar = ttk.Frame(box)
        bar.pack(fill="x")
        self.preview_var = tk.StringVar(value="Pick an area or a model to see it here.")
        self.only_var = tk.BooleanVar(value=False)
        self.compare_var = tk.BooleanVar(value=False)
        self.wire_var = tk.BooleanVar(value=False)
        self.pbr_var = tk.BooleanVar(value=self.view_settings["pbr_maps"])
        self.live_var = tk.BooleanVar(value=self.view_settings["live_reflections"])
        problem = viewer.available()
        if problem is None:
            try:
                self.view = viewer.ModelView(box, self._view_fields, self._view_pick, self.view_settings,
                                             width=480, height=320)
            except Exception as e:  # noqa: BLE001 - the editor works without the preview
                problem = "The 3D preview couldn't start: %s" % e
                self.view = None
        controls = [
            ("Frame all", lambda: self.view and self.view.frame_all(), "Show the whole area or model (Home)."),
            ("Frame material", lambda: self.view and self.view.frame_selected(),
             "Zoom to the picked material (F)."),
        ]
        for text, command, tip in controls:
            b = ttk.Button(bar, text=text, command=command)
            b.pack(side="left", padx=(0, 4))
            self._tip(b, tip)
        checks = [
            ("Only this material", self.only_var, "Draw the picked material alone."),
            ("Outline", self.wire_var, "Outline the picked material's triangles."),
            ("Without my changes", self.compare_var,
             "Draw every material with the game's own settings, to compare with yours."),
        ]
        for text, var, tip in checks:
            c = ttk.Checkbutton(bar, text=text, variable=var, command=self._view_options)
            c.pack(side="left", padx=(4, 0))
            self._tip(c, tip)
        game = ttk.Frame(box)
        game.pack(fill="x")
        ttk.Label(game, text="Game settings:", foreground="#555").pack(side="left")
        for text, var, tip in [
            ("PBR maps", self.pbr_var, "Video > Quality > PBR maps in the game (read from its settings.cfg). "
             "Off in the game, the PBR settings change nothing there; tick it here to preview them anyway."),
            ("Live reflections", self.live_var, "Video > Quality > Live reflections in the game. The preview's "
             "live reflections show a studio around the model; the game's show the room."),
        ]:
            c = ttk.Checkbutton(game, text=text, variable=var, command=self._view_options)
            c.pack(side="left", padx=(4, 0))
            self._tip(c, tip)
        ttk.Label(game, text="Luminance").pack(side="left", padx=(8, 2))
        self.lum_var = tk.StringVar(value="%d" % round(self.view_settings["luminance"] * 100))
        lum = ttk.Spinbox(game, from_=0, to=400, increment=10, width=5, textvariable=self.lum_var,
                          command=self._view_options)
        lum.pack(side="left")
        lum.bind("<Return>", lambda e: self._view_options())
        lum.bind("<FocusOut>", lambda e: self._view_options())
        ttk.Label(game, text="%").pack(side="left")
        self._tip(lum, "Video > Quality > Luminance in the game (percent, read from its settings.cfg): how "
                       "bright every glow layer is, times each material's own Luminance.")
        hint = ttk.Label(game, text="Drag: turn  Right-drag: move  Wheel: zoom  Shift-drag: light  Click: pick  "
                         "Double-click: turn around there  W/A/S/D/Q/E: fly", foreground="#777")
        hint.pack(side="left", padx=(12, 0))
        if self.view is not None:
            self.view.pack(fill="both", expand=True, pady=(4, 0))
            self.view.on_problem = lambda text: self.preview_var.set(text)
        else:
            ttk.Label(box, text=problem + "\nThe editor works without it.", foreground="#b35c00",
                      justify="center", anchor="center").pack(fill="both", expand=True)
        ttk.Label(box, textvariable=self.preview_var, foreground="#555").pack(fill="x")

    def _view_fields(self, material):
        return self.overrides.get(material.key_text) if self.overrides else {}

    def _view_options(self):
        if self.view is None:
            return
        self.view.only_selected = self.only_var.get()
        self.view.compare = self.compare_var.get()
        self.view.wire = self.wire_var.get()
        self.view_settings["pbr_maps"] = self.pbr_var.get()
        self.view_settings["live_reflections"] = self.live_var.get()
        try:
            self.view_settings["luminance"] = max(0.0, min(400.0, float(self.lum_var.get()))) / 100.0
        except ValueError:
            self.lum_var.set("%d" % round(self.view_settings["luminance"] * 100))
        self.view.request()

    def _view_pick(self, index):
        """A material clicked in the preview: pick it in the list."""
        if not self.selected or self.view is None or self.view.scene is None:
            return
        set_index = self.view.scene.set_index
        for n, m in enumerate(self.selected[1]):
            if m.index == index and m.set_index == set_index:
                self.mats.selection_set(str(n))
                self.mats.see(str(n))
                return

    def _load_preview(self, set_index=0, frame=True):
        if self.view is None or self.source is None or self.library is None:
            return
        self.preview_token += 1
        token = self.preview_token
        kind, rid = self.source
        library, pack = self.library, self.pack
        self.preview_var.set("Loading the preview...")
        if frame:
            self.view.set_scene(None)

        def work():
            try:
                return token, preview.build_scene(library, kind, rid, set_index, pack), None
            except Exception as e:  # noqa: BLE001 - shown under the preview
                return token, None, "%s: %s" % (type(e).__name__, e)

        self._run(work, lambda result: self._preview_ready(result, frame), "Couldn't load the preview")

    def _preview_ready(self, result, frame):
        token, scene, error = result
        if token != self.preview_token or self.view is None:
            return
        if error:
            self.view.set_scene(None)
            self.preview_var.set("No preview: %s" % error)
            return
        self.view.set_scene(scene, frame=frame)
        if self.material is not None:
            self.view.select(self.material.index if self.material.set_index == scene.set_index else None)
        drawn = len(scene.batches)
        with_maps = sum(1 for k in scene.batches if scene.has_maps(k))
        parts = ["%d triangles, %d material(s) drawn" % (scene.triangles, drawn)]
        if with_maps:
            parts.append("%d with PBR maps" % with_maps)
        if scene.set_index:
            parts.append("material set %d" % scene.set_index)
        parts += [note.rstrip(".") for note in scene.notes]
        self.preview_var.set(". ".join(parts) + ". Studio lighting, not the room's.")

    def _tip(self, widget, text):
        widget.bind("<Enter>", lambda e: self.help_var.set(text))

    def status(self, text):
        self.status_var.set(text)

    # ------------------------------------------------------------------------------- the files
    def _set_overrides(self, path):
        self.overrides = mc.Overrides(path)
        self.out_var.set(path or "(no mods folder: choose where to save)")
        warn = []
        if self.overrides.problem:
            warn.append("Couldn't read the existing file (%s); saving will replace it." % self.overrides.problem)
        if not mc.setting_bool(self.settings.get("mods"), True):
            warn.append("Mods are off in the game (F1 > Mods > Load mods): turn them on for these changes to show.")
        if path and self.mods:
            folder = os.path.basename(os.path.dirname(path))
            disabled = [d for d in (self.settings.get("mods_disabled") or "").split("/") if d]
            if folder in disabled:
                warn.append("The mod folder '%s' is switched off in F1 > Mods." % folder)
        if not self.user:
            warn.append("The game's user folder wasn't found: put the editor beside metroid_prime2_port.exe, "
                        "or choose where to save.")
        self.warn_var.set("  ".join(warn))
        if self.pack is not None:
            self._pack_warning()
        self._refresh_list_marks()

    def choose_output(self):
        initial = os.path.dirname(self.overrides.path) if self.overrides and self.overrides.path else self.mods
        path = filedialog.asksaveasfilename(title="Save materials.json in a mod folder", initialdir=initial or None,
                                            initialfile=mc.OVERRIDES_FILE, defaultextension=".json",
                                            filetypes=[("Material settings", "materials.json"), ("JSON", "*.json")])
        if path:
            self._set_overrides(path)

    def save(self):
        if self.save_after is not None:
            self.after_cancel(self.save_after)
            self.save_after = None
        if not self.overrides or not self.overrides.path:
            self.choose_output()
            if not self.overrides or not self.overrides.path:
                return
        try:
            self.overrides.save()
        except OSError as e:
            messagebox.showerror(TITLE, "Couldn't save %s:\n%s" % (self.overrides.path, e))
            return
        self.status("Saved %d material(s) to %s. The running game picks it up within a couple of seconds."
                    % (len(self.overrides.materials), self.overrides.path))

    def _schedule_save(self):
        if not self.autosave.get():
            self.status("Changed (not saved yet: Save or Ctrl+S).")
            return
        if self.save_after is not None:
            self.after_cancel(self.save_after)
        self.save_after = self.after(SAVE_DELAY_MS, self.save)

    # --------------------------------------------------------------------------------- the disc
    def browse_disc(self):
        path = filedialog.askopenfilename(title="Your Metroid Prime 2: Echoes disc image",
                                          filetypes=[("GameCube disc image", "*.iso *.gcm *.ciso"), ("All files", "*.*")])
        if path:
            self.disc_var.set(path)
            self.open_disc()

    def open_disc(self):
        path = self.disc_var.get().strip()
        if not path:
            return
        problem = mc.disc_problem(path) if os.path.isfile(path) else "There's no file at %s." % path
        if problem:
            messagebox.showerror(TITLE, problem)
            return
        self.status("Reading the disc...")
        self._run(lambda: mc.Library(path), self._disc_opened, "Couldn't read the disc")

    def _disc_opened(self, library):
        if self.library is not None:
            self.library.close()
        self.library = library
        self.source = None
        if self.view is not None:
            self.view.set_scene(None)
        self.pack = mc.TexturePack(mc.texture_pack_roots(self.user, self.settings))
        self._fill_browser()
        areas = sum(len(w["areas"]) for w in library.worlds)
        self.status("%d world(s), %d area(s), %d model(s). Texture pack: %s. Pick one to see its materials."
                    % (len(library.worlds), areas, len(library.models), self.pack.summary()))
        self._pack_warning()

    def _pack_warning(self):
        """Say why the pack shows nothing, as far as the editor can tell."""
        notes = [w for w in self.warn_var.get().split("  ") if w and not w.startswith("Texture pack:")]
        if not mc.setting_bool(self.settings.get("texture_pack"), True):
            notes.append("Texture pack: switched off in the game (F1 > Mods > Textures), so its textures and "
                         "PBR maps aren't used.")
        elif self.pack is not None and not self.pack.textures:
            folder = mc.user_texture_folder(self.user) or "(no user folder)"
            reason = next((note for _r, f, _c, note in self.pack.folders if f is None and note), None)
            notes.append("Texture pack: no pack textures found in %s%s. Put the pack's files (tex1_... .dds/.png) "
                         "there, as the game reads them." % (folder, " (%s)" % reason if reason else ""))
        self.warn_var.set("  ".join(notes))

    def _fill_browser(self):
        self.browser.delete(*self.browser.get_children())
        if self.library is None:
            return
        needle = self.search_var.get().strip().lower()
        for w in self.library.worlds:
            areas = [a for a in w["areas"] if not needle or needle in a["name"].lower() or
                     needle in a["internal"].lower() or needle in "%08x" % a["mrea"]]
            if needle and not areas and needle not in w["name"].lower():
                continue
            node = self.browser.insert("", "end", text="%s  (%s)" % (w["name"], w["pak"]), open=bool(needle))
            for a in (areas if areas or needle else w["areas"]):
                self.browser.insert(node, "end", iid="MREA:%08X" % a["mrea"], text=a["name"])
        models = [rid for rid in self.library.models
                  if not needle or needle in self.library.model_name(rid).lower()]
        node = self.browser.insert("", "end", text="Models (%d)" % len(models), open=bool(needle))
        for rid in models[:3000]:
            self.browser.insert(node, "end", iid="CMDL:%08X" % rid, text=self.library.model_name(rid))
        if len(models) > 3000:
            self.browser.insert(node, "end", text="... %d more: filter to find them" % (len(models) - 3000))

    def _browse_pick(self, _event):
        sel = self.browser.selection()
        if not sel or ":" not in sel[0]:
            return
        kind, hexid = sel[0].split(":")
        rid = int(hexid, 16)
        label = self.browser.item(sel[0], "text")
        self.status("Reading %s..." % label)
        if kind == "MREA":
            self._run(lambda: [self.library.area_materials(rid)],
                      lambda sets: self._show_materials(label, sets, (kind, rid)), "Couldn't read the area")
        else:
            self._run(lambda: self.library.model_materials(rid),
                      lambda sets: self._show_materials(label, sets, (kind, rid)), "Couldn't read the model")

    def _show_materials(self, label, sets, source=None):
        materials = [m for s in sets for m in s]
        self.selected = (label, materials)
        self.list_title.set("%s: %d material(s)" % (label, len(materials)))
        self.mats.delete(*self.mats.get_children())
        for n, m in enumerate(materials):
            name = "%d" % m.index if len(sets) == 1 else "set %d: %d" % (m.set_index, m.index)
            textures = ", ".join("%08X" % t if t is not None else "?" for t in m.textures)
            self.mats.insert("", "end", iid=str(n), text=name,
                             values=("yes" if m.key_text in self.overrides.materials else "", textures, m.summary()))
        self.status("%s: %d material(s)." % (label, len(materials)))
        self.material = None
        self._enable_fields(False)
        if self.view is not None:
            self.view.select(None)
        if source is not None:
            self.source = source
            self._load_preview()

    def _refresh_list_marks(self):
        if self.view is not None:
            self.view.request()  # every change of the settings comes through here
        if not self.selected or not hasattr(self, "mats"):
            return
        for n, m in enumerate(self.selected[1]):
            if self.mats.exists(str(n)):
                self.mats.set(str(n), "edited", "yes" if m.key_text in self.overrides.materials else "")

    # ------------------------------------------------------------------------------ a material
    def _material_pick(self, _event):
        sel = self.mats.selection()
        if not sel or not self.selected:
            return
        m = self.selected[1][int(sel[0])]
        self.material = m
        self.detail_title.set("Material %d of %s" % (m.index, self.selected[0]))
        self.key_var.set(m.key_text)
        self._show_textures(m)
        self._load_fields(self.overrides.get(m.key_text))
        self._enable_fields(True)
        self.same_box["values"] = ["%08X" % t for t in m.textures if t is not None]
        if self.same_box["values"]:
            self.same_box.current(0)
        if self.view is not None:
            scene = self.view.scene
            if scene is not None and scene.kind == "CMDL" and m.set_index != scene.set_index:
                self._load_preview(m.set_index, frame=False)  # draw the model with this material's set
            self.view.select(m.index)
            if scene is not None and m.set_index == scene.set_index and m.index not in scene.batches:
                self.status("Material %d isn't drawn by any of this %s's triangles."
                            % (m.index, "area" if scene.kind == "MREA" else "model"))

    def _show_textures(self, m):
        for child in self.thumb_frame.winfo_children():
            child.destroy()
        self.thumbs = []
        for slot, tid in enumerate(m.textures[:4]):
            cell = ttk.Frame(self.thumb_frame)
            cell.pack(side="left", padx=(0, 8))
            image = None
            pack = self.library.texture_pack_name(tid) if tid is not None else None
            tex, nrm, orm = self.pack.find(pack) if self.pack is not None else (None, None, None)
            if tex and preview.Image is not None:
                # The pack's texture, as the game draws it.
                picture = preview.pack_picture(tex, THUMB)
                if picture is not None:
                    try:
                        image = tk.PhotoImage(data=base64.b64encode(
                            mc.png_bytes(picture.width, picture.height, picture.rgba)).decode())
                    except tk.TclError:
                        image = None
            if image is None and tid is not None:
                data = self.library.texture(tid)
                if data:
                    try:
                        w, h, rgba = mc.decode_txtr(data, THUMB)
                        image = tk.PhotoImage(data=base64.b64encode(mc.png_bytes(w, h, rgba)).decode())
                        scale = max(1, THUMB // max(w, h))
                        if scale > 1:
                            image = image.zoom(scale, scale)
                    except (ValueError, tk.TclError, IndexError):
                        image = None
            if image is not None:
                self.thumbs.append(image)
                ttk.Label(cell, image=image).pack()
            else:
                ttk.Label(cell, text="(no preview)", width=12).pack()
            maps = "PBR maps" if nrm and orm else ("HD texture" if tex else "")
            ttk.Label(cell, text="%s%s" % ("%08X" % tid if tid is not None else "?", "\n" + maps if maps else "")).pack()
        if len(m.textures) > 4:
            ttk.Label(self.thumb_frame, text="+%d more" % (len(m.textures) - 4)).pack(side="left")

    def _enable_fields(self, on):
        state = "normal" if on else "disabled"
        for kind, a, _b, w in self.field_widgets.values():
            if kind == "bool":
                w.configure(state="readonly" if on else "disabled")
            else:
                scale, _text, entry = w
                scale.configure(state=state)
                entry.configure(state=state)
        if not on:
            self.detail_title.set("")
            self.key_var.set("")
            for child in self.thumb_frame.winfo_children():
                child.destroy()

    def _load_fields(self, fields):
        self.loading_fields = True
        for name, (kind, a, b, w) in self.field_widgets.items():
            spec = next(f for f in mc.FIELDS if f[0] == name)
            if kind == "bool":
                v = fields.get(name)
                a.set("Game's choice" if v is None else ("On" if v else "Off"))
            else:
                v = fields.get(name)
                a.set(v is not None)
                b.set(v if v is not None else spec[5])
                w[1].set("%.2f" % b.get())
        self.note_var.set(fields.get("note", ""))
        self.loading_fields = False

    def _fields(self):
        out = {}
        for name, (kind, a, b, _w) in self.field_widgets.items():
            if kind == "bool":
                if a.get() != "Game's choice":
                    out[name] = a.get() == "On"
            elif a.get():
                out[name] = round(float(b.get()), 3)
        if self.note_var.get().strip():
            out["note"] = self.note_var.get().strip()
        return out

    def _slid(self, name):
        kind, on, val, (scale, text, entry) = self.field_widgets[name]
        text.set("%.2f" % val.get())
        if not self.loading_fields:
            on.set(True)
            self._changed()

    def _typed(self, name):
        kind, on, val, (scale, text, entry) = self.field_widgets[name]
        try:
            v = mc.clamp_field(name, float(text.get()))
        except ValueError:
            text.set("%.2f" % val.get())
            return
        if abs(v - val.get()) > 1e-9:
            val.set(v)
            on.set(True)
            self._changed()
        text.set("%.2f" % v)

    def _changed(self):
        if self.loading_fields or self.material is None:
            return
        self.overrides.set(self.material.key_text, self._fields())
        self._refresh_list_marks()
        self._schedule_save()

    def reset_material(self):
        if self.material is None:
            return
        self.overrides.set(self.material.key_text, {})
        self._load_fields({})
        self._refresh_list_marks()
        self._schedule_save()

    def copy_settings(self):
        if self.material is not None:
            self.clipboard = dict(self._fields())
            self.clipboard.pop("note", None)
            self.status("Copied %d setting(s)." % len(self.clipboard))

    def paste_settings(self):
        if self.material is None or self.clipboard is None:
            return
        fields = dict(self.clipboard)
        note = self.overrides.get(self.material.key_text).get("note")
        if note:
            fields["note"] = note
        self.overrides.set(self.material.key_text, fields)
        self._load_fields(self.overrides.get(self.material.key_text))
        self._refresh_list_marks()
        self._schedule_save()

    def apply_same_texture(self):
        if self.material is None or not self.selected or not self.same_var.get():
            return
        tid = int(self.same_var.get(), 16)
        fields = self._fields()
        fields.pop("note", None)
        count = 0
        for m in self.selected[1]:
            if tid in m.textures:
                merged = dict(fields)
                note = self.overrides.get(m.key_text).get("note")
                if note:
                    merged["note"] = note
                self.overrides.set(m.key_text, merged)
                count += 1
        self._refresh_list_marks()
        self._schedule_save()
        self.status("Applied to %d material(s) using texture %08X." % (count, tid))

    def _copy_key(self):
        if self.key_var.get():
            self.clipboard_clear()
            self.clipboard_append(self.key_var.get())

    # ------------------------------------------------------------------------------ background
    def _run(self, work, done, what):
        def worker():
            try:
                self.jobs.put((done, work(), None))
            except Exception as e:  # noqa: BLE001 - shown to the user
                self.jobs.put((None, None, "%s: %s" % (what, e)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_jobs(self):
        try:
            while True:
                done, result, error = self.jobs.get_nowait()
                if error:
                    self.status(error)
                    messagebox.showerror(TITLE, error)
                else:
                    done(result)
        except queue.Empty:
            pass
        self.after(100, self._poll_jobs)

    def _close(self):
        if self.save_after is not None:
            self.save()
        if self.library is not None:
            self.library.close()
        self.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--game", help="the folder metroid_prime2_port.exe is in (default: the editor's)")
    parser.add_argument("--user", help="the game's user folder (default: as the game finds it)")
    parser.add_argument("--disc", help="the disc image (default: the game's disc_path setting)")
    parser.add_argument("--output", help="the materials.json to write (default: <mods>/Material Editor/materials.json)")
    args = parser.parse_args(argv)
    Editor(args).mainloop()


if __name__ == "__main__":
    main()
