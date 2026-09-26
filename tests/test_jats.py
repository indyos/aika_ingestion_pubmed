from __future__ import annotations

import re
import unicodedata

import pytest

from aika_ingestion_pubmed.jats import JatsToMarkdown
from aika_ingestion_pubmed.models import AbstractSection
from aika_ingestion_pubmed.pmc import parse_article_set
from aika_ingestion_pubmed.xmlutil import parse_xml

from .support import read_fixture


def convert(name: str, pmcid: str, **kwargs: object) -> str:
    article = parse_article_set(read_fixture(f"jats/{name}.xml"))[pmcid]
    root = parse_xml(article.xml)
    title = JatsToMarkdown.extract_title(root) or "Titolo"
    return JatsToMarkdown().convert(root, title=title, **kwargs)  # pyright: ignore[reportArgumentType]


@pytest.fixture(scope="module")
def guideline() -> str:
    return convert("guideline_structured", "PMC1000001")


@pytest.fixture(scope="module")
def edge() -> str:
    return convert("review_edge_cases", "PMC1000002")


# ------------------------------------------------------------------ struttura


def test_single_h1_and_abstract_first(guideline: str) -> None:
    lines = guideline.splitlines()
    assert (
        lines[0]
        == "# Nutritional management of adults on maintenance hemodialysis: a practice guideline"
    )
    assert sum(1 for ln in lines if ln.startswith("# ")) == 1
    assert lines[2] == "## Abstract"


def test_structured_abstract_has_h3_subsections(guideline: str) -> None:
    abstract = guideline.split("## Abstract")[1].split("\n## ")[0]
    assert "### Background" in abstract
    assert "### Recommendations" in abstract


def test_heading_levels_have_no_jumps(guideline: str, edge: str) -> None:
    for md in (guideline, edge):
        previous = 1
        for line in md.splitlines():
            m = re.match(r"^(#+) ", line)
            if m:
                level = len(m.group(1))
                assert level <= previous + 1, f"salto di livello prima di: {line}"
                previous = level


def test_original_section_titles_kept_in_order(guideline: str) -> None:
    titles = [ln for ln in guideline.splitlines() if ln.startswith("## ")]
    assert titles == ["## Abstract", "## 1. Introduction", "## 2. Methods", "## 3. Results"]
    assert "### 2.1 Search strategy" in guideline
    assert "### 2.2 Statistical analysis" in guideline


def test_one_paragraph_per_line(guideline: str) -> None:
    intro = next(ln for ln in guideline.splitlines() if ln.startswith("Patients on"))
    assert "\n" not in intro
    assert "  " not in intro  # niente spazi doppi da indentazione XML


def test_excluded_material_absent(guideline: str) -> None:
    for text in (
        "Funding",
        "Example Foundation",
        "Conflict of Interest",
        "competing interests",
        "Supplementary",
        "Acknowledg",
        "We thank the nurses",
        "References",
        "Department of Nephrology",
        "Graphical abstract",
        "hemodialysis, nutrition",  # keyword group
    ):
        assert text not in guideline


def test_sections_excluded_by_sec_type_or_by_title_independently(edge: str) -> None:
    # solo `sec-type` (titolo neutro «Statement») e solo titolo («Financial support»)
    assert "owns shares" not in edge
    assert "## Statement" not in edge
    assert "Example Agency" not in edge
    assert "Financial support" not in edge
    assert "## Acknowledgements" not in edge and "Thanks to everyone" not in edge
    assert "## Summary" in edge  # le sezioni normali restano


def test_no_toc_or_graphical_abstract_in_fallback(edge: str) -> None:
    assert "table-of-contents blurb" not in edge
    assert "Graphical abstract text" not in edge


def test_untitled_leading_content_gets_fallback_heading(edge: str) -> None:
    assert "## Abstract\n\nPlain unstructured abstract" in edge
    assert "\n## Main text\n\nBody paragraph before any section title" in edge


def test_untitled_parent_section_child_is_h2(edge: str) -> None:
    assert "## Untitled parent, titled child" in edge


# ------------------------------------------------------------------ inline


def test_bold_italic_preserved_and_whitespace_moved_outside(guideline: str, edge: str) -> None:
    assert "**maintenance hemodialysis**" in guideline
    assert "*P* < 0.05" in guideline
    assert "with *spaced italics* and" in edge  # niente «* spaced italics *»


def test_numbers_units_and_symbols_exact(guideline: str) -> None:
    for exact in (
        "5.5 mmol/L",  # NBSP normalizzato a spazio
        "10<sup>9</sup>/L",
        "25 µmol/L",
        "H<sub>2</sub>O",
        "K<sup>+</sup> ≤ 5.5 mmol/L",
        "1.0–1.2 g/kg/day",
        "aged ≥18 years",
        ">3 months",
    ):
        assert exact in guideline


def test_bibliographic_citations_removed_cleanly(guideline: str, edge: str) -> None:
    assert "hemodialysis." in guideline  # «... hemodialysis <xref>1</xref>.»
    assert "per session." in guideline  # richiamo in <sup>
    assert "(, )" not in guideline and "()" not in guideline and "[]" not in guideline
    assert "5.5 mmol/L (" not in guideline  # «(2, 3)» eliminato per intero
    assert "and a citation plus ranges." in edge
    assert "" not in guideline + edge


def test_non_bibliographic_xrefs_keep_their_text(guideline: str, edge: str) -> None:
    assert "See Table 1 and the site" in guideline
    assert "(Figure 2)" in edge


def test_footnote_markers_removed_in_prose(edge: str) -> None:
    assert "footnote marker and" in edge


def test_external_link_and_escaping(guideline: str) -> None:
    assert "[renal guide](https://example.org/renal)" in guideline
    assert r"a \* asterisk and an\_underscore" in guideline


def test_paragraph_starts_are_not_read_as_markdown_blocks(edge: str) -> None:
    assert "\n1\\. Starts like an ordered list" in edge
    assert "\n\\- Starts like a bullet" in edge
    assert "\n\\# Starts like a heading" in edge


# ------------------------------------------------------------------ liste, formule


def test_nested_lists(guideline: str) -> None:
    assert (
        "- adults aged ≥18 years;\n"
        "- on dialysis for >3 months, including:\n"
        "  1. hemodialysis;\n"
        "  2. hemodiafiltration."
    ) in guideline


def test_definition_list(edge: str) -> None:
    assert "- **K**: potassium\n- **PD**: peritoneal dialysis" in edge


def test_display_formula_uses_tex_not_duplicated_mathml(guideline: str) -> None:
    assert "$$x = 1$$" in guideline
    assert "x=1" not in guideline.replace("x = 1", "")


# ------------------------------------------------------------------ tabelle e figure


def test_table_nested_in_paragraph_is_extracted_whole(guideline: str) -> None:
    block = guideline.split("::: table id=T1")[1].split("\n:::")[0]
    assert "**Table 1. Daily nutrient targets by dialysis modality**" in block
    assert "Values are per kg of ideal body weight." in block
    assert "Includes protein lost in dialysate." in block  # nota a piè di tabella
    # il testo del paragrafo prima e dopo la tabella resta, in paragrafi separati
    assert "Daily targets are summarised below.\n\n::: table" in guideline
    assert ":::\n\nThe table above is complete." in guideline


def test_table_merged_cells_expanded(guideline: str) -> None:
    table = [ln for ln in guideline.splitlines() if ln.startswith("|")]
    assert table[0] == "| Nutrient | Modality / HD | Modality / PD |"  # intestazione a 2 livelli
    assert table[1] == "| --- | --- | --- |"
    assert "| Protein (g/kg/day) | 1.0–1.2 | 1.0–1.2<sup>a</sup> |" in table
    # rowspan: «Energy» ripetuto; colspan: testo ripetuto nelle colonne coperte
    assert "| Energy | 25–35 kcal/kg | 25–35 kcal/kg |" in table
    assert "| Energy | Individualise to activity level | Individualise to activity level |" in table
    assert {len(row.strip("|").split(" | ")) for row in table if r"\|" not in row} == {3}


def test_pipe_in_cell_is_escaped(guideline: str) -> None:
    assert r"| Sodium \| salt |" in guideline


def test_table_without_thead_and_with_nested_table(edge: str) -> None:
    assert "| Food | K (mg/100 g) |\n| --- | --- |" in edge  # prima riga = intestazione
    assert "| Banana<sup>b</sup> | 358 |" in edge
    assert "| Nested inner cell | n/a |" in edge


def test_table_only_image_keeps_caption(edge: str) -> None:
    assert "::: table id=T3\n\n**Table 3. Table given only as an image**\n\n:::" in edge


def test_figure_is_caption_only(guideline: str) -> None:
    assert (
        "::: figure id=F1\n\n**Figure 1. Flow of studies through the review.**\n\n:::" in guideline
    )
    assert "jpg" not in guideline


def test_boxed_text_title_kept(guideline: str) -> None:
    assert "**Key points**\n\nMonitor serum potassium monthly." in guideline


# ------------------------------------------------------------------ abstract, casi limite


def test_pubmed_abstract_used_when_jats_has_none() -> None:
    article = parse_article_set(read_fixture("jats/no_body.xml"))["PMC1000003"]
    root = parse_xml(article.xml)
    # rimuovo l'abstract JATS: deve subentrare quello PubMed
    for ab in root.iter("abstract"):
        ab.getparent().remove(ab)  # pyright: ignore[reportOptionalMemberAccess]
    md = JatsToMarkdown().convert(
        root,
        title="T",
        fallback_abstract=[
            AbstractSection("BACKGROUND", "Testo *uno*."),
            AbstractSection(None, "Due."),
        ],
    )
    assert "## Abstract\n\n### BACKGROUND\n\nTesto *uno*.\n\nDue." in md


def _article_with_abstracts(*abstracts: str) -> str:
    xml = (
        "<article><front><article-meta>"
        + "".join(abstracts)
        + "</article-meta></front><body><sec><title>Methods</title><p>Testo.</p></sec></body></article>"
    )
    return JatsToMarkdown().convert(
        parse_xml(xml.encode()),
        title="T",
        fallback_abstract=[AbstractSection(None, "Abstract da PubMed.")],
    )


def test_graphical_abstract_never_used_as_the_abstract() -> None:
    graphical = (
        '<abstract abstract-type="graphical"><p>Testo del graphical abstract.</p></abstract>'
    )
    only_graphical = _article_with_abstracts(graphical)
    assert "graphical abstract" not in only_graphical
    assert "## Abstract\n\nAbstract da PubMed.\n\n## Methods" in only_graphical

    main = "<abstract><p>Abstract vero.</p></abstract>"
    graphical_first = _article_with_abstracts(graphical, main)
    assert "## Abstract\n\nAbstract vero.\n\n## Methods" in graphical_first
    assert "graphical abstract" not in graphical_first
    assert "PubMed" not in graphical_first


def test_missing_abstract_still_emits_heading() -> None:
    article = parse_article_set(read_fixture("jats/no_body.xml"))["PMC1000003"]
    root = parse_xml(article.xml)
    for ab in root.iter("abstract"):
        ab.getparent().remove(ab)  # pyright: ignore[reportOptionalMemberAccess]
    md = JatsToMarkdown().convert(root, title="T")
    assert md == "# T\n\n## Abstract\n"


def test_output_is_nfc_and_ends_with_single_newline(edge: str) -> None:
    assert unicodedata.is_normalized("NFC", edge)
    assert "Café" in edge and "é should be NFC" in edge
    assert edge.endswith("\n") and not edge.endswith("\n\n")
    assert "\r" not in edge


def test_no_language_translation_or_html_leftovers(guideline: str) -> None:
    assert not re.search(r"</?(?!sup|sub)[a-z]+[ >]", guideline)


# ------------------------------------------------------------------ determinismo


@pytest.mark.parametrize(
    ("name", "pmcid"),
    [("guideline_structured", "PMC1000001"), ("review_edge_cases", "PMC1000002")],
)
def test_conversion_is_byte_identical(name: str, pmcid: str) -> None:
    first = convert(name, pmcid).encode("utf-8")
    second = convert(name, pmcid).encode("utf-8")
    assert first == second


def test_determinism_across_reserialization_of_the_xml() -> None:
    """Stesso XML → stesso file, anche dopo un giro parse → serialize (come fa il fetcher)."""
    raw = parse_article_set(read_fixture("jats/guideline_structured.xml"))["PMC1000001"]
    again = parse_article_set(
        b"<pmc-articleset>" + raw.xml.split(b"?>", 1)[1] + b"</pmc-articleset>"
    )
    a = JatsToMarkdown().convert(parse_xml(raw.xml), title="T")
    b = JatsToMarkdown().convert(parse_xml(again["PMC1000001"].xml), title="T")
    assert a.encode() == b.encode()
