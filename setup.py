"""Nur für Installationen über pip/pipx (pyproject.toml): ``CHANGELOG.md`` aus dem Projektordner ins Paket legen,
damit „Neu bei uns“ es findet (``clientinfo.changelog_candidates``). PKGBUILD und ``install.sh --user`` kopieren die
Datei selbst."""
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

HERE = Path(__file__).resolve().parent


class BuildPyWithChangelog(build_py):
    def run(self) -> None:
        super().run()
        src = HERE / "CHANGELOG.md"
        if src.is_file():
            dest = Path(self.build_lib) / "galaxy_linux" / "CHANGELOG.md"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)


setup(cmdclass={"build_py": BuildPyWithChangelog})
