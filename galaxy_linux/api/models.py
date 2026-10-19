"""Datenmodelle für Bibliothekseinträge und Spiel-Metadaten."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

GAMESDB_IMG = "https://images.gog.com"


def _img(url_format: dict | None, formatter: str, ext: str = "jpg") -> str:
    if not url_format or not url_format.get("url_format"):
        return ""
    return url_format["url_format"].replace("{formatter}", formatter).replace("{ext}", ext)


def _localized(v: Any, lang: str = "de-DE") -> str:
    if isinstance(v, dict):
        return v.get(lang) or v.get("*") or v.get("en-US") or next(iter(v.values()), "") or ""
    return v or ""


@dataclass
class LibraryEntry:
    """Ein Eintrag aus embed.gog.com/account/getFilteredProducts."""
    id: str
    title: str
    slug: str = ""
    image: str = ""
    works_on: dict[str, bool] = field(default_factory=dict)
    category: str = ""
    is_game: bool = True
    is_hidden: bool = False
    release_date: str = ""
    dlc_count: int = 0
    updates: int = 0
    is_new: bool = False
    is_in_development: bool = False
    url: str = ""

    @classmethod
    def from_embed(cls, p: dict) -> "LibraryEntry":
        rd = p.get("releaseDate")
        if isinstance(rd, dict):
            rd = rd.get("date", "")
        return cls(
            id=str(p.get("id")),
            title=p.get("title", "") or "",
            slug=p.get("slug", "") or "",
            image=p.get("image", "") or "",
            works_on={k.lower(): bool(v) for k, v in (p.get("worksOn") or {}).items()},
            category=p.get("category", "") or "",
            is_game=bool(p.get("isGame", True)),
            is_hidden=bool(p.get("isHidden", False)),
            release_date=str(rd or ""),
            dlc_count=int(p.get("dlcCount", 0) or 0),
            updates=int(p.get("updates", 0) or 0),
            is_new=bool(p.get("isNew", False)),
            is_in_development=bool(p.get("isInDevelopment", False)),
            url=p.get("url", "") or "",
        )

    @classmethod
    def from_dict(cls, d: dict) -> "LibraryEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return asdict(self)

    def image_url(self, suffix: str = "_196.jpg") -> str:
        img = self.image
        if not img:
            return ""
        if img.startswith("//"):
            img = "https:" + img
        return img + suffix

    @property
    def platforms(self) -> list[str]:
        out = []
        if self.works_on.get("windows"):
            out.append("windows")
        if self.works_on.get("linux"):
            out.append("linux")
        if self.works_on.get("mac"):
            out.append("osx")
        return out

    @property
    def gog_url(self) -> str:
        return ("https://www.gog.com" + self.url) if self.url.startswith("/") else self.url


@dataclass
class GameMeta:
    """Zusammengeführte Metadaten aus gamesdb.gog.com und api.gog.com/products."""
    id: str
    title: str
    summary: str = ""
    vertical_cover: str = ""
    background: str = ""
    square_icon: str = ""
    logo: str = ""
    genres: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    developers: list[str] = field(default_factory=list)
    publishers: list[str] = field(default_factory=list)
    release_date: str = ""
    rating: float | None = None
    dlc_ids: list[str] = field(default_factory=list)
    os_list: list[str] = field(default_factory=list)
    content_system: dict[str, bool] = field(default_factory=dict)
    changelog: str = ""
    gog_url: str = ""
    screenshots: list[str] = field(default_factory=list)

    @classmethod
    def from_sources(cls, entry: LibraryEntry, gamesdb: dict | None, product: dict | None) -> "GameMeta":
        m = cls(id=entry.id, title=entry.title, vertical_cover=entry.image_url("_glx_vertical_cover.jpg"),
                release_date=entry.release_date, os_list=entry.platforms, gog_url=entry.gog_url)
        if product:
            m.title = product.get("title") or m.title
            imgs = product.get("images") or {}
            if imgs.get("background"):
                m.background = "https:" + imgs["background"] if imgs["background"].startswith("//") else imgs["background"]
            if imgs.get("sidebarIcon"):
                m.square_icon = "https:" + imgs["sidebarIcon"] if imgs["sidebarIcon"].startswith("//") else imgs["sidebarIcon"]
            if imgs.get("logo2x"):
                m.logo = "https:" + imgs["logo2x"] if imgs["logo2x"].startswith("//") else imgs["logo2x"]
            desc = product.get("description") or {}
            lead, full = _strip_html(desc.get("lead") or ""), _strip_html(desc.get("full") or "")
            m.summary = (lead + "\n\n" + full).strip() if lead and full and not full.startswith(lead[:40]) else (full or lead)
            m.content_system = {k: bool(v) for k, v in (product.get("content_system_compatibility") or {}).items()}
            rd = product.get("release_date")
            if rd:
                m.release_date = str(rd)[:10]
            cl = product.get("changelog")
            if cl:
                m.changelog = _strip_html(cl)
            m.dlc_ids = [str(d.get("id")) for d in (product.get("expanded_dlcs") or []) if d.get("id")]
            m.screenshots = [
                (s.get("formatted_images") or [{}])[0].get("image_url", "")
                for s in (product.get("screenshots") or [])[:6]
            ]
            m.screenshots = [s for s in m.screenshots if s]
            links = product.get("links") or {}
            if links.get("product_card"):
                m.gog_url = links["product_card"]
        if gamesdb:
            g = gamesdb.get("game") or {}
            title = _localized(gamesdb.get("title"))
            if title:
                m.title = title
            summ = _localized(gamesdb.get("summary")) or _localized(g.get("summary"))
            if summ and len(summ) > len(m.summary):
                m.summary = summ
            vc = _img(gamesdb.get("vertical_cover") or g.get("vertical_cover"), "_glx_vertical_cover")
            if vc:
                m.vertical_cover = vc
            bg = _img(gamesdb.get("background") or g.get("background"), "_glx_bg_top_padding_7")
            if bg:
                m.background = bg
            icon = _img(gamesdb.get("square_icon") or g.get("square_icon"), "_glx_square_icon_v2")
            if icon:
                m.square_icon = icon
            logo = _img(gamesdb.get("logo") or g.get("logo"), "", "png")  # transparentes Logo in voller Größe
            if logo:
                m.logo = logo
            m.genres = [_localized(x.get("name")) for x in g.get("genres") or []]
            m.themes = [_localized(x.get("name")) for x in g.get("themes") or []]
            m.developers = [x.get("name", "") for x in g.get("developers") or []]
            m.publishers = [x.get("name", "") for x in g.get("publishers") or []]
            if g.get("aggregated_rating") is not None:
                try:
                    m.rating = float(g["aggregated_rating"])
                except (TypeError, ValueError):
                    pass
            frd = gamesdb.get("first_release_date") or g.get("first_release_date")
            if frd and not m.release_date:
                m.release_date = str(frd)[:10]
            if gamesdb.get("dlcs_ids"):
                m.dlc_ids = [str(x) for x in gamesdb["dlcs_ids"]]
            oss = [o.get("slug", "") for o in gamesdb.get("supported_operating_systems") or []]
            if oss:
                m.os_list = oss
        return m

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GameMeta":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _strip_html(text: str) -> str:
    import html
    import re
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"</p>|</li>|</h\d>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
