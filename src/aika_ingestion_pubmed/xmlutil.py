"""Parsing XML sicuro (niente DTD esterni, niente rete, niente entità)."""

from __future__ import annotations

from lxml import etree

# Unico punto in cui si nomina il tipo privato degli stub di lxml.
type Element = etree._Element  # pyright: ignore[reportPrivateUsage]


def secure_parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        remove_comments=True,
        remove_pis=True,
        huge_tree=False,
    )


def parse_xml(data: bytes) -> Element:
    return etree.fromstring(data, parser=secure_parser())
