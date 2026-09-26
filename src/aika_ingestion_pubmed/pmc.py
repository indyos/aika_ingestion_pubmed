"""``PmcFullTextFetcher``: full text JATS dall'Open Access Subset di PMC via ``efetch db=pmc``."""

from __future__ import annotations

import re
from collections.abc import Sequence

from lxml import etree

from .eutils import EutilsClient
from .inline import local_name, text_of
from .models import PmcArticle
from .xmlutil import Element, parse_xml

_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_PMC_ID_TYPES = ("pmcid", "pmc", "pmc-uid")


class PmcFullTextFetcher:
    def __init__(self, client: EutilsClient, batch_size: int = 100) -> None:
        self._client = client
        self._batch_size = batch_size

    def fetch(self, pmcids: Sequence[str]) -> dict[str, PmcArticle]:
        """Restituisce ``{PMCID: PmcArticle}`` per gli articoli presenti nella risposta.

        Gli articoli senza ``<body>`` non vuoto hanno ``has_body=False``: il chiamante li scarta.
        """
        found: dict[str, PmcArticle] = {}
        for start in range(0, len(pmcids), self._batch_size):
            chunk = pmcids[start : start + self._batch_size]
            response = self._client.post(
                "efetch.fcgi",
                {
                    "db": "pmc",
                    "id": ",".join(_numeric(p) for p in chunk),
                    "retmode": "xml",
                },
            )
            found.update(parse_article_set(response.content))
        return found


def parse_article_set(xml: bytes) -> dict[str, PmcArticle]:
    root = parse_xml(xml)
    articles = [root] if local_name(root) == "article" else list(root.iter("article"))
    result: dict[str, PmcArticle] = {}
    for art in articles:
        pmcid = _pmcid(art)
        if pmcid is None:
            continue
        result[pmcid] = PmcArticle(
            pmcid=pmcid,
            xml=etree.tostring(art, encoding="utf-8", xml_declaration=True),
            license_raw=extract_license(art),
            has_body=has_body(art),
        )
    return result


def has_body(article: Element) -> bool:
    """Full text disponibile solo se esiste un ``<body>`` con contenuto testuale."""
    body = article.find("body")
    return body is not None and bool(text_of(body).strip())


def extract_license(article: Element) -> str | None:
    """Licenza grezza: ``license_ref`` (ALI) o ``xlink:href`` se presenti, altrimenti ``license-type``."""
    for lic in article.iterfind("front/article-meta/permissions/license"):
        for child in lic.iter():
            if local_name(child) == "license_ref":
                text = text_of(child).strip()
                if text:
                    return text
        href = lic.get(_XLINK_HREF)
        if href:
            return href.strip()
        for child in lic.iter():
            if local_name(child) == "ext-link" and child.get(_XLINK_HREF):
                return str(child.get(_XLINK_HREF)).strip()
        lic_type = lic.get("license-type")
        if lic_type:
            return lic_type.strip()
    return None


def _pmcid(article: Element) -> str | None:
    for kind in _PMC_ID_TYPES:
        for e in article.iterfind("front/article-meta/article-id"):
            if e.get("pub-id-type") == kind:
                digits = re.sub(r"\D", "", text_of(e))
                if digits:
                    return f"PMC{digits}"
    return None


def _numeric(pmcid: str) -> str:
    return re.sub(r"\D", "", pmcid)
