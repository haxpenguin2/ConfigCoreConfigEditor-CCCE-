#!/usr/bin/env python3
"""
config_core.py

Core library (ConfigFile) + GUI shell that auto-senses packages in a GitHub repo,
validates compatibility via plugin manifest (plugin.json), and allows installing
compatible packages into ~/.config_editor_packages/<package>.

Usage:
  python3 config_core.py

Requirements:
  - PyQt6 for GUI: pip3 install PyQt6
  - (optional) PyYAML if you want plugin manifests in YAML: pip3 install PyYAML

Notes:
 - The GitHub repo scanned is hard-coded below (GITHUB_OWNER, GITHUB_REPO).
 - You may set a GitHub personal access token (read-only) in the UI to increase
   rate limits or access private repos.
"""

from __future__ import annotations
import os
import sys
import shutil
import zipfile
import tempfile
import json
import base64
import traceback
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# PyQt6 imports
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QMessageBox, QLineEdit,
    QFrame, QTextEdit, QTabWidget, QGroupBox, QGridLayout, QComboBox,
    QSplitter, QSizePolicy
)
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtCore import Qt

# Core version used for manifest compatibility checks
__version__ = "0.2.0"

# GitHub repo to scan for plugin packages (your repo)
GITHUB_OWNER = "haxpenguin2"
GITHUB_REPO = "ConfigCoreModules"
GITHUB_API_ROOT = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/"

# Where installed packages land
PACKAGES_DIR = Path.home() / ".config_editor_packages"

# -------------------------
# Basic file helpers & ConfigFile
# -------------------------
def ensure_packages_dir() -> Path:
    PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
    return PACKAGES_DIR

def read_file_lines(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().splitlines()

def write_file_lines(path: str, lines: List[str]) -> None:
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def make_backup(path: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = f"{path}.bak.{ts}"
    shutil.copy2(path, bak)
    return bak

def leading_whitespace(line: str) -> str:
    return line[:len(line) - len(line.lstrip("\t "))]

def is_commented(line: str) -> bool:
    return line.lstrip().startswith("#")

def comment_line(line: str) -> str:
    if is_commented(line):
        return line
    lw = leading_whitespace(line)
    return lw + "#" + line[len(lw):]

def uncomment_line(line: str) -> str:
    s = line.lstrip()
    if not s.startswith("#"):
        return line
    lw = leading_whitespace(line)
    rest = line[len(lw):]
    return lw + rest.replace("#", "", 1)

class ConfigFile:
    """
    In-memory editable representation of a text config file.
    Replace whole lines or append. Save with backup.
    """
    def __init__(self, path: str):
        self.path = str(Path(path).expanduser())
        if not os.path.isfile(self.path):
            raise FileNotFoundError(self.path)
        self._orig_lines: List[str] = []
        self.lines: List[str] = []
        self.load()

    def load(self) -> None:
        self._orig_lines = read_file_lines(self.path)
        self.lines = list(self._orig_lines)

    def line_count(self) -> int:
        return len(self.lines)

    def get_line(self, idx: int) -> str:
        return self.lines[idx]

    def replace_line(self, idx: int, new_line: str) -> None:
        if not (0 <= idx < len(self.lines)):
            raise IndexError("index out of range")
        self.lines[idx] = new_line

    def append_line(self, new_line: str) -> None:
        self.lines.append(new_line)

    def insert_line(self, idx: int, new_line: str) -> None:
        self.lines.insert(idx, new_line)

    def save(self, backup: bool = True) -> str:
        bak = ""
        if backup:
            bak = make_backup(self.path)
        write_file_lines(self.path, self.lines)
        self.load()
        return bak

    def discard_changes(self) -> None:
        self.load()

# -------------------------
# GitHub API helpers (no external deps)
# -------------------------
def github_api_get(path: str, token: Optional[str] = None) -> Tuple[int, dict]:
    """
    GET a GitHub contents API path (returns HTTP status and parsed JSON).
    `path` should be a path relative to repo root or empty string for root.
    """
    url = GITHUB_API_ROOT + path
    req = Request(url, headers={"Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", f"token {token}")
    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw)
    except HTTPError as e:
        try:
            body = e.read().decode()
            data = json.loads(body) if body else {}
        except Exception:
            data = {}
        return e.code, data
    except URLError as e:
        raise RuntimeError(f"Network error: {e}") from e

def list_remote_packages(token: Optional[str] = None) -> List[dict]:
    """
    Return list of subfolders at repo root. Each item is the JSON object GitHub returns.
    """
    status, data = github_api_get("", token=token)
    if status != 200:
        raise RuntimeError(f"GitHub API returned status {status}: {data.get('message','')}")
    # filter directories only
    return [item for item in data if item.get("type") == "dir"]

def fetch_remote_manifest(package_name: str, token: Optional[str] = None) -> dict:
    """
    Try to fetch plugin manifest JSON from package folder. Supports plugin.json or manifest.json.
    Returns parsed manifest dict or empty dict if not found or invalid.
    """
    for filename in ("plugin.json", "manifest.json"):
        status, data = github_api_get(f"{package_name}/{filename}", token=token)
        if status == 200 and isinstance(data, dict) and "content" in data:
            try:
                raw = base64.b64decode(data["content"]).decode("utf-8")
                return json.loads(raw)
            except Exception:
                # fail quietly and continue
                try:
                    import yaml  # optional
                    return yaml.safe_load(raw)
                except Exception:
                    return {}
    return {}

# -------------------------
# Manifest compatibility checks
# -------------------------
def _parse_version(v: str) -> Tuple[int, ...]:
    # parse '1.2.3' -> (1,2,3)
    parts = []
    for p in v.strip().split("."):
        try:
            parts.append(int(p))
        except Exception:
            parts.append(0)
    return tuple(parts)

def is_plugin_compatible(manifest: dict, core_version: str = __version__) -> bool:
    """
    Simple compatibility rules:
      - If manifest has 'min_core_version': core >= min_core_version required
      - If manifest has 'compatible_core_versions': core must be exactly one of them
      - Otherwise assume compatible
    """
    try:
        if not manifest:
            return True
        core_v = _parse_version(core_version)
        minv = manifest.get("min_core_version")
        if minv:
            return core_v >= _parse_version(str(minv))
        compat_list = manifest.get("compatible_core_versions") or manifest.get("compatible_versions")
        if compat_list:
            # list of strings
            for v in compat_list:
                if core_v == _parse_version(str(v)):
                    return True
            return False
        return True
    except Exception:
        return False

# -------------------------
# Installer (download zip and extract package subfolder)
# -------------------------
def install_package_from_github(package_folder: str, branch: str = "main", token: Optional[str] = None) -> str:
    """
    Download the repo zip for `branch`, extract only the subfolder `package_folder`
    into ~/.config_editor_packages/<package_folder> and return installed path.
    """
    repo_url = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/archive/refs/heads/{branch}.zip"
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        req = Request(repo_url, headers={"User-Agent": "ConfigCoreInstaller/1.0"})
        if token:
            req.add_header("Authorization", f"token {token}")
        with urlopen(req, timeout=30) as resp, open(tmp_path, "wb") as out:
            out.write(resp.read())
        with zipfile.ZipFile(tmp_path, "r") as zf:
            members = zf.namelist()
            if not members:
                raise FileNotFoundError("Repo zip appears empty")
            # detect root like 'ConfigCoreModules-main/'
            root = members[0].split("/", 1)[0] + "/"
            target_prefix = f"{root}{package_folder}/"
            matched = [m for m in members if m.startswith(target_prefix)]
            if not matched:
                raise FileNotFoundError(f"Package folder '{package_folder}' not found in repo zip")
            dest = ensure_packages_dir() / package_folder
            # remove existing to replace
            if dest.exists():
                shutil.rmtree(dest)
            for member in matched:
                rel = member[len(target_prefix):]
                if rel == "":
                    continue
                target = dest / rel
                if member.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        dst.write(src.read())
            return str(dest)
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass

# -------------------------
# Plugin discovery & import helpers
# -------------------------
def list_installed_packages() -> List[str]:
    d = ensure_packages_dir()
    return sorted([p.name for p in d.iterdir() if p.is_dir()])

def read_local_manifest(package_path: Path) -> dict:
    """
    Read plugin.json or manifest.json from installed package folder, return dict or {}
    """
    for fn in ("plugin.json", "manifest.json"):
        p = package_path / fn
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                try:
                    import yaml
                    return yaml.safe_load(p.read_text(encoding="utf-8"))
                except Exception:
                    return {}
    return {}

def find_plugins_paths() -> List[Path]:
    roots: List[Path] = []
    local_plugins = Path.cwd() / "plugins"
    if local_plugins.is_dir():
        roots.append(local_plugins)
    pkgs = ensure_packages_dir()
    for sub in pkgs.iterdir():
        if sub.is_dir():
            # if a manifest declares incompatibility, plugin won't be loaded later
            roots.append(sub)
    # detect single-file plugin modules in cwd ending with _plugin.py
    for f in Path.cwd().iterdir():
        if f.is_file() and f.name.endswith("_plugin.py"):
            roots.append(f)
    # keep unique order-preserving
    seen = []
    uniq = []
    for r in roots:
        key = str(r.resolve())
        if key not in seen:
            seen.append(key)
            uniq.append(r)
    return uniq

def import_plugin_module(root: Path):
    import importlib, importlib.util
    try:
        if root.is_dir():
            # try import by folder name if it's a package
            parent = str(root.parent)
            modname = root.name
            if (root / "__init__.py").exists():
                if parent not in sys.path:
                    sys.path.insert(0, parent)
                try:
                    return importlib.import_module(modname)
                except Exception:
                    pass
            # else try to find plugin.py or <name>_plugin.py
            candidates = [root / "plugin.py", root / f"{modname}_plugin.py", root / "__init__.py"]
            for c in candidates:
                if c.exists():
                    spec = importlib.util.spec_from_file_location(f"{modname}_file", str(c))
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    return mod
        else:
            # single file plugin
            spec = importlib.util.spec_from_file_location(root.stem, str(root))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    except Exception:
        traceback.print_exc()
        return None

# -------------------------
# GUI shell with remote sensing & compatibility checks
# -------------------------
class CoreGUI(QMainWindow):
    def __init__(self, config_path: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("Config Core — Editor Host")
        self.resize(1100, 760)
        self.config_path = config_path or self.auto_find_config()
        self.config = None
        if self.config_path:
            try:
                self.config = ConfigFile(self.config_path)
            except Exception as e:
                QMessageBox.critical(self, "Config load failed", f"Failed to open config: {e}")
                self.config = None

        # dark modern stylesheet
        self.setStyleSheet("""
            QWidget { background: #071018; color: #dbe7f5; font-family: "Segoe UI", Roboto, sans-serif; }
            QGroupBox { border: 1px solid #122126; border-radius: 8px; padding: 8px; margin-top: 6px; }
            QPushButton { background: #0f2a33; border: 1px solid #22434f; padding: 6px 10px; border-radius: 6px; color: #e6eef8; }
            QPushButton:hover { background: #133b45; }
            QLineEdit, QTextEdit, QComboBox { background: #071018; border: 1px solid #122126; padding: 6px; border-radius: 6px; color: #e6eef8; }
            QListWidget { background: #071018; border: 1px solid #122126; }
        """)

        central = QWidget()
        self.setCentralWidget(central)
        main = QVBoxLayout()
        central.setLayout(main)

        header = QHBoxLayout()
        main.addLayout(header)
        self.cfg_label = QLabel(f"Config: {self.config_path or '(none)'}")
        self.cfg_label.setFont(QFont("monospace", 10))
        header.addWidget(self.cfg_label)
        header.addStretch()
        reload_btn = QPushButton("Reload plugins")
        reload_btn.clicked.connect(self.reload_plugins)
        header.addWidget(reload_btn)

        main.addSpacing(6)
        self.tabs = QTabWidget()
        main.addWidget(self.tabs)

        # Editors tab contains plugin tabs
        self.editors_tab = QWidget()
        self.editors_layout = QVBoxLayout()
        self.editors_tab.setLayout(self.editors_layout)
        self.tabs.addTab(self.editors_tab, "Editors")
        self.plugin_tabs = QTabWidget()
        self.editors_layout.addWidget(self.plugin_tabs)

        # Packages tab
        self.pack_tab = QWidget()
        self.pack_layout = QVBoxLayout()
        self.pack_tab.setLayout(self.pack_layout)
        self.tabs.addTab(self.pack_tab, "Packages")

        self._build_packages_ui()
        # initial plugin load
        self.reload_plugins()

    def auto_find_config(self) -> Optional[str]:
        possible = [
            os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "i3", "config"),
            os.path.join(os.path.expanduser("~/.config"), "i3", "config"),
            os.path.join(os.path.expanduser("~/.i3"), "config"),
        ]
        for p in possible:
            if os.path.isfile(p):
                return p
        return None

    # -------------------------
    # Packages UI building
    # -------------------------
    def _build_packages_ui(self):
        info = QLabel(f"Remote repo: {GITHUB_OWNER}/{GITHUB_REPO}  — core version: {__version__}")
        info.setWordWrap(True)
        self.pack_layout.addWidget(info)

        grid = QGridLayout()
        self.pack_layout.addLayout(grid)
        grid.addWidget(QLabel("GitHub token (optional):"), 0, 0)
        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText("Paste token here to increase rate limits / access private repo")
        grid.addWidget(self.token_input, 0, 1, 1, 3)
        refresh_remote_btn = QPushButton("Refresh remote list")
        refresh_remote_btn.clicked.connect(self.refresh_remote_list)
        grid.addWidget(refresh_remote_btn, 0, 4)

        # two-panel splitter: available remote packages and installed packages
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.pack_layout.addWidget(splitter)

        # left: remote packages
        left_widget = QWidget()
        left_layout = QVBoxLayout()
        left_widget.setLayout(left_layout)
        left_layout.addWidget(QLabel("Available (remote) packages:"))
        self.remote_list = QListWidget()
        left_layout.addWidget(self.remote_list)
        remote_row = QHBoxLayout()
        self.install_btn = QPushButton("Install selected")
        self.install_btn.clicked.connect(self.install_selected_remote)
        remote_row.addWidget(self.install_btn)
        self.update_btn = QPushButton("Update selected (reinstall)")
        self.update_btn.clicked.connect(self.update_selected_remote)
        remote_row.addWidget(self.update_btn)
        left_layout.addLayout(remote_row)
        splitter.addWidget(left_widget)

        # right: installed packages
        right_widget = QWidget()
        right_layout = QVBoxLayout()
        right_widget.setLayout(right_layout)
        right_layout.addWidget(QLabel("Installed packages:"))
        self.installed_list = QListWidget()
        right_layout.addWidget(self.installed_list)
        inst_row = QHBoxLayout()
        rm_btn = QPushButton("Uninstall selected")
        rm_btn.clicked.connect(self.uninstall_selected)
        inst_row.addWidget(rm_btn)
        open_btn = QPushButton("Open folder")
        open_btn.clicked.connect(self.open_selected_folder)
        inst_row.addWidget(open_btn)
        right_layout.addLayout(inst_row)
        splitter.addWidget(right_widget)

        # populate installed initially
        self.refresh_installed_list()
        # refresh remote list on startup
        self.refresh_remote_list()

    def refresh_installed_list(self):
        self.installed_list.clear()
        for name in list_installed_packages():
            # show compatibility info using local manifest
            path = PACKAGES_DIR / name
            manifest = read_local_manifest(path)
            compat = is_plugin_compatible(manifest)
            text = f"{name} {'(compatible)' if compat else '(incompatible)'}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, {"name": name, "manifest": manifest})
            self.installed_list.addItem(item)

    def refresh_remote_list(self):
        self.remote_list.clear()
        token = self.token_input.text().strip() or None
        try:
            dirs = list_remote_packages(token=token)
        except Exception as e:
            QMessageBox.critical(self, "Remote list failed", f"Failed to list remote packages: {e}")
            return
        for d in dirs:
            name = d.get("name")
            manifest = fetch_remote_manifest(name, token=token)
            compat = is_plugin_compatible(manifest)
            display = f"{name} {'(compatible)' if compat else '(incompatible)'}"
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, {"name": name, "manifest": manifest, "remote_obj": d})
            self.remote_list.addItem(item)

    def install_selected_remote(self):
        it = self.remote_list.currentItem()
        if not it:
            QMessageBox.information(self, "Select", "Select a remote package to install.")
            return
        data = it.data(Qt.ItemDataRole.UserRole) or {}
        name = data.get("name")
        manifest = data.get("manifest", {}) or {}
        if not is_plugin_compatible(manifest):
            QMessageBox.warning(self, "Incompatible", f"Package {name} is marked incompatible with core {__version__}. Installation blocked.")
            return
        token = self.token_input.text().strip() or None
        try:
            dest = install_package_from_github(name, branch="main", token=token)
        except Exception as e:
            QMessageBox.critical(self, "Install failed", f"Failed to install {name}: {e}")
            return
        QMessageBox.information(self, "Installed", f"Installed {name} to:\n{dest}")
        self.refresh_installed_list()
        self.reload_plugins()

    def update_selected_remote(self):
        # reinstall/update the selected remote package (same as install but overwrites)
        self.install_selected_remote()

    def uninstall_selected(self):
        it = self.installed_list.currentItem()
        if not it:
            QMessageBox.information(self, "Select", "Select an installed package to remove.")
            return
        data = it.data(Qt.ItemDataRole.UserRole) or {}
        name = data.get("name")
        if not name:
            return
        path = PACKAGES_DIR / name
        if not path.exists():
            QMessageBox.information(self, "Missing", "Package folder not found.")
            self.refresh_installed_list()
            return
        confirm = QMessageBox.question(self, "Confirm uninstall", f"Remove package folder {path}?")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            shutil.rmtree(path)
        except Exception as e:
            QMessageBox.critical(self, "Remove failed", f"Failed to remove {path}: {e}")
            return
        QMessageBox.information(self, "Removed", f"Removed {path}")
        self.refresh_installed_list()
        self.reload_plugins()

    def open_selected_folder(self):
        it = self.installed_list.currentItem()
        if not it:
            QMessageBox.information(self, "Select", "Select a package first.")
            return
        name = (it.data(Qt.ItemDataRole.UserRole) or {}).get("name")
        if not name:
            return
        path = PACKAGES_DIR / name
        if not path.exists():
            QMessageBox.information(self, "Missing", "Package folder not found.")
            return
        try:
            os.execvp("xdg-open", ["xdg-open", str(path)])
        except Exception:
            QMessageBox.information(self, "Open folder", f"Package folder: {path}")

    # -------------------------
    # Plugins loading (skip incompatible)
    # -------------------------
    def reload_plugins(self):
        # clear plugin tabs
        while self.plugin_tabs.count():
            w = self.plugin_tabs.widget(0)
            self.plugin_tabs.removeTab(0)
            w.deleteLater()

        roots = find_plugins_paths()
        loaded = 0
        for root in roots:
            # check local manifest first if root is a folder inside PACKAGES_DIR
            manifest = {}
            if isinstance(root, Path) and root.exists() and root.is_dir():
                manifest = read_local_manifest(root)
                if not is_plugin_compatible(manifest):
                    # show disabled tab with reason
                    txt = QWidget()
                    txt.setLayout(QVBoxLayout())
                    lbl = QLabel(f"Plugin {root.name} is incompatible with core {__version__}. Manifest: {manifest}")
                    lbl.setWordWrap(True)
                    txt.layout().addWidget(lbl)
                    self.plugin_tabs.addTab(txt, f"{root.name} (incompatible)")
                    continue
            mod = import_plugin_module(root)
            if not mod:
                continue
            editor_cls = getattr(mod, "EditorWidget", None)
            editor_factory = getattr(mod, "create_editor", None)
            try:
                if editor_cls:
                    if self.config:
                        instance = editor_cls(self.config)
                    else:
                        instance = editor_cls(None)
                    self.plugin_tabs.addTab(instance, getattr(mod, "__name__", root.name if isinstance(root, Path) else str(root)))
                    loaded += 1
                elif editor_factory:
                    widget = editor_factory(self.config)
                    self.plugin_tabs.addTab(widget, getattr(mod, "__name__", root.name if isinstance(root, Path) else str(root)))
                    loaded += 1
            except Exception:
                traceback.print_exc()
                continue
        self.statusBar().showMessage(f"Loaded {loaded} plugin(s) from {len(roots)} root(s)")
        self.refresh_installed_list()

# -------------------------
# Entrypoint
# -------------------------
def main():
    app = QApplication(sys.argv)
    cfg = None
    # try to find a default i3 config
    candidates = [
        os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "i3", "config"),
        os.path.join(os.path.expanduser("~/.config"), "i3", "config"),
        os.path.join(os.path.expanduser("~/.i3"), "config"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            cfg = p
            break
    wnd = CoreGUI(config_path=cfg)
    wnd.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
