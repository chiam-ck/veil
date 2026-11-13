import json
import socket
import zipfile
from pathlib import Path
import pytest
import pymupdf
from lxml import etree as E
from veil.engine import package
from veil import config

@pytest.mark.parametrize('fmt',['docx','pptx','xlsx','pdf'])
def test_supported_outputs_valid_no_known_pii(engine,dataset,tmp_path,fmt):
    source=dataset/'development'/('synthetic-00.'+fmt);before=source.read_bytes()
    r=engine.process(source,tmp_path/'output')
    assert r['status']=='completed',r
    assert source.read_bytes()==before
    clean=tmp_path/'output'/('sanitised.'+fmt)
    if fmt!='pdf':
        parts=package(clean);data=b' '.join(parts.values())
        for value in [b'Amelia Quill',b'person00@example.test',b'S1000000Z',b'VEIL-0000-TEST',b'synthetic-author@example.test',b'notes@example.test',b'hidden@example.test',b'orphan@example.test',b'deleted@example.test',b'formula@example.test',b'comment@example.test']:
            assert value not in data
        if fmt=='docx':
            from docx import Document
            d=Document(clean);assert '[PERSON_001]' in '\n'.join(p.text for p in d.paragraphs)
            assert 'word/comments.xml' in parts
            assert b'commentReference' in parts['word/document.xml']
        if fmt=='pptx':
            from pptx import Presentation
            p=Presentation(clean);assert len(p.slides)==1
            assert '[EMAIL_ADDRESS_' in p.slides[0].notes_slide.notes_text_frame.text
        if fmt=='xlsx':
            from openpyxl import load_workbook
            w=load_workbook(clean);assert w['Hidden'].sheet_state=='hidden'
            assert w['Synthetic']['B2'].value.startswith('="[EMAIL_ADDRESS_')
            assert w['Synthetic']['B3'].value=='=A2' and w['Synthetic']['B4'].value=='=SUM(1,2)'
            assert w['Synthetic']['B5'].comment is not None
            assert 'comment@example.test' not in w['Synthetic']['B5'].comment.text
    else:
        with pymupdf.open(clean) as d:
            assert len(d)==1 and not d[0].get_text().strip()
            assert not list(d[0].annots() or []) and not d.embfile_count()
    report=json.dumps(r)
    for value in ['Amelia Quill','person00@example.test','S1000000Z','VEIL-0000-TEST']:
        assert value not in report
    assert 'mapping' not in report

@pytest.mark.parametrize('fmt',['docx','pptx','xlsx','pdf'])
def test_clear_image_ocr_does_not_blanket_require_review(engine,dataset,tmp_path,fmt):
    r=engine.process(dataset/'development'/('synthetic-02.'+fmt),tmp_path/'output')
    assert r['status']=='completed'
    assert all(o['low_confidence_lines']==0 for o in r['ocr_observations'])
    assert r['ocr_observations']
    assert r['entity_counts'].get('EMAIL_ADDRESS',0)>=1

@pytest.mark.parametrize('kind',['traversal','bomb','embedded','unknown'])
def test_unsafe_packages_block_without_artifacts(engine,tmp_path,kind):
    p=tmp_path/'bad.docx'
    with zipfile.ZipFile(p,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('word/document.xml','<root/>')
        if kind=='traversal': z.writestr('../escape','x')
        if kind=='bomb':z.writestr('large.xml','x'*2_000_000)
        if kind=='embedded':z.writestr('word/embeddings/object.bin','secret')
        if kind=='unknown':z.writestr('word/media/image.emf','secret')
    r=engine.process(p,tmp_path/'out')
    assert r['status']=='blocked'
    assert [x.name for x in (tmp_path/'out').iterdir()]==['report.json']

@pytest.mark.parametrize("password",["secret", ""])
def test_encrypted_pdf_blocked(engine,tmp_path,password):
    d=pymupdf.open();d.new_page();p=tmp_path/'secret.pdf'
    d.save(p,encryption=pymupdf.PDF_ENCRYPT_AES_256,owner_pw='owner',user_pw=password);d.close()
    r=engine.process(p,tmp_path/'out');assert r['status']=='blocked'

def test_protected_xlsx_blocked(engine,tmp_path):
    from openpyxl import Workbook
    w=Workbook();w.active.protection.set_password('test');p=tmp_path/'a.xlsx';w.save(p)
    assert engine.process(p,tmp_path/'out')['status']=='blocked'

def test_preservation_uses_mocked_llm_without_other_network(engine,dataset,tmp_path,monkeypatch):
    def deny(*a,**k):raise AssertionError('Network attempted')
    monkeypatch.setattr(socket.socket,'connect',deny)
    r=engine.process(dataset/'development'/'synthetic-02.pdf',tmp_path/'out')
    assert r['status']=='completed'

def test_consistent_placeholders_and_numbers(engine):
    from veil.detection import Session
    s=Session(engine.detector)
    a,_=s.redact('Name: Amelia Quill','one');b,_=s.redact('Name: Amelia Quill','two')
    assert a==b=='Name: [PERSON_001]'
    assert not engine.detector.find('Invoice total 123.45; quantity 42; year 2026; reference 1234567')

def test_never_overwrite(engine,dataset,tmp_path):
    src=dataset/'development'/'synthetic-00.pdf';out=tmp_path/'out';out.mkdir();(out/'sanitised.pdf').write_bytes(b'keep')
    with pytest.raises(FileExistsError):engine.process(src,out)
    assert (out/'sanitised.pdf').read_bytes()==b'keep'

def test_supported_chinese_is_detected_without_language_blanket_review(engine,dataset,tmp_path):
    r=engine.process(dataset/'development'/'synthetic-07.docx',tmp_path/'out')
    assert r['status']=='completed'
    assert r['entity_counts'].get('PERSON',0)>0
    assert '林小明' not in (tmp_path/'out/sanitised.txt').read_text()
    assert not any(w['code']=='unsupported_script' for w in r['issues'])

def test_group_split_name_and_email(engine,tmp_path):
    from docx import Document
    d=Document();p=d.add_paragraph('Name: ');p.add_run('Amelia ').bold=True;p.add_run('Quill')
    p=d.add_paragraph();p.add_run('split@exa').italic=True;p.add_run('mple.test')
    d.save(tmp_path/'in.docx');r=engine.process(tmp_path/'in.docx',tmp_path/'out')
    assert r['status']=='completed'
    text='\n'.join(p.text for p in Document(tmp_path/'out'/'sanitised.docx').paragraphs)
    assert 'Amelia' not in text and 'split@' not in text
    assert '[PERSON_001]' in text and '[EMAIL_ADDRESS_001]' in text

def test_nonempty_output_directory_is_never_deleted(engine,tmp_path):
    p=tmp_path/'bad.pdf';p.write_bytes(b'bad');out=tmp_path/'out';out.mkdir();(out/'original.txt').write_text('keep')
    with pytest.raises(FileExistsError):engine.process(p,out)
    assert (out/'original.txt').read_text()=='keep'

def test_benign_external_hyperlinks_preserved_without_fetch(engine,tmp_path,monkeypatch):
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.oxml.ns import qn
    d=Document();p=d.add_paragraph();rid=d.part.relate_to('https://example.test/private',RELATIONSHIP_TYPE.HYPERLINK,is_external=True)
    link=OxmlElement('w:hyperlink');link.set(qn('r:id'),rid);r=OxmlElement('w:r');t=OxmlElement('w:t');t.text='Email: link@example.test';r.append(t);link.append(r);p._p.append(link)
    source=tmp_path/'link.docx';d.save(source)
    monkeypatch.setattr(socket.socket,'connect',lambda *a,**k:pytest.fail('Network attempted'))
    result=engine.process(source,tmp_path/'out');assert result['status']=='completed'
    parts=package(tmp_path/'out'/'sanitised.docx')
    assert b'https://example.test/private' in b' '.join(parts.values())
    assert not result['removed_under_policy']

def test_revision_settings_and_ids_preserved(engine,dataset,tmp_path):
    source=dataset/'development'/'synthetic-00.docx'
    before=package(source);r=engine.process(source,tmp_path/'out');assert r['status']=='completed'
    after=package(tmp_path/'out/sanitised.docx')
    for name,data in before.items():
        if name.endswith('.xml'):
            old=E.fromstring(data);new=E.fromstring(after[name])
            for kind in ['rsids','trackRevisions','del','ins','commentReference']:
                query='//*[local-name()="'+kind+'"]'
                assert len(old.xpath(query))==len(new.xpath(query))
            assert sum(E.QName(k).localname.lower().startswith('rsid') for n in old.iter() if isinstance(n.tag,str) for k in n.attrib)==sum(E.QName(k).localname.lower().startswith('rsid') for n in new.iter() if isinstance(n.tag,str) for k in n.attrib)

@pytest.mark.parametrize('pages',[10,50])
def test_repeated_page_pdf_independent_poppler_validation(engine,tmp_path,pages):
    import subprocess,shutil
    if not shutil.which('pdfinfo'):pytest.skip('Install Poppler for independent PDF validation')
    d=pymupdf.open()
    for _ in range(pages):d.new_page().insert_text((50,100),'Email: repeated@example.test')
    source=tmp_path/'repeat.pdf';d.save(source);d.close()
    r=engine.process(source,tmp_path/'out');assert r['status']=='completed'
    output=tmp_path/'out'/'sanitised.pdf'
    result=subprocess.run(['pdfinfo',str(output)],capture_output=True,text=True,timeout=20)
    assert result.returncode==0 and 'Syntax Error' not in result.stderr,result.stderr
    assert str(pages) in result.stdout
    with pymupdf.open(output) as check:assert len({p.xref for p in check})==pages
