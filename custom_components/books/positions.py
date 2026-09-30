"""Tolino reading positions <-> EPUB CFI, computed from the document's real structure.

Tolino writes positions like 'OEBPS/ch17.html#point(/1/3/3/1:0)': a child-sequence over ALL child nodes - elements AND
text nodes, whitespace-only ones included, 1-based; the first step is the root element. An EPUB CFI instead numbers
elements by 2n and the text between them by 2n+1. The two coincide only when the source is pretty-printed so that every
element is followed by whitespace, which is why a naive "same numbers" mapping works for some books and is wrong for
others. Verified on real data: Tolino's step sequence resolves to the very text node in which Tolino recorded a
highlight, and to the chapter heading for a bookmark written at the start of a chapter.

Tolino's character offsets count extra spaces around punctuation, so offsets are not carried over (paragraph precision).
"""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
from urllib.parse import unquote
from xml.etree import ElementTree as ET

_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container", "o": "http://www.idpf.org/2007/opf"}


class Epub:
    """The bits of an EPUB needed for position mapping: spine order and the spine documents."""

    def __init__(self, data: bytes) -> None:
        self._zip = zipfile.ZipFile(io.BytesIO(data))
        opf_path = ET.fromstring(self._zip.read("META-INF/container.xml")).find(".//c:rootfile", _NS).get("full-path")
        base = posixpath.dirname(opf_path)
        root = ET.fromstring(self._zip.read(opf_path))
        manifest = {i.get("id"): unquote(i.get("href", "")) for i in root.iterfind(".//o:manifest/o:item", _NS)}
        self.spine = [posixpath.normpath(posixpath.join(base, manifest[r.get("idref")]))
                      for r in root.iterfind(".//o:spine/o:itemref", _NS) if r.get("idref") in manifest]

    def spine_index(self, href: str) -> int | None:
        href = posixpath.normpath(unquote(href))
        return self.spine.index(href) if href in self.spine else None

    def document(self, index: int) -> ET.Element:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        return ET.fromstring(self._zip.read(self.spine[index]), parser=parser)


def _nodes(element: ET.Element) -> list[tuple[str, object]]:
    """Child nodes in document order: ('el', element) incl. comments, ('text', str) for non-empty character data."""
    out: list[tuple[str, object]] = []
    if element.text:
        out.append(("text", element.text))
    for child in element:
        out.append(("el", child))
        if child.tail:
            out.append(("text", child.tail))
    return out


def point_to_cfi(epub: Epub, position: str | None) -> str | None:
    """'OEBPS/a.html#point(/1/3/3/1:138)' -> 'epubcfi(/6/{2n}!/4/4/1:0)'; None if it cannot be resolved."""
    if not position or "#point(" not in position:
        return None
    href, _, rest = position.partition("#point(")
    path = rest.rstrip(")").split(":", 1)[0]
    if not re.fullmatch(r"(/\d+)+", path):
        return None
    steps = [int(s) for s in path.strip("/").split("/")]
    index = epub.spine_index(href)
    if index is None or steps[0] != 1:
        return None
    try:
        node = epub.document(index)
    except (ET.ParseError, KeyError):
        return None
    cfi: list[str] = []
    last_is_text = False
    for step in steps[1:]:
        if node is None:                                   # a text node has no children
            return None
        kids = _nodes(node)
        if not 1 <= step <= len(kids):
            return None
        kind, value = kids[step - 1]
        elements_before = sum(1 for k in kids[: step - 1] if k[0] == "el")
        if kind == "el":
            cfi.append(str(2 * (elements_before + 1)))
            node, last_is_text = value, False
        else:
            cfi.append(str(2 * elements_before + 1))
            node, last_is_text = None, True
    if not cfi:
        return None
    return f"epubcfi(/6/{2 * (index + 1)}!/{'/'.join(cfi)}{':0' if last_is_text else ''})"


def cfi_to_point(epub: Epub, cfi: str | None) -> str | None:
    """'epubcfi(/6/14!/4/2/1:57)' -> 'OEBPS/ch.html#point(/1/3/3/1:57)'; None if it cannot be resolved."""
    if not cfi:
        return None
    m = re.fullmatch(r"epubcfi\(/6/(\d+)(?:\[[^\]]*\])?!((?:/\d+(?:\[[^\]]*\])?)+)(?::(\d+))?(?:\[[^\]]*\])?\)", cfi.strip())
    if not m or int(m.group(1)) % 2:
        return None
    index = int(m.group(1)) // 2 - 1
    if not 0 <= index < len(epub.spine):
        return None
    steps = [int(s) for s in re.findall(r"/(\d+)", re.sub(r"\[[^\]]*\]", "", m.group(2)))]
    try:
        node = epub.document(index)
    except (ET.ParseError, KeyError):
        return None
    point = ["1"]
    for n, step in enumerate(steps):
        if node is None:
            return None
        kids = _nodes(node)
        if step % 2 == 0:                                  # element number step // 2
            seen = 0
            for position, (kind, value) in enumerate(kids, 1):
                if kind == "el":
                    seen += 1
                    if seen == step // 2:
                        point.append(str(position)); node = value
                        break
            else:
                return None
        else:                                              # the text after (step - 1) // 2 elements
            want, seen, found = (step - 1) // 2, 0, None
            for position, (kind, _) in enumerate(kids, 1):
                if kind == "el":
                    seen += 1
                elif seen == want:
                    found = position
                    break
            if found is None or n != len(steps) - 1:
                return None
            point.append(str(found)); node = None
    offset = m.group(3) if m.group(3) is not None else "0"
    return f"{epub.spine[index]}#point(/{'/'.join(point)}:{offset})"
