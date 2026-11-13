"""Removing printer metadata must preserve the slides and leave no dangling links."""
import io
import json
import zipfile

from lxml import etree as E
from pptx import Presentation
from pptx.util import Inches
import pytest

from veil.detection import Session
from veil.engine import package, parse
from veil.office_printers import remove_pptx_printer_settings
from veil.office_preservation import PreservationError


def make_pptx(path):
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(2))
    box.text_frame.text = 'Email: synthetic@example.test'
    box.text_frame.paragraphs[0].runs[0].font.bold = True
    slide.notes_slide.notes_text_frame.text = 'A benign note that must survive.'
    deck.save(path)
    return path


def test_printer_cleanup_preserves_every_slide_layout_and_note_byte(tmp_path, engine):
    path = make_pptx(tmp_path / 'source.pptx')
    parts = package(path)
    before = dict(parts)
    printer = next(n for n in parts if '/printerSettings/' in n)
    canary = 'PRIVATE-PRINTER-OWNER'.encode('utf-16-le')
    parts[printer] = canary
    xml = {n: parse(v) for n, v in parts.items() if n.endswith(('.xml', '.rels'))}
    session = Session(engine.detector)
    remove_pptx_printer_settings(parts, xml, session, {n: 'part:0001' for n in parts})
    assert printer not in parts
    assert not session.warnings and session.removals['saved_printer_settings'] == 1
    assert not session.uninspected
    for name, data in before.items():
        if name.startswith(('ppt/slides/', 'ppt/slideLayouts/', 'ppt/slideMasters/', 'ppt/notesSlides/', 'ppt/theme/')):
            assert parts[name] == data
    for root in xml.values():
        assert not any('printerSettings' in value for node in root.iter()
                       if isinstance(node.tag, str) for value in node.attrib.values())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in parts.items():
            archive.writestr(name, E.tostring(xml[name]) if name in xml else data)
    reopened = Presentation(io.BytesIO(buffer.getvalue()))
    assert len(reopened.slides) == 1
    assert reopened.slides[0].shapes[0].text == 'Email: synthetic@example.test'
    assert reopened.slides[0].shapes[0].text_frame.paragraphs[0].runs[0].font.bold
    assert 'benign note' in reopened.slides[0].notes_slide.notes_text_frame.text
    assert canary not in buffer.getvalue()


def test_pptx_completes_without_printer_review_and_still_redacts(tmp_path, engine):
    path = make_pptx(tmp_path / 'source.pptx')
    original = path.read_bytes()
    report = engine.process(path, tmp_path / 'output')
    assert report['status'] == 'completed', report
    assert not report['review_summary']['required']
    assert any(i['code'] == 'printer_settings_removed' and i['severity'] == 'info' for i in report['issues'])
    assert not report['uninspected_content']
    result = package(tmp_path / 'output/sanitised.pptx')
    assert not any('/printerSettings/' in name for name in result)
    assert b'synthetic@example.test' not in b' '.join(result.values())
    assert path.read_bytes() == original
    assert 'synthetic@example.test' not in json.dumps(report)


def test_printer_cleanup_does_not_remove_unrelated_document_links(tmp_path, engine):
    parts = package(make_pptx(tmp_path / 'source.pptx'))
    xml = {n: parse(v) for n, v in parts.items() if n.endswith(('.xml', '.rels'))}
    rels = xml['ppt/_rels/presentation.xml.rels']
    before = [E.tostring(n) for n in rels if not n.get('Type', '').endswith('/printerSettings')]
    remove_pptx_printer_settings(parts, xml, Session(engine.detector), {n: 'part:0001' for n in parts})
    assert [E.tostring(n) for n in rels] == before


def test_unexpected_use_of_printer_binary_still_blocks(tmp_path, engine):
    parts = package(make_pptx(tmp_path / 'source.pptx'))
    xml = {n: parse(v) for n, v in parts.items() if n.endswith(('.xml', '.rels'))}
    rel = next(n for n in xml['ppt/_rels/presentation.xml.rels'] if n.get('Type', '').endswith('/printerSettings'))
    rel.set('Type', 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/image')
    with pytest.raises(PreservationError, match='Unexpected relationship'):
        remove_pptx_printer_settings(parts, xml, Session(engine.detector), {n: 'part:0001' for n in parts})
