"""``PubMedMetadataParser``: PubMed XML → :class:`PubMedRecord`."""

from __future__ import annotations

import re
from typing import Final

from .inline import finalize, plain_text, render_inline
from .models import AbstractSection, PubMedRecord
from .xmlutil import Element, parse_xml

RETRACTED_PUBLICATION_TYPES: Final = frozenset(
    {"retracted publication", "retraction of publication"}
)

_MONTHS: Final = {
    m: f"{i:02d}"
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
_YEAR: Final = re.compile(r"\b(1[5-9]\d\d|20\d\d)\b")


class PubMedMetadataParser:
    def parse(self, xml: bytes) -> list[PubMedRecord]:
        root = parse_xml(xml)
        return [self._record(a) for a in root.iter("PubmedArticle")]

    def _record(self, art: Element) -> PubMedRecord:
        pmid = _text(art, "MedlineCitation/PMID")
        if not pmid:
            raise ValueError("PubmedArticle senza PMID")
        article = art.find("MedlineCitation/Article")
        title_el = article.find("ArticleTitle") if article is not None else None
        title = plain_text(title_el) if title_el is not None else ""
        publication_types = [
            plain_text(e)
            for e in art.iterfind("MedlineCitation/Article/PublicationTypeList/PublicationType")
        ]
        retraction_in = any(
            c.get("RefType") == "RetractionIn"
            for c in art.iterfind("MedlineCitation/CommentsCorrectionsList/CommentsCorrections")
        )
        return PubMedRecord(
            pmid=pmid,
            title=title,
            authors=_authors(art),
            journal=_text(art, "MedlineCitation/Article/Journal/Title"),
            published=_pub_date(art),
            languages=[plain_text(e) for e in art.iterfind("MedlineCitation/Article/Language")],
            mesh_terms=[
                plain_text(e)
                for e in art.iterfind("MedlineCitation/MeshHeadingList/MeshHeading/DescriptorName")
            ],
            publication_types=publication_types,
            doi=_article_id(art, "doi"),
            pmcid=_article_id(art, "pmc"),
            abstract=_abstract(art),
            retracted=retraction_in
            or any(pt.casefold() in RETRACTED_PUBLICATION_TYPES for pt in publication_types),
        )


def _text(el: Element, path: str) -> str | None:
    found = el.find(path)
    if found is None:
        return None
    value = plain_text(found)
    return value or None


def _article_id(art: Element, id_type: str) -> str | None:
    for e in art.iterfind("PubmedData/ArticleIdList/ArticleId"):
        if e.get("IdType") == id_type:
            value = plain_text(e)
            if value:
                return value
    return None


def _authors(art: Element) -> list[str]:
    names: list[str] = []
    for a in art.iterfind("MedlineCitation/Article/AuthorList/Author"):
        collective = _text(a, "CollectiveName")
        if collective:
            names.append(collective)
            continue
        last = _text(a, "LastName")
        fore = _text(a, "ForeName") or _text(a, "Initials")
        full = " ".join(p for p in (fore, last) if p)
        if full:
            names.append(full)
    return names


def _pub_date(art: Element) -> str | None:
    """Data di pubblicazione anche parziale: ``YYYY``, ``YYYY-MM`` o ``YYYY-MM-DD``."""
    pd = art.find("MedlineCitation/Article/Journal/JournalIssue/PubDate")
    if pd is None:
        return None
    year = _text(pd, "Year")
    month = _month(_text(pd, "Month"))
    day = _text(pd, "Day")
    if year is None:
        medline = _text(pd, "MedlineDate")
        m = _YEAR.search(medline or "")
        if m is None:
            return None
        year = m.group(1)
        month = _month(next((w for w in re.split(r"[\s/-]+", medline or "") if _month(w)), None))
        day = None
    if not re.fullmatch(r"\d{4}", year):
        return None
    if month is None:
        return year
    if day is not None and re.fullmatch(r"\d{1,2}", day):
        return f"{year}-{month}-{int(day):02d}"
    return f"{year}-{month}"


def _month(value: str | None) -> str | None:
    if not value:
        return None
    if value.isdigit() and 1 <= int(value) <= 12:
        return f"{int(value):02d}"
    return _MONTHS.get(value[:3].casefold())


def _abstract(art: Element) -> list[AbstractSection]:
    sections: list[AbstractSection] = []
    for at in art.iterfind("MedlineCitation/Article/Abstract/AbstractText"):
        text = finalize(render_inline(at))
        if not text:
            continue
        label = at.get("Label")
        sections.append(AbstractSection(label=label.strip() if label else None, text=text))
    return sections
