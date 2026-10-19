"""Erkennung von Kompatibilitätstools (Proton, Wine, umu) analog zu Steams Auswahlliste."""
from __future__ import annotations

from ..i18n import tr

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

try:  # python-vdf, optional
    import vdf as _vdf
except Exception:  # pragma: no cover
    _vdf = None


@dataclass(frozen=True)
class CompatTool:
    id: str
    name: str
    kind: str               # proton | wine | umu-latest | native
    path: str = ""          # Proton-Verzeichnis bzw. Wine-Binary bzw. umu-Token
    version: str = ""
    source: str = ""
    requires_runtime: str = ""  # Steam-AppID der benötigten Steam Linux Runtime

    @property
    def label(self) -> str:
        return f"{self.name} · {self.source}" if self.source else self.name

    @property
    def is_proton(self) -> bool:
        return self.kind == "proton"


NATIVE = CompatTool("native", tr('Nativ (kein Kompatibilitätstool)'), "native")
UMU_GE = CompatTool("umu:GE-Proton", tr('GE-Proton (neueste Version, Download über umu)'), "umu-latest", "GE-Proton", "", "umu")
UMU_UMU = CompatTool("umu:UMU-Proton", tr('UMU-Proton (neueste Version, Download über umu)'), "umu-latest", "UMU-Proton", "", "umu")

HOME = Path.home()
STEAM_ROOT_CANDIDATES = [
    HOME / ".steam" / "root",
    HOME / ".steam" / "steam",
    HOME / ".local" / "share" / "Steam",
    HOME / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
    HOME / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
]
COMPAT_TOOLS_DIRS = [
    HOME / ".local" / "share" / "Steam" / "compatibilitytools.d",
    HOME / ".steam" / "root" / "compatibilitytools.d",
    HOME / ".steam" / "steam" / "compatibilitytools.d",
    HOME / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam" / "compatibilitytools.d",
    Path("/usr/share/steam/compatibilitytools.d"),
    Path("/usr/local/share/steam/compatibilitytools.d"),
]
HEROIC_PROTON_DIRS = [
    HOME / ".config" / "heroic" / "tools" / "proton",
    HOME / ".var" / "app" / "com.heroicgameslauncher.hgl" / "config" / "heroic" / "tools" / "proton",
]
LUTRIS_WINE_DIRS = [
    HOME / ".local" / "share" / "lutris" / "runners" / "wine",
    HOME / ".var" / "app" / "net.lutris.Lutris" / "data" / "lutris" / "runners" / "wine",
]
BOTTLES_RUNNER_DIRS = [
    HOME / ".local" / "share" / "bottles" / "runners",
    HOME / ".var" / "app" / "com.usebottles.bottles" / "data" / "bottles" / "runners",
]
RUNTIME_NAMES = {
    "1070560": "SteamLinuxRuntime",
    "1391110": "SteamLinuxRuntime_soldier",
    "1628350": "SteamLinuxRuntime_sniper",
}


# --------------------------------------------------------------------------- VDF
def parse_vdf(text: str) -> dict:
    if _vdf is not None:
        try:
            return _vdf.loads(text)
        except Exception:
            pass
    return _parse_vdf_minimal(text)


def _parse_vdf_minimal(text: str) -> dict:
    tokens = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*', text)
    stack: list[dict] = [{}]
    key: str | None = None
    for s, brace in tokens:
        if brace == "{":
            new: dict = {}
            if key is not None:
                stack[-1][key] = new
            stack.append(new)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif s is not None and (s or brace == ""):
            if key is None:
                key = s
            else:
                stack[-1][key] = s
                key = None
    return stack[0]


def _read_vdf(path: Path) -> dict:
    try:
        return parse_vdf(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


# --------------------------------------------------------------------------- Steam
def steam_roots() -> list[Path]:
    out: list[Path] = []
    for c in STEAM_ROOT_CANDIDATES:
        try:
            if c.is_dir():
                r = c.resolve()
                if r not in out:
                    out.append(r)
        except OSError:
            continue
    return out


def steam_root() -> Path | None:
    roots = steam_roots()
    return roots[0] if roots else None


def steam_library_folders() -> list[Path]:
    """Alle <Bibliothek>/steamapps-Verzeichnisse laut libraryfolders.vdf."""
    libs: list[Path] = []
    for root in steam_roots():
        sa = root / "steamapps"
        if sa.is_dir() and sa not in libs:
            libs.append(sa)
        data = _read_vdf(sa / "libraryfolders.vdf")
        folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
        for _, v in folders.items():
            p = v.get("path") if isinstance(v, dict) else v
            if isinstance(p, str):
                lp = Path(p) / "steamapps"
                if lp.is_dir() and lp not in libs:
                    libs.append(lp)
    return libs


def find_runtime(appid: str) -> Path | None:
    """Findet die Steam Linux Runtime (Container) mit _v2-entry-point für eine AppID."""
    if not appid:
        return None
    for lib in steam_library_folders():
        acf = _read_vdf(lib / f"appmanifest_{appid}.acf")
        installdir = (acf.get("AppState") or {}).get("installdir")
        cands = [lib / "common" / installdir] if installdir else []
        name = RUNTIME_NAMES.get(appid)
        if name:
            cands.append(lib / "common" / name)
        for c in cands:
            if (c / "_v2-entry-point").exists():
                return c
    return None


# --------------------------------------------------------------------------- Werkzeuge
def _natural_key(s: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _proton_version(d: Path) -> str:
    try:
        parts = (d / "version").read_text().split()
        return parts[1] if len(parts) > 1 else (parts[0] if parts else "")
    except OSError:
        return ""


def _proton_display_name(d: Path) -> str:
    data = _read_vdf(d / "compatibilitytool.vdf")
    tools = (data.get("compatibilitytools") or {}).get("compat_tools") or {}
    for _, v in tools.items():
        if isinstance(v, dict) and v.get("display_name"):
            return str(v["display_name"])
    return d.name


def _requires_runtime(d: Path) -> str:
    data = _read_vdf(d / "toolmanifest.vdf")
    return str((data.get("manifest") or {}).get("require_tool_appid") or "")


def _proton_tool(d: Path, source: str) -> CompatTool | None:
    try:
        if not (d / "proton").is_file():
            return None
        d = d.resolve()
    except OSError:
        return None
    return CompatTool(
        id=f"proton:{d}",
        name=_proton_display_name(d),
        kind="proton",
        path=str(d),
        version=_proton_version(d),
        source=source,
        requires_runtime=_requires_runtime(d),
    )


def _wine_tool(bin_path: Path, source: str, name: str | None = None) -> CompatTool | None:
    try:
        if not bin_path.is_file() or not os.access(bin_path, os.X_OK):
            return None
        bin_path = bin_path.resolve()
    except OSError:
        return None
    return CompatTool(id=f"wine:{bin_path}", name=name or bin_path.parent.parent.name, kind="wine",
                      path=str(bin_path), source=source)


def _iter_dirs(p: Path):
    try:
        return sorted((c for c in p.iterdir() if c.is_dir()), key=lambda c: _natural_key(c.name), reverse=True)
    except OSError:
        return []


def umu_available() -> bool:
    return shutil.which("umu-run") is not None


def umu_path() -> str | None:
    return shutil.which("umu-run")


_wine_version_cache: dict[str, str] = {}


def system_wine() -> CompatTool | None:
    w = shutil.which("wine")
    if not w:
        return None
    ver = _wine_version_cache.get(w)
    if ver is None:
        try:
            ver = subprocess.run([w, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            ver = ""
        _wine_version_cache[w] = ver
    return CompatTool(id=f"wine:{w}", name=tr('System-Wine ({0})').format(ver or 'wine'), kind="wine", path=w,
                      version=ver, source="System")


def discover(include_umu_latest: bool = True, include_wine: bool = True) -> list[CompatTool]:
    """Liefert alle gefundenen Kompatibilitätstools, neueste zuerst, ohne Duplikate."""
    tools: list[CompatTool] = []
    seen: set[str] = set()

    def add(t: CompatTool | None) -> None:
        if t and t.id not in seen:
            seen.add(t.id)
            tools.append(t)

    for cdir in COMPAT_TOOLS_DIRS:
        src = "System" if str(cdir).startswith("/usr") else "compatibilitytools.d"
        for d in _iter_dirs(cdir):
            add(_proton_tool(d, src))
    for lib in steam_library_folders():
        for d in _iter_dirs(lib / "common"):
            if d.name.lower().startswith("proton"):
                add(_proton_tool(d, "Steam"))
    for hdir in HEROIC_PROTON_DIRS:
        for d in _iter_dirs(hdir):
            add(_proton_tool(d, "Heroic"))
    if include_umu_latest and umu_available():
        add(UMU_GE)
        add(UMU_UMU)
    if include_wine:
        add(system_wine())
        for ldir in LUTRIS_WINE_DIRS:
            for d in _iter_dirs(ldir):
                add(_wine_tool(d / "bin" / "wine", "Lutris", d.name))
        for bdir in BOTTLES_RUNNER_DIRS:
            for d in _iter_dirs(bdir):
                add(_wine_tool(d / "bin" / "wine", "Bottles", d.name))
    return tools


def resolve(tool_id: str, tools: list[CompatTool] | None = None) -> CompatTool | None:
    if not tool_id or tool_id == "auto":
        return None
    if tool_id == NATIVE.id:
        return NATIVE
    tools = tools if tools is not None else discover()
    for t in tools:
        if t.id == tool_id:
            return t
    # Pfad-basierte IDs auch dann akzeptieren, wenn das Tool nicht mehr in der Liste steht
    if tool_id.startswith("proton:"):
        return _proton_tool(Path(tool_id[len("proton:"):]), "manuell")
    if tool_id.startswith("wine:"):
        return _wine_tool(Path(tool_id[len("wine:"):]), "manuell")
    if tool_id.startswith("umu:"):
        return CompatTool(tool_id, tool_id[4:] + " (umu)", "umu-latest", tool_id[4:], "", "umu")
    return None


def default_tool(tools: list[CompatTool] | None = None, prefer_umu: bool = True) -> CompatTool | None:
    """Automatische Wahl: System-Proton (z. B. proton-cachyos) > neuestes Proton > umu-GE > Wine."""
    tools = tools if tools is not None else discover()
    protons = [t for t in tools if t.kind == "proton"]
    system = [t for t in protons if t.source == "System"]
    if system:
        return system[0]
    if protons:
        return protons[0]
    if prefer_umu:
        for t in tools:
            if t.kind == "umu-latest":
                return t
    for t in tools:
        if t.kind == "wine":
            return t
    return None


def tool_needs_umu(tool: CompatTool) -> bool:
    """SLR-Protons (toolmanifest mit require_tool_appid) brauchen den Container – ohne Steam-Runtime nur per umu."""
    if tool.kind == "umu-latest":
        return True
    if tool.kind == "proton" and tool.requires_runtime:
        return find_runtime(tool.requires_runtime) is None
    return False
