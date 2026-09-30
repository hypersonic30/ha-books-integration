"""Tolino position <-> EPUB CFI, computed from the document structure."""
import pytest

from custom_components.books.positions import Epub, cfi_to_point, point_to_cfi

from .test_tolino_sync import DOC_ALIGNED, DOC_COMPACT, make_epub


@pytest.fixture(scope="module")
def epub():
    return Epub(make_epub())


def test_spine_is_read_in_order_with_decoded_hrefs(epub):
    assert epub.spine == ["OEBPS/a.xhtml", "OEBPS/b.xhtml", "OEBPS/c d.xhtml"]
    assert Epub(make_epub(opf_dir="OPS/text")).spine[0] == "OPS/text/a.xhtml"
    assert epub.spine_index("OEBPS/c%20d.xhtml") == 2 and epub.spine_index("./OEBPS/b.xhtml") == 1 and epub.spine_index("x.xhtml") is None


# Pretty-printed source: whitespace between every pair of tags, so node numbers and CFI numbers coincide.
@pytest.mark.parametrize("point,cfi", [
    ("/1/4/2/1:5", "/4/2/1:0"),          # text of <h1>
    ("/1/4/2", "/4/2"),                  # the <h1> element itself: no offset
    ("/1/4/4/1:3", "/4/4/1:0"),          # "Hello "
    ("/1/4/4/2/1:1", "/4/4/2/1:0"),      # inside <b>
    ("/1/4/4/3:2", "/4/4/3:0"),          # text after <b>
])
def test_aligned_documents(epub, point, cfi):
    assert point_to_cfi(epub, f"OEBPS/b.xhtml#point({point})") == f"epubcfi(/6/4!{cfi})"


# Compact source (like the Harry Potter files): the numbers differ - this is the case a naive mapping gets wrong.
@pytest.mark.parametrize("point,cfi", [
    ("/1/3/3/1:0", "/4/4/1:0"),          # html -> (head, " ", body=3) -> (p, " ", h1=3) -> text
    ("/1/3/5", "/4/6"),                  # second <p>
    ("/1/3/5/1:0", "/4/6/1:0"),          # "c"
    ("/1/3/5/2", "/4/6/2"),              # <i>
    ("/1/3/5/3:0", "/4/6/3:0"),          # "d" after <i>
    ("/1/3/1/1:0", "/4/2/1:0"),          # first <p>'s text
])
def test_compact_documents(epub, point, cfi):
    assert point_to_cfi(epub, f"OEBPS/c d.xhtml#point({point})") == f"epubcfi(/6/6!{cfi})"


@pytest.mark.parametrize("position", [
    None, "", "OEBPS/a.xhtml", "OEBPS/zzz.xhtml#point(/1/4/2:0)",        # no point / unknown document
    "OEBPS/a.xhtml#point(/2/4/2:0)", "OEBPS/a.xhtml#point(/4/2:0)",       # must start at the root element
    "OEBPS/a.xhtml#point(/1/99/2:0)",                                      # step out of range
    "OEBPS/a.xhtml#point(/1/4/2/1/1:0)",                                   # stepping into a text node
    "OEBPS/a.xhtml#point(/1/x/2:0)", "OEBPS/a.xhtml#point()", "OEBPS/a.xhtml#point(/1)",
])
def test_unresolvable_positions_give_none(epub, position):
    assert point_to_cfi(epub, position) is None


def test_broken_document_gives_none():
    broken = Epub(make_epub(docs={"a.xhtml": "<html><body><p>unclosed</body>"}))
    assert point_to_cfi(broken, "OEBPS/a.xhtml#point(/1/2/1:0)") is None


def test_comments_count_as_nodes():
    doc = '<html xmlns="http://www.w3.org/1999/xhtml"><head/><body><!-- c --><p>x</p></body></html>'
    e = Epub(make_epub(docs={"a.xhtml": doc}))
    # body children: comment(1), p(2) -> the <p> is the first ELEMENT (comments are nodes for Tolino, not CFI elements
    # in epub.js - but they do occupy a node number). /1/2/2 = html -> body -> p
    assert point_to_cfi(e, "OEBPS/a.xhtml#point(/1/2/2/1:0)") is not None


@pytest.mark.parametrize("doc_index,point", [
    (1, "OEBPS/b.xhtml#point(/1/4/4/2/1:0)"), (2, "OEBPS/c d.xhtml#point(/1/3/3/1:0)"), (2, "OEBPS/c d.xhtml#point(/1/3/5/3:0)"),
    (1, "OEBPS/b.xhtml#point(/1/4/2:0)"),   # Tolino always writes an offset, also after an element
])
def test_roundtrip(epub, doc_index, point):
    assert cfi_to_point(epub, point_to_cfi(epub, point)) == point


@pytest.mark.parametrize("cfi,point", [
    ("epubcfi(/6/6!/4/4/1:57)", "OEBPS/c d.xhtml#point(/1/3/3/1:57)"),        # offset is kept in this direction
    ("epubcfi(/6/4!/4/2[chap1]/1:0)", "OEBPS/b.xhtml#point(/1/4/2/1:0)"),     # CFI id assertions are ignored
    ("epubcfi(/6/2[ch]!/4/4)", "OEBPS/a.xhtml#point(/1/4/4:0)"),
])
def test_cfi_to_point(epub, cfi, point):
    assert cfi_to_point(epub, cfi) == point


@pytest.mark.parametrize("cfi", [None, "", "epubcfi()", "epubcfi(/6/3!/4/2)", "epubcfi(/6/99!/4/2)", "epubcfi(/6/4!/4/98/1:0)",
                                 "epubcfi(/6/4!/4/2/1/1:0)", "epubcfi(/6/4/4/2)", "garbage"])
def test_bad_cfis_give_none(epub, cfi):
    assert cfi_to_point(epub, cfi) is None
