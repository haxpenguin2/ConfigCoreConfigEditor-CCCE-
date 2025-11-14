#!/usr/bin/env python3
"""
plugin_builder.py

Graphical plugin creation engine for the ConfigCore system.

- Build a simple plugin UI (vertical layout) by adding widgets.
- Buttons may run shell commands or edit the ConfigFile (append/replace a line).
- Export a plugin folder containing plugin.py and plugin.json (and optional scripts).
- Preview the plugin UI live in the builder.

Usage:
    pip3 install PyQt6
    python3 plugin_builder.py

Output:
    Exports plugin folders you can copy to ~/.config_editor_packages/<plugin_name>
    or install via the Core's "Install" flow (zip or repo).

Notes:
- Generated plugin code expects to be loaded by your Core and receives the core.ConfigFile instance
  as the single constructor arg for EditorWidget(core_config).
- The generated plugin uses only standard libs + PyQt6 to keep dependencies minimal.
"""
from __future__ import annotations
import json
import os
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional
import textwrap
import subprocess

from PyQt6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QLineEdit, QTextEdit,
    QComboBox, QMessageBox, QFileDialog, QSplitter, QFrame, QSpinBox,
    QGroupBox, QFormLayout, QSizePolicy, QTabWidget
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

# -------------------------
# Model: widget/item schema
# -------------------------
WidgetSpec = Dict[str, Any]
# example:
# {
#   "type": "button",
#   "label": "Run something",
#   "action": {"kind": "run_shell", "cmd": "echo hi"},
# }

DEFAULT_MIN_CORE = "0.2.0"

# -------------------------
# Helper: code generation templates
# -------------------------
PLUGIN_PY_TEMPLATE = """#!/usr/bin/env python3
\"\"\"Auto-generated plugin by plugin_builder.py

Plugin: {name}
Generated: {ts}
Core min version: {min_core}

This EditorWidget builds a simple vertical layout of widgets as specified in plugin.json
and implements button actions:
 - run_shell: runs a shell command (non-blocking)
 - append_line: appends a line to the core config (uses core.append_line)
 - replace_line: replaces a specific zero-based line index (uses core.replace_line)

You can expand this file manually if you need more complex behavior.
\"\"\"
from __future__ import annotations
import subprocess
import traceback
from typing import List, Dict, Any
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton, QTextEdit, QMessageBox
from PyQt6.QtGui import QFont
from PyQt6.QtCore import Qt

class EditorWidget(QWidget):
    def __init__(self, core_config=None):
        super().__init__()
        self.core = core_config  # may be None in preview
        self.setLayout(QVBoxLayout())
        # Build UI
{build_code}
    def run_shell(self, cmd: str):
        \"\"\"Run a shell command non-blocking. Errors shown in message box (if possible).\"\"\"
        try:
            # Note: security: this executes arbitrary shell commands in user's environment.
            subprocess.Popen(cmd, shell=True)
        except Exception as e:
            try:
                QMessageBox.critical(self, \"Command failed\", str(e))
            except Exception:
                print(\"Command failed:\", e)
{helper_methods}
"""

PLUGIN_JSON_TEMPLATE = {
    "name": "",            # filled in
    "version": "0.1.0",
    "description": "",
    "min_core_version": DEFAULT_MIN_CORE,
    # optional: ui description (for human editing / future use)
    "ui": {
        "layout": "vertical",
        "widgets": []  # filled with widget specs
    }
}

# -------------------------
# Builder UI
# -------------------------
class BuilderMain(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ConfigCore Plugin Builder")
        self.resize(1100, 720)

        central = QWidget()
        self.setCentralWidget(central)
        main = QVBoxLayout()
        central.setLayout(main)

        header = QHBoxLayout()
        main.addLayout(header)
        header.addWidget(QLabel("Plugin Builder"))
        header.addStretch()
        self.name_input = QLineEdit("my_plugin")
        self.name_input.setPlaceholderText("plugin folder / module name (no spaces)")
        header.addWidget(QLabel("Name:"))
        header.addWidget(self.name_input)
        self.version_input = QLineEdit("0.1.0")
        self.min_core_input = QLineEdit(DEFAULT_MIN_CORE)
        header.addWidget(QLabel("v"))
        header.addWidget(self.version_input)
        header.addWidget(QLabel("min_core:"))
        header.addWidget(self.min_core_input)

        main.addSpacing(6)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(splitter)

        # Left: widget list + actions
        left = QWidget()
        left_layout = QVBoxLayout()
        left.setLayout(left_layout)
        splitter.addWidget(left)

        left_layout.addWidget(QLabel("Widgets (vertical layout):"))
        self.widget_list = QListWidget()
        left_layout.addWidget(self.widget_list)

        wl_row = QHBoxLayout()
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self.add_widget_prompt)
        wl_row.addWidget(add_btn)
        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(self.edit_selected)
        wl_row.addWidget(edit_btn)
        rm_btn = QPushButton("Remove")
        rm_btn.clicked.connect(self.remove_selected)
        wl_row.addWidget(rm_btn)
        up_btn = QPushButton("Up")
        up_btn.clicked.connect(self.move_up)
        wl_row.addWidget(up_btn)
        down_btn = QPushButton("Down")
        down_btn.clicked.connect(self.move_down)
        wl_row.addWidget(down_btn)
        left_layout.addLayout(wl_row)

        left_layout.addSpacing(6)
        left_layout.addWidget(QLabel("Scripts to include (optional):"))
        self.scripts_list = QListWidget()
        left_layout.addWidget(self.scripts_list)
        scr_row = QHBoxLayout()
        add_scr = QPushButton("Add script")
        add_scr.clicked.connect(self.add_script)
        scr_row.addWidget(add_scr)
        rm_scr = QPushButton("Remove script")
        rm_scr.clicked.connect(self.remove_script)
        scr_row.addWidget(rm_scr)
        left_layout.addLayout(scr_row)
        left_layout.addStretch()

        # Right: preview + export/settings
        right = QWidget()
        right_layout = QVBoxLayout()
        right.setLayout(right_layout)
        splitter.addWidget(right)
        splitter.setSizes([420, 640])

        # preview area
        right_layout.addWidget(QLabel("Live preview:"))
        self.preview_area = QGroupBox()
        self.preview_area.setLayout(QVBoxLayout())
        right_layout.addWidget(self.preview_area)
        self.preview_container = QWidget()
        self.preview_container.setLayout(QVBoxLayout())
        self.preview_area.layout().addWidget(self.preview_container)

        # bottom actions: preview refresh + export
        btn_row = QHBoxLayout()
        preview_btn = QPushButton("Refresh Preview")
        preview_btn.clicked.connect(self.refresh_preview)
        btn_row.addWidget(preview_btn)
        export_btn = QPushButton("Export plugin...")
        export_btn.clicked.connect(self.export_plugin)
        btn_row.addWidget(export_btn)
        save_json_btn = QPushButton("Save plugin.json locally")
        save_json_btn.clicked.connect(self.save_plugin_json)
        btn_row.addWidget(save_json_btn)
        right_layout.addLayout(btn_row)

        # internal model
        self.widgets: List[WidgetSpec] = []
        self.scripts: List[Dict[str,str]] = []  # {name: filename, content: text}
        self.refresh_widget_list()
        self.refresh_preview()

    # -------------------------
    # widget list manipulation
    # -------------------------
    def refresh_widget_list(self):
        self.widget_list.clear()
        for spec in self.widgets:
            typ = spec.get("type")
            label = spec.get("label") or ""
            it = QListWidgetItem(f"{typ}: {label}")
            it.setData(Qt.ItemDataRole.UserRole, spec)
            self.widget_list.addItem(it)

    def add_widget_prompt(self):
        dlg = WidgetEditorDialog(self, None)
        if dlg.exec():
            spec = dlg.get_spec()
            self.widgets.append(spec)
            self.refresh_widget_list()
            self.refresh_preview()

    def edit_selected(self):
        it = self.widget_list.currentItem()
        if not it:
            QMessageBox.information(self, "Select", "Select a widget to edit.")
            return
        spec = it.data(Qt.ItemDataRole.UserRole)
        idx = self.widget_list.currentRow()
        dlg = WidgetEditorDialog(self, spec)
        if dlg.exec():
            new = dlg.get_spec()
            self.widgets[idx] = new
            self.refresh_widget_list()
            self.refresh_preview()

    def remove_selected(self):
        r = self.widget_list.currentRow()
        if r >= 0:
            self.widgets.pop(r)
            self.refresh_widget_list()
            self.refresh_preview()

    def move_up(self):
        r = self.widget_list.currentRow()
        if r > 0:
            self.widgets[r-1], self.widgets[r] = self.widgets[r], self.widgets[r-1]
            self.refresh_widget_list()
            self.widget_list.setCurrentRow(r-1)
            self.refresh_preview()

    def move_down(self):
        r = self.widget_list.currentRow()
        if r >= 0 and r < len(self.widgets)-1:
            self.widgets[r+1], self.widgets[r] = self.widgets[r], self.widgets[r+1]
            self.refresh_widget_list()
            self.widget_list.setCurrentRow(r+1)
            self.refresh_preview()

    # -------------------------
    # scripts
    # -------------------------
    def add_script(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Choose script file to include (it will be copied into plugin)", "", "All files (*)")
        if not fn:
            return
        p = Path(fn)
        content = p.read_text(encoding="utf-8", errors="ignore")
        self.scripts.append({"name": p.name, "content": content})
        self.scripts_list.addItem(p.name)

    def remove_script(self):
        r = self.scripts_list.currentRow()
        if r >= 0:
            self.scripts.pop(r)
            self.scripts_list.takeItem(r)

    # -------------------------
    # preview
    # -------------------------
    def clear_preview(self):
        layout = self.preview_container.layout()
        while layout.count():
            w = layout.takeAt(0).widget()
            if w:
                w.deleteLater()

    def refresh_preview(self):
        self.clear_preview()
        # create a simple preview widget hierarchy based on self.widgets
        for spec in self.widgets:
            typ = spec.get("type")
            if typ == "label":
                lab = QLabel(spec.get("label",""))
                lab.setFont(QFont("monospace", 10))
                lab.setWordWrap(True)
                self.preview_container.layout().addWidget(lab)
            elif typ == "text":
                te = QTextEdit()
                te.setPlainText(spec.get("text",""))
                te.setReadOnly(True)
                te.setFixedHeight(100)
                self.preview_container.layout().addWidget(te)
            elif typ == "button":
                btn = QPushButton(spec.get("label","Button"))
                action = spec.get("action", {})
                def make_handler(act):
                    def handler():
                        kind = act.get("kind")
                        if kind == "run_shell":
                            cmd = act.get("cmd","")
                            try:
                                subprocess.Popen(cmd, shell=True)
                                QMessageBox.information(self, "Preview", f"Would run: {cmd}")
                            except Exception as e:
                                QMessageBox.critical(self, "Err", str(e))
                        else:
                            QMessageBox.information(self, "Preview", f"Action: {act.get('kind')}")
                    return handler
                btn.clicked.connect(make_handler(action))
                self.preview_container.layout().addWidget(btn)
        self.preview_container.layout().addStretch()

    # -------------------------
    # export & saving
    # -------------------------
    def save_plugin_json(self):
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Name required", "Please fill plugin name before saving.")
            return
        destfn, _ = QFileDialog.getSaveFileName(self, "Save plugin.json as...", f"{name}_plugin.json", "JSON files (*.json)")
        if not destfn:
            return
        manifest = PLUGIN_JSON_TEMPLATE.copy()
        manifest["name"] = name
        manifest["version"] = self.version_input.text().strip() or "0.1.0"
        manifest["min_core_version"] = self.min_core_input.text().strip() or DEFAULT_MIN_CORE
        manifest["description"] = f"Auto-generated plugin {name}"
        manifest["ui"] = {"layout":"vertical", "widgets": self.widgets}
        Path(destfn).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        QMessageBox.information(self, "Saved", f"plugin.json saved to {destfn}")

    def export_plugin(self):
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Name required", "Please fill plugin name before exporting.")
            return
        dest = QFileDialog.getExistingDirectory(self, "Choose base folder to export plugin into")
        if not dest:
            return
        plugin_dir = Path(dest) / name
        if plugin_dir.exists():
            confirm = QMessageBox.question(self, "Overwrite", f"Folder {plugin_dir} exists. Overwrite?")
            if confirm != QMessageBox.StandardButton.Yes:
                return
            shutil.rmtree(plugin_dir)
        plugin_dir.mkdir(parents=True, exist_ok=True)
        # write plugin.json
        manifest = PLUGIN_JSON_TEMPLATE.copy()
        manifest["name"] = name
        manifest["version"] = self.version_input.text().strip() or "0.1.0"
        manifest["min_core_version"] = self.min_core_input.text().strip() or DEFAULT_MIN_CORE
        manifest["description"] = f"Auto-generated plugin {name}"
        manifest["ui"] = {"layout":"vertical", "widgets": self.widgets}
        (plugin_dir / "plugin.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        # write any scripts
        if self.scripts:
            scripts_dir = plugin_dir / "scripts"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            for s in self.scripts:
                (scripts_dir / s["name"]).write_text(s["content"], encoding="utf-8")
        # write plugin.py
        build_code = self._generate_build_code(self.widgets)
        helper_methods = textwrap.dedent("""
        def append_line(self, new_line: str):
            try:
                if self.core:
                    self.core.append_line(new_line)
                else:
                    print("append_line (preview):", new_line)
            except Exception as e:
                try:
                    QMessageBox.critical(self, "Append failed", str(e))
                except Exception:
                    print("Append failed:", e)

        def replace_line(self, idx: int, new_line: str):
            try:
                if self.core:
                    self.core.replace_line(idx, new_line)
                else:
                    print(f"replace_line (preview): idx={idx} -> {new_line}")
            except Exception as e:
                try:
                    QMessageBox.critical(self, "Replace failed", str(e))
                except Exception:
                    print("Replace failed:", e)
        """)
        plugin_py = PLUGIN_PY_TEMPLATE.format(
            name=name,
            ts="(generated)",
            min_core=manifest["min_core_version"],
            build_code=build_code,
            helper_methods=helper_methods
        )
        (plugin_dir / "plugin.py").write_text(plugin_py, encoding="utf-8")
        QMessageBox.information(self, "Exported", f"Exported plugin to {plugin_dir}")

    def _generate_build_code(self, widgets: List[WidgetSpec]) -> str:
        """
        Produce indented python code snippet that when placed inside __init__
        builds the UI and wires actions.
        """
        lines = []
        lines.append("        # ui widgets")
        for i, spec in enumerate(widgets):
            t = spec.get("type")
            if t == "label":
                txt = spec.get("label","").replace('"', '\\"')
                lines.append(f'        lbl_{i} = QLabel(\"{txt}\")')
                lines.append(f'        lbl_{i}.setFont(QFont(\"monospace\", 10))')
                lines.append(f'        lbl_{i}.setWordWrap(True)')
                lines.append(f'        self.layout().addWidget(lbl_{i})')
            elif t == "text":
                content = spec.get("text","").replace('"""', '\\"\\\\"\\\"')
                lines.append(f'        te_{i} = QTextEdit()')
                lines.append(f'        te_{i}.setPlainText(r\"\"\"{content}\"\"\")')
                lines.append(f'        te_{i}.setReadOnly(True)')
                lines.append(f'        te_{i}.setFixedHeight(120)')
                lines.append(f'        self.layout().addWidget(te_{i})')
            elif t == "button":
                lbl = spec.get("label","Button").replace('"', '\\"')
                action = spec.get("action", {})
                kind = action.get("kind")
                lines.append(f'        btn_{i} = QPushButton(\"{lbl}\")')
                if kind == "run_shell":
                    cmd = action.get("cmd","").replace('"','\\"')
                    lines.append(f'        def _on_btn_{i}():')
                    lines.append(f'            try:')
                    lines.append(f'                subprocess.Popen(r\"{cmd}\", shell=True)')
                    lines.append(f'            except Exception as e:')
                    lines.append(f'                try:')
                    lines.append(f'                    QMessageBox.critical(self, \"Command failed\", str(e))')
                    lines.append(f'                except Exception:')
                    lines.append(f'                    print(\"Command failed:\", e)')
                    lines.append(f'        btn_{i}.clicked.connect(_on_btn_{i})')
                elif kind == "append_line":
                    new_line = action.get("line","").replace('"','\\"')
                    lines.append(f'        def _on_btn_{i}():')
                    lines.append(f'            try:')
                    lines.append(f'                if self.core:')
                    lines.append(f'                    self.core.append_line(r\"{new_line}\")')
                    lines.append(f'                else:')
                    lines.append(f'                    print(\"append_line (preview): {new_line}\")')
                    lines.append(f'            except Exception as e:')
                    lines.append(f'                try:')
                    lines.append(f'                    QMessageBox.critical(self, \"Append failed\", str(e))')
                    lines.append(f'                except Exception:')
                    lines.append(f'                    print(\"Append failed:\", e)')
                    lines.append(f'        btn_{i}.clicked.connect(_on_btn_{i})')
                elif kind == "replace_line":
                    idx = int(action.get("index", 0))
                    new_line = action.get("line","").replace('"','\\"')
                    lines.append(f'        def _on_btn_{i}():')
                    lines.append(f'            try:')
                    lines.append(f'                if self.core:')
                    lines.append(f'                    self.core.replace_line({idx}, r\"{new_line}\")')
                    lines.append(f'                else:')
                    lines.append(f'                    print(\"replace_line (preview): idx={idx} -> {new_line}\")')
                    lines.append(f'            except Exception as e:')
                    lines.append(f'                try:')
                    lines.append(f'                    QMessageBox.critical(self, \"Replace failed\", str(e))')
                    lines.append(f'                except Exception:')
                    lines.append(f'                    print(\"Replace failed:\", e)')
                    lines.append(f'        btn_{i}.clicked.connect(_on_btn_{i})')
                else:
                    # no-op
                    lines.append(f'        def _on_btn_{i}():')
                    lines.append(f'            print(\"button pressed (no action)\")')
                    lines.append(f'        btn_{i}.clicked.connect(_on_btn_{i})')
                lines.append(f'        self.layout().addWidget(btn_{i})')
            lines.append("")
        return "\n".join(lines)


# -------------------------
# Widget editor dialog
# -------------------------
from PyQt6.QtWidgets import QDialog, QDialogButtonBox
class WidgetEditorDialog(QDialog):
    def __init__(self, parent: Optional[QWidget], spec: Optional[WidgetSpec]):
        super().__init__(parent)
        self.setWindowTitle("Widget Editor")
        self.resize(640, 400)
        self.spec = spec.copy() if spec else {}
        layout = QVBoxLayout()
        self.setLayout(layout)
        form = QFormLayout()
        layout.addLayout(form)

        self.type_combo = QComboBox()
        self.type_combo.addItems(["label", "text", "button"])
        if self.spec.get("type"):
            self.type_combo.setCurrentText(self.spec["type"])
        form.addRow("Type:", self.type_combo)

        self.label_input = QLineEdit(self.spec.get("label",""))
        form.addRow("Label / Title:", self.label_input)

        self.text_edit = QTextEdit(self.spec.get("text",""))
        self.text_edit.setFixedHeight(120)
        form.addRow("Text content (for 'text' or default):", self.text_edit)

        # action area for button
        self.action_kind = QComboBox()
        self.action_kind.addItems(["none", "run_shell", "append_line", "replace_line"])
        act = self.spec.get("action",{}) or {}
        self.action_kind.setCurrentText(act.get("kind","none"))
        form.addRow("Button action:", self.action_kind)

        self.cmd_input = QLineEdit(act.get("cmd",""))
        form.addRow("Shell command (run_shell):", self.cmd_input)

        self.append_line_input = QLineEdit(act.get("line",""))
        form.addRow("Line text (append_line / replace_line):", self.append_line_input)

        self.replace_index_input = QSpinBox()
        self.replace_index_input.setMinimum(0)
        self.replace_index_input.setMaximum(10000)
        self.replace_index_input.setValue(int(act.get("index", 0) or 0))
        form.addRow("Replace index (0-based):", self.replace_index_input)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # enable/disable fields on change
        self.type_combo.currentIndexChanged.connect(self._on_type_change)
        self.action_kind.currentIndexChanged.connect(self._on_action_change)
        self._on_type_change()
        self._on_action_change()

    def _on_type_change(self):
        t = self.type_combo.currentText()
        if t == "label":
            self.label_input.setEnabled(True)
            self.text_edit.setEnabled(False)
            self.action_kind.setEnabled(False)
        elif t == "text":
            self.label_input.setEnabled(False)
            self.text_edit.setEnabled(True)
            self.action_kind.setEnabled(False)
        else:  # button
            self.label_input.setEnabled(True)
            self.text_edit.setEnabled(False)
            self.action_kind.setEnabled(True)

    def _on_action_change(self):
        kind = self.action_kind.currentText()
        self.cmd_input.setEnabled(kind == "run_shell")
        self.append_line_input.setEnabled(kind in ("append_line","replace_line"))
        self.replace_index_input.setEnabled(kind == "replace_line")

    def get_spec(self) -> WidgetSpec:
        t = self.type_combo.currentText()
        spec: WidgetSpec = {"type": t}
        if t in ("label","button"):
            spec["label"] = self.label_input.text().strip()
        if t == "text":
            spec["text"] = self.text_edit.toPlainText()
        if t == "button":
            kind = self.action_kind.currentText()
            if kind == "none":
                spec["action"] = {"kind":"none"}
            elif kind == "run_shell":
                spec["action"] = {"kind":"run_shell", "cmd": self.cmd_input.text()}
            elif kind == "append_line":
                spec["action"] = {"kind":"append_line", "line": self.append_line_input.text()}
            elif kind == "replace_line":
                spec["action"] = {"kind":"replace_line", "index": int(self.replace_index_input.value()), "line": self.append_line_input.text()}
        return spec

# -------------------------
# Entrypoint
# -------------------------
def main():
    app = QApplication([])
    wnd = BuilderMain()
    wnd.show()
    app.exec()

if __name__ == "__main__":
    main()
