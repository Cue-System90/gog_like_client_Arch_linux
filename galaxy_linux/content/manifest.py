"""Datenmodelle und Parser für Build- und Depot-Manifeste des GOG Content-Systems v2."""
from __future__ import annotations

from ..i18n import tr

import posixpath
from dataclasses import dataclass, field
from typing import Any, Iterable


class ManifestError(Exception):
    pass


def normalize_path(p: str) -> str:
    """Windows-Pfad aus dem Manifest in einen sicheren relativen POSIX-Pfad wandeln."""
    p = (p or "").replace("\\", "/")
    while p.startswith("/") or p.startswith("./"):
        p = p[2:] if p.startswith("./") else p[1:]
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if any(x == ".." for x in parts):
        raise ManifestError(tr('Unzulässiger Pfad im Manifest: {0!r}').format(p))
    return "/".join(parts)


@dataclass
class Build:
    build_id: str
    generation: int
    version_name: str = ""
    tags: list[str] = field(default_factory=list)
    branch: str | None = None
    date_published: str = ""
    link: str = ""
    legacy_build_id: str | None = None

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Build":
        return cls(
            build_id=str(d.get("build_id", "")),
            generation=int(d.get("generation", 0) or 0),
            version_name=str(d.get("version_name") or ""),
            tags=list(d.get("tags") or []),
            branch=d.get("branch"),
            date_published=str(d.get("date_published") or ""),
            link=str(d.get("link") or ""),
            legacy_build_id=str(d["legacy_build_id"]) if d.get("legacy_build_id") else None,
        )

    @property
    def is_v2(self) -> bool:
        return self.generation == 2

    @property
    def label(self) -> str:
        b = f" [{self.branch}]" if self.branch else ""
        return f"{self.version_name or self.build_id}{b} · {self.date_published[:10]}"


@dataclass(frozen=True)
class Chunk:
    md5: str
    compressed_md5: str
    size: int
    compressed_size: int

    @classmethod
    def from_json(cls, c: dict[str, Any]) -> "Chunk":
        return cls(
            md5=str(c.get("md5", "")),
            compressed_md5=str(c.get("compressedMd5") or c.get("md5", "")),
            size=int(c.get("size", 0) or 0),
            compressed_size=int(c.get("compressedSize") or c.get("size", 0) or 0),
        )


@dataclass
class DepotFile:
    path: str
    chunks: list[Chunk]
    flags: list[str] = field(default_factory=list)
    md5: str | None = None
    sha256: str | None = None
    sfc_ref: tuple[int, int] | None = None   # (offset, size) im SmallFilesContainer
    product_id: str = ""
    depot_manifest: str = ""

    @property
    def size(self) -> int:
        return sum(c.size for c in self.chunks)

    @property
    def compressed_size(self) -> int:
        return 0 if self.sfc_ref else sum(c.compressed_size for c in self.chunks)

    @property
    def executable(self) -> bool:
        return "executable" in self.flags

    @property
    def is_support(self) -> bool:
        return "support" in self.flags


@dataclass
class DepotLink:
    path: str
    target: str


@dataclass
class DepotManifest:
    files: list[DepotFile]
    directories: list[str]
    links: list[DepotLink]
    small_files_container: list[Chunk]

    @classmethod
    def parse(cls, d: dict[str, Any], product_id: str = "", depot_manifest: str = "") -> "DepotManifest":
        depot = d.get("depot") or d
        files: list[DepotFile] = []
        dirs: list[str] = []
        links: list[DepotLink] = []
        for it in depot.get("items") or []:
            typ = it.get("type")
            if typ == "DepotFile":
                sfc = it.get("sfcRef")
                files.append(DepotFile(
                    path=normalize_path(it.get("path", "")),
                    chunks=[Chunk.from_json(c) for c in it.get("chunks") or []],
                    flags=list(it.get("flags") or []),
                    md5=it.get("md5"),
                    sha256=it.get("sha256"),
                    sfc_ref=(int(sfc["offset"]), int(sfc["size"])) if sfc else None,
                    product_id=product_id,
                    depot_manifest=depot_manifest,
                ))
            elif typ == "DepotDirectory":
                dirs.append(normalize_path(it.get("path", "")))
            elif typ == "DepotLink":
                links.append(DepotLink(normalize_path(it.get("path", "")), str(it.get("target", ""))))
        sfc = depot.get("smallFilesContainer") or {}
        return cls(files, dirs, links, [Chunk.from_json(c) for c in sfc.get("chunks") or []])


@dataclass
class Depot:
    manifest: str
    size: int = 0
    compressed_size: int = 0
    product_id: str = ""
    languages: list[str] = field(default_factory=list)
    is_gog_depot: bool = False
    os_bitness: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Depot":
        return cls(
            manifest=str(d.get("manifest", "")),
            size=int(d.get("size", 0) or 0),
            compressed_size=int(d.get("compressedSize", 0) or 0),
            product_id=str(d.get("productId", "")),
            languages=list(d.get("languages") or ["*"]),
            is_gog_depot=bool(d.get("isGogDepot", False)),
            os_bitness=[str(x) for x in d.get("osBitness") or []],
        )

    def matches_language(self, lang: str) -> bool:
        if "*" in self.languages or not self.languages:
            return True
        return _lang_match(lang, self.languages)


def _lang_match(wanted: str, available: Iterable[str]) -> bool:
    w = wanted.lower()
    for a in available:
        a = a.lower()
        if a == w or a.split("-")[0] == w.split("-")[0]:
            return True
    return False


def pick_language(available: Iterable[str], wanted: str) -> str:
    av = [a for a in available if a and a != "*"]
    if not av:
        return wanted
    w = wanted.lower()
    for a in av:
        if a.lower() == w:
            return a
    for a in av:
        if a.lower().split("-")[0] == w.split("-")[0]:
            return a
    for a in av:
        if a.lower().startswith("en"):
            return a
    return av[0]


@dataclass
class BuildManifest:
    build_id: str
    base_product_id: str
    install_directory: str
    platform: str
    version: int
    depots: list[Depot]
    dependencies: list[str]
    products: list[dict[str, str]]
    client_id: str = ""
    client_secret: str = ""
    offline_depot: Depot | None = None
    tags: list[str] = field(default_factory=list)
    script_interpreter: bool = False

    @classmethod
    def parse(cls, d: dict[str, Any]) -> "BuildManifest":
        if int(d.get("version", 0) or 0) != 2:
            raise ManifestError(tr('Nur Content-System-Manifeste der Generation 2 werden unterstützt'))
        return cls(
            build_id=str(d.get("buildId", "")),
            base_product_id=str(d.get("baseProductId", "")),
            install_directory=str(d.get("installDirectory") or ""),
            platform=str(d.get("platform") or "windows"),
            version=2,
            depots=[Depot.from_json(x) for x in d.get("depots") or []],
            dependencies=[str(x) for x in d.get("dependencies") or []],
            products=[{"productId": str(p.get("productId", "")), "name": str(p.get("name", ""))}
                      for p in d.get("products") or []],
            client_id=str(d.get("clientId") or ""),
            client_secret=str(d.get("clientSecret") or ""),
            offline_depot=Depot.from_json(d["offlineDepot"]) if d.get("offlineDepot") else None,
            tags=list(d.get("tags") or []),
            script_interpreter=bool(d.get("scriptInterpreter", False)),
        )

    @property
    def available_languages(self) -> list[str]:
        out: list[str] = []
        for dep in self.depots:
            for lang in dep.languages:
                if lang != "*" and lang not in out:
                    out.append(lang)
        return out

    def select_depots(self, language: str, owned_ids: set[str] | None, include_dlcs: bool = True) -> tuple[list[Depot], str]:
        """Depots für Sprache + Besitz (Basisspiel immer, DLCs nur wenn im Besitz)."""
        lang = pick_language(self.available_languages, language)
        chosen: list[Depot] = []
        for dep in self.depots:
            is_base = dep.product_id in ("", self.base_product_id)
            if not is_base:
                if not include_dlcs:
                    continue
                if owned_ids is not None and dep.product_id not in owned_ids:
                    continue
            if dep.matches_language(lang):
                chosen.append(dep)
        return chosen, lang

    def dlc_ids(self) -> list[str]:
        return [p["productId"] for p in self.products if p["productId"] and p["productId"] != self.base_product_id]


@dataclass
class DependencyDepot:
    dependency_id: str
    manifest: str
    readable_name: str = ""
    executable_path: str = ""
    executable_args: str = ""
    internal: bool = False
    compressed_size: int = 0
    os_bitness: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "DependencyDepot":
        ex = d.get("executable") or {}
        return cls(
            dependency_id=str(d.get("dependencyId", "")),
            manifest=str(d.get("manifest", "")),
            readable_name=str(d.get("readableName") or d.get("dependencyId", "")),
            executable_path=normalize_path(ex.get("path", "")) if ex.get("path") else "",
            executable_args=str(ex.get("arguments") or ""),
            internal=bool(d.get("internal", False)),
            compressed_size=int(d.get("compressedSize", 0) or 0),
            os_bitness=[str(x) for x in d.get("osBitness") or []],
            languages=list(d.get("languages") or ["*"]),
        )


def parse_dependency_repository(d: dict[str, Any]) -> dict[str, DependencyDepot]:
    out: dict[str, DependencyDepot] = {}
    for x in d.get("depots") or []:
        dep = DependencyDepot.from_json(x)
        if dep.dependency_id:
            out[dep.dependency_id] = dep
    return out


def dirname(p: str) -> str:
    return posixpath.dirname(p)
