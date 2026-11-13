"""Acceptance: status follows observable gaps, without losing PII or Office content."""
import io
import json
import zipfile
from pathlib import Path

import pytest
import pymupdf as fitz
from docx import Document
from PIL import Image, ImageDraw
from lxml import etree as E
from veil.engine import package


def codes(report,severity):
    return {i['code'] for i in report['issues'] if i['severity']==severity}


def test_default_word_scaffolding_completes_and_still_redacts(engine,tmp_path):
    d=Document();p=d.add_paragraph('Name: ');p.add_run('Amelia ').bold=True;p.add_run('Quill')
    d.add_paragraph('Email: sample@example.test');source=tmp_path/'input.docx';d.save(source)
    before=source.read_bytes();r=engine.process(source,tmp_path/'out')
    assert r['status']=='completed',r
    assert r['warnings']==[] and not r['review_summary']['required']
    assert {'blank_image','office_xml_scaffolding'}<=codes(r,'info')
    assert not codes(r,'review')
    result=tmp_path/'out/sanitised.docx'
    text='\n'.join(p.text for p in Document(result).paragraphs)
    assert '[PERSON_001]' in text and '[EMAIL_ADDRESS_001]' in text
    assert 'Amelia' not in text and 'sample@example.test' not in text
    assert len(Document(result).paragraphs[0].runs)==3
    assert source.read_bytes()==before
    assert 'Amelia' not in json.dumps(r) and 'sample@example.test' not in json.dumps(r)


def test_nonblank_thumbnail_is_not_assumed_safe(engine,tmp_path,monkeypatch):
    d=Document();d.add_paragraph('SYNTHETIC TEST MATERIAL');p=tmp_path/'in.docx';d.save(p)
    parts=package(p);im=Image.new('RGB',(400,500),'white');ImageDraw.Draw(im).text((30,40),'Hidden PII sample@example.test',fill='black');b=io.BytesIO();im.save(b,format='JPEG');parts['docProps/thumbnail.jpeg']=b.getvalue()
    with zipfile.ZipFile(p,'w') as z:
        for name,data in parts.items():z.writestr(name,data)
    monkeypatch.setattr(engine,'ocr',lambda image:(None,None))
    r=engine.process(p,tmp_path/'out')
    assert r['status']=='review_required' and 'ocr_no_text' in codes(r,'review')
    finding=next(i for i in r['issues'] if i['code']=='ocr_no_text')
    assert 'preview thumbnail' in finding['location_label'] and finding['action']


def test_uniform_blank_scanned_page_does_not_need_review(engine,tmp_path):
    d=fitz.open();p=d.new_page();im=Image.new('RGB',(500,700),'white');b=io.BytesIO();im.save(b,format='PNG');p.insert_image(p.rect,stream=b.getvalue());d.save(tmp_path/'blank.pdf');d.close()
    r=engine.process(tmp_path/'blank.pdf',tmp_path/'out')
    assert r['status']=='completed' and 'blank_image' in codes(r,'info')
    assert r['ocr_observations'][0]['uniform_image']


@pytest.mark.parametrize('case',['no_text','low_confidence','small','rotated'])
def test_observable_ocr_gaps_require_review_at_the_image(engine,tmp_path,monkeypatch,case):
    d=fitz.open();p=d.new_page();im=Image.new('RGB',(500,700),'white');ImageDraw.Draw(im).text((30,40),'SYNTHETIC sample@example.test',fill='black');b=io.BytesIO();im.save(b,format='PNG');p.insert_image(p.rect,stream=b.getvalue());d.save(tmp_path/'scan.pdf');d.close()
    box=[[40,40],[300,40],[300,65],[40,65]]
    confidence=.97
    if case=='low_confidence':confidence=.65
    if case=='small':box=[[40,40],[300,40],[300,48],[40,48]]
    if case=='rotated':box=[[40,40],[300,100],[295,125],[35,65]]
    rows=[] if case=='no_text' else [[box,'sample@example.test',confidence]]
    monkeypatch.setattr(engine,'ocr',lambda image:(rows,None))
    r=engine.process(tmp_path/'scan.pdf',tmp_path/'out')
    assert r['status']=='review_required',r
    issues=[i for i in r['issues'] if i['severity']=='review']
    assert any(i['code'] in {'ocr_no_text','ocr_quality'} for i in issues)
    assert all(i['location']=='page:1:render' and i['action'] for i in issues)
    assert 'sample@example.test' not in json.dumps(r)


def test_confident_supported_ocr_is_information_not_blanket_review(engine,tmp_path,monkeypatch):
    # Controlled OCR isolates classification. Real OCR fixture coverage is tested separately.
    from veil.detection import Session
    im=Image.new('RGB',(500,150),'white');ImageDraw.Draw(im).text((40,40),'sample@example.test',fill='black')
    monkeypatch.setattr(engine,'ocr',lambda image:([[[[35,35],[400,35],[400,65],[35,65]],'sample@example.test',.98]],None))
    s=Session(engine.detector);clean=engine.image(im,s,'page:1:render')
    assert not s.warnings and s.notices
    assert s.counts['EMAIL_ADDRESS']==1 and clean.getpixel((100,50))==(0,0,0)
    assert s.issues[0]['code']=='ocr_processed' and s.issues[0]['severity']=='info'


def test_unknown_custom_xml_still_requires_specific_review(engine,tmp_path):
    d=Document();d.add_paragraph('SYNTHETIC TEST MATERIAL');p=tmp_path/'in.docx';d.save(p)
    parts=package(p);parts['customXml/item1.xml']=b'<unknown><data>Email: sample@example.test</data></unknown>'
    with zipfile.ZipFile(p,'w') as z:
        for name,data in parts.items():z.writestr(name,data)
    r=engine.process(p,tmp_path/'out')
    assert r['status']=='review_required' and 'custom_xml' in codes(r,'review')
    assert b'sample@example.test' not in package(tmp_path/'out/sanitised.docx')['customXml/item1.xml']
    assert next(i for i in r['issues'] if i['code']=='custom_xml')['location'].startswith('part:')


def test_straight_table_borders_are_not_outlined_text_warning(engine,tmp_path):
    d=fitz.open();p=d.new_page();p.insert_text((50,100),'Email: sample@example.test');p.draw_rect(fitz.Rect(40,70,400,120));p.draw_line((40,95),(400,95));d.save(tmp_path/'table.pdf');d.close()
    r=engine.process(tmp_path/'table.pdf',tmp_path/'out')
    assert r['status']=='completed' and 'pdf_rules' in codes(r,'info')
    assert r['entity_counts']['EMAIL_ADDRESS']==1


def test_complex_vector_graphics_are_ocr_inspected(engine,tmp_path,monkeypatch):
    d=fitz.open();p=d.new_page();p.insert_text((50,100),'SYNTHETIC TEST MATERIAL');p.draw_circle((200,200),30);d.save(tmp_path/'vector.pdf');d.close()
    calls=[]
    def no_ocr(image):calls.append(image.shape);return None,None
    monkeypatch.setattr(engine,'ocr',no_ocr)
    r=engine.process(tmp_path/'vector.pdf',tmp_path/'out')
    assert calls and r['status']=='review_required'
    assert any(i['location']=='page:1:vector-render' and i['code']=='ocr_no_text' for i in r['issues'])


def test_removed_annotation_icon_does_not_create_vector_review(engine,tmp_path,monkeypatch):
    d=fitz.open();p=d.new_page();p.insert_text((50,100),'Email: sample@example.test');p.add_text_annot((400,100),'Synthetic annotation');d.save(tmp_path/'annotated.pdf');d.close()
    before=(tmp_path/'annotated.pdf').read_bytes()
    def unexpected_ocr(image):pytest.fail('Removed annotation icon should not cause OCR')
    monkeypatch.setattr(engine,'ocr',unexpected_ocr)
    r=engine.process(tmp_path/'annotated.pdf',tmp_path/'out')
    assert r['status']=='completed' and r['entity_counts']['EMAIL_ADDRESS']==1
    assert (tmp_path/'annotated.pdf').read_bytes()==before
    with fitz.open(tmp_path/'out/sanitised.pdf') as result:assert not list(result[0].annots() or [])
