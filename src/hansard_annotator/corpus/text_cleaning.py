"""Conservative extraction of raw and clean speech text."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from hansard_annotator.corpus.hashing import canonical_json, normalise_unicode

BLOCK_TAGS = {"p", "li", "dt", "dd", "tr"}
CONTAINER_TAGS = {"ul", "ol", "dl", "table", "tbody", "thead", "tfoot"}
WORD_RE = re.compile(r"[^\W_]+(?:[\u2019'][^\W_]+)*", flags=re.UNICODE)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass
class ExtractedImage:
    index: int
    attributes: dict[str, str]
    alt_text: str | None
    replacement: str


@dataclass
class ExtractedText:
    text_raw: str
    text_clean: str
    block_structure_json: str
    calculated_word_count: int
    images: list[ExtractedImage] = field(default_factory=list)
    warning_codes: list[str] = field(default_factory=list)


def clean_whitespace(value: str) -> str:
    value = normalise_unicode(value).replace("\r\n", "\n").replace("\r", "\n")
    return " ".join(value.split())


def raw_whitespace(value: str) -> str:
    value = normalise_unicode(value).replace("\r\n", "\n").replace("\r", "\n")
    return value.strip()


def count_words(value: str) -> int:
    return len(WORD_RE.findall(value))


def _render_inline(
    element: etree._Element,
    *,
    include_image_replacements: bool,
    images: list[ExtractedImage],
) -> str:
    pieces: list[str] = []
    if element.text:
        pieces.append(element.text)
    for child in element:
        tag = local_name(child.tag)
        if tag == "img":
            attributes = dict(sorted((str(key), str(value)) for key, value in child.attrib.items()))
            alt_raw = attributes.get("alt")
            alt_text = clean_whitespace(alt_raw) if alt_raw else None
            replacement = alt_text if alt_text else "[IMAGE]"
            images.append(
                ExtractedImage(
                    index=len(images) + 1,
                    attributes=attributes,
                    alt_text=alt_text,
                    replacement=replacement,
                )
            )
            if include_image_replacements:
                pieces.append(replacement)
        else:
            pieces.append(
                _render_inline(
                    child,
                    include_image_replacements=include_image_replacements,
                    images=images,
                )
            )
        if child.tail:
            pieces.append(child.tail)
    return "".join(pieces)


def _block_elements(speech: etree._Element) -> list[etree._Element]:
    blocks: list[etree._Element] = []

    def visit(element: etree._Element) -> None:
        tag = local_name(element.tag)
        if tag in BLOCK_TAGS:
            blocks.append(element)
            return
        if tag in CONTAINER_TAGS or element is speech:
            for child in element:
                visit(child)
            return
        blocks.append(element)

    for child in speech:
        visit(child)
    return blocks


def extract_speech_text(speech: etree._Element) -> ExtractedText:
    images: list[ExtractedImage] = []
    blocks_output: list[dict[str, Any]] = []
    raw_blocks: list[str] = []
    clean_blocks: list[str] = []

    blocks = _block_elements(speech)
    if not blocks and (speech.text or "").strip():
        blocks = [speech]

    for block in blocks:
        tag = local_name(block.tag)
        if tag == "tr":
            cells = [child for child in block if local_name(child.tag) in {"td", "th"}]
            raw_cell_texts: list[str] = []
            clean_cell_texts: list[str] = []
            block_images_start = len(images)
            for cell in cells:
                raw_cell_texts.append(
                    raw_whitespace(
                        _render_inline(
                            cell,
                            include_image_replacements=False,
                            images=images,
                        )
                    )
                )
                cell_clean_images: list[ExtractedImage] = []
                clean_cell_texts.append(
                    clean_whitespace(
                        _render_inline(
                            cell,
                            include_image_replacements=True,
                            images=cell_clean_images,
                        )
                    )
                )
            raw_text = "\t".join(raw_cell_texts)
            clean_text = "\t".join(clean_cell_texts)
            block_images = images[block_images_start:]
        else:
            block_images_start = len(images)
            raw_text = raw_whitespace(
                _render_inline(
                    block,
                    include_image_replacements=False,
                    images=images,
                )
            )
            # Rendering a second time would duplicate image metadata, so use a temporary list.
            clean_images: list[ExtractedImage] = []
            clean_text = clean_whitespace(
                _render_inline(
                    block,
                    include_image_replacements=True,
                    images=clean_images,
                )
            )
            # Retain only one metadata record per source image.
            block_images = images[block_images_start:]
            if clean_images and not block_images:
                images.extend(clean_images)
                block_images = clean_images

        if raw_text or clean_text or block_images:
            raw_blocks.append(raw_text)
            clean_blocks.append(clean_text)
            blocks_output.append(
                {
                    "block_type": tag,
                    "text_raw": raw_text,
                    "text_clean": clean_text,
                    "images": [
                        {
                            "attributes": image.attributes,
                            "alt_text": image.alt_text,
                            "replacement": image.replacement,
                        }
                        for image in block_images
                    ],
                }
            )

    text_raw = "\n\n".join(raw_blocks)
    text_clean = "\n\n".join(clean_blocks)
    warnings = ["IMAGE_CONTENT_NOT_FETCHED"] if images else []
    if not text_clean:
        warnings.append("EMPTY_SPEECH_TEXT")
    return ExtractedText(
        text_raw=text_raw,
        text_clean=text_clean,
        block_structure_json=canonical_json(blocks_output),
        calculated_word_count=count_words(text_clean),
        images=images,
        warning_codes=warnings,
    )
