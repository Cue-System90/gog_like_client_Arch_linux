"""Sicherheitsnetz für alle Tests: HOME und XDG_* zeigen auf einen eigenen Temp-Ordner, bevor galaxy_linux geladen wird.
So kann kein Test versehentlich die echten Daten (~/.config/galaxy-linux, ~/.local/share/galaxy-linux, GOverlays
nonsteam_folders.txt oder Cover-Cache) anfassen, auch wenn er vergisst, einen Pfad umzubiegen."""
import atexit
import os
import shutil
import tempfile

_HOME = tempfile.mkdtemp(prefix="galaxy-linux-tests-")
atexit.register(shutil.rmtree, _HOME, True)
os.environ["HOME"] = _HOME
for _var, _sub in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"), ("XDG_CACHE_HOME", ".cache")):
    os.environ[_var] = os.path.join(_HOME, _sub)
    os.makedirs(os.environ[_var], exist_ok=True)
os.environ.pop("HOST_XDG_DATA_HOME", None)
