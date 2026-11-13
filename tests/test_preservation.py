import json
import zipfile
from pathlib import Path
import pytest
from lxml import etree as E
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from openpyxl import Workbook,load_workbook
from openpyxl.comments import Comment
from openpyxl.workbook.defined_name import DefinedName
from veil.engine import package

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
S='http://schemas.openxmlformats.org/spreadsheetml/2006/main'

def rewrite(path,update):
    parts=package(path);update(parts)
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in parts.items():z.writestr(name,data)

def cache(parts,values):
    root=E.fromstring(parts['xl/worksheets/sheet1.xml'])
    for coord,(value,kind) in values.items():
        cell=root.find('.//{'+S+'}c[@r="'+coord+'"]');cell.set('t',kind)
        v=cell.find('{'+S+'}v')
        if v is None:v=E.SubElement(cell,'{'+S+'}v')
        v.text=value
    parts['xl/worksheets/sheet1.xml']=E.tostring(root)

def test_benign_formulas_names_comments_and_caches_survive(engine,tmp_path):
    p=tmp_path/'in.xlsx';w=Workbook();s=w.active;s.title='Budget';s.append([12,30]);s['C1']='=SUM( A1,   B1 )';s['C1'].number_format='#,##0.00';s['A2'].comment=Comment('Check totals before submitting.','Finance team')
    w.defined_names.add(DefinedName('Amounts',attr_text="'Budget'!$A$1:$B$1"));s['C2']='=SUM(Amounts)';s['C3']='=IF(A1>0,"approved","pending")';w.save(p)
    rewrite(p,lambda parts:cache(parts,{'C1':('42','n'),'C2':('42','n'),'C3':('approved','str')}))
    original=p.read_bytes();r=engine.process(p,tmp_path/'out');assert r['status']=='completed',r
    assert original==p.read_bytes() and not r['removed_under_policy']
    out=tmp_path/'out/sanitised.xlsx';v=load_workbook(out);data=load_workbook(out,data_only=True)
    assert v.active['C1'].value=='=SUM( A1,   B1 )' and data.active['C1'].value==42
    assert v.active['C1'].number_format=='#,##0.00'
    assert v.active['C2'].value=='=SUM(Amounts)' and data.active['C2'].value==42
    assert v.defined_names['Amounts'].attr_text=="'Budget'!$A$1:$B$1"
    assert v.active['A2'].comment.text=='Check totals before submitting.'
    assert v.active['A2'].comment.author=='Finance team'
    assert any(n.endswith('.vml') for n in package(out))
    assert v.active['C3'].value=='=IF(A1>0,"approved","pending")'


def test_only_pii_formula_literals_comments_and_caches_change(engine,tmp_path):
    p=tmp_path/'in.xlsx';w=Workbook();s=w.active;s['A1']='="private@example.test"';s['B1']='=SUM(1,2)';s['C1']='="public memo"';s['A2'].comment=Comment('Please contact private@example.test about this total.','Amelia Quill');w.save(p)
    rewrite(p,lambda parts:cache(parts,{'A1':('private@example.test','str'),'B1':('3','n'),'C1':('public memo','str')}))
    r=engine.process(p,tmp_path/'out');assert r['status']=='completed',r
    out=tmp_path/'out/sanitised.xlsx';v=load_workbook(out);data=load_workbook(out,data_only=True)
    assert v.active['A1'].value=='="[EMAIL_ADDRESS_001]"' and data.active['A1'].value=='[EMAIL_ADDRESS_001]'
    assert v.active['B1'].value=='=SUM(1,2)' and data.active['B1'].value==3
    assert v.active['C1'].value=='="public memo"'
    comment=v.active['A2'].comment
    assert comment and comment.text=='Please contact [EMAIL_ADDRESS_001] about this total.'
    assert 'Amelia' not in comment.author and comment.author.startswith('[PERSON_')
    assert b'private@example.test' not in b' '.join(package(out).values())
    assert not r['removed_under_policy'] and r['content_changes']['PII formula literals redacted; formula retained']==1
    assert 'private@example.test' not in json.dumps(r)


def test_only_dependent_formula_caches_invalidated(engine,tmp_path):
    p=tmp_path/'in.xlsx';w=Workbook();s=w.active;s['A1']='person@example.test';s['B1']='=A1';s['C1']='=B1';s['D1']='=SUM(1,2)';w.save(p)
    rewrite(p,lambda parts:cache(parts,{'B1':('person@example.test','str'),'C1':('person@example.test','str'),'D1':('3','n')}))
    r=engine.process(p,tmp_path/'out');assert r['status']=='review_required',r
    out=tmp_path/'out/sanitised.xlsx';v=load_workbook(out);data=load_workbook(out,data_only=True)
    assert v.active['B1'].value=='=A1' and v.active['C1'].value=='=B1'
    assert data.active['B1'].value is None and data.active['C1'].value is None
    assert data.active['D1'].value==3 and v.active['D1'].value=='=SUM(1,2)'
    assert not r['removed_under_policy']


@pytest.mark.parametrize('formula',['=CHAR(65)&CHAR(109)','=CONCAT("Am","elia Quill")','=INDIRECT("A1")',"='[external.xlsx]Sheet1'!A1"])
def test_unsupported_formulas_block_without_stripping_output(engine,tmp_path,formula):
    p=tmp_path/'in.xlsx';w=Workbook();w.active['A1']=formula;w.save(p);original=p.read_bytes()
    r=engine.process(p,tmp_path/'out');assert r['status']=='blocked',r
    assert p.read_bytes()==original and list((tmp_path/'out').iterdir())==[tmp_path/'out/report.json']
    assert 'Amelia' not in json.dumps(r)


def test_preexisting_uncached_formula_is_informational(engine,tmp_path):
    p=tmp_path/'in.xlsx';w=Workbook();w.active['A1']='=SUM(1,2)';w.save(p)
    r=engine.process(p,tmp_path/'out');assert r['status']=='completed'
    assert load_workbook(tmp_path/'out/sanitised.xlsx').active['A1'].value=='=SUM(1,2)'
    assert not r['warnings']
    assert any('without evaluation' in x for x in r['notices'])


def test_shared_formulas_keep_structure(engine,tmp_path):
    p=tmp_path/'in.xlsx';w=Workbook();s=w.active;s['A1']=5;s['A2']=7;s['B1']='=A1*2';s['B2']='=A2*2';w.save(p)
    def patch(parts):
        cache(parts,{'B1':('10','n'),'B2':('14','n')});r=E.fromstring(parts['xl/worksheets/sheet1.xml'])
        a=r.find('.//{'+S+'}c[@r="B1"]/{'+S+'}f');a.set('t','shared');a.set('si','0');a.set('ref','B1:B2')
        b=r.find('.//{'+S+'}c[@r="B2"]/{'+S+'}f');b.text=None;b.set('t','shared');b.set('si','0');parts['xl/worksheets/sheet1.xml']=E.tostring(r)
    rewrite(p,patch);r=engine.process(p,tmp_path/'out');assert r['status']=='completed',r
    out=tmp_path/'out/sanitised.xlsx';v=load_workbook(out);assert v.active['B2'].value=='=A2*2'
    root=E.fromstring(package(out)['xl/worksheets/sheet1.xml']);assert len(root.xpath('//*[local-name()="f"][@t="shared"]'))==2


def test_revision_structure_and_both_text_views_preserved(engine,tmp_path):
    p=tmp_path/'in.docx';d=Document();para=d.add_paragraph('Please keep this explanation. ')
    for kind,content in [('del','old@example.test'),('ins','new@example.test')]:
        rev=OxmlElement('w:'+kind);rev.set(qn('w:id'),'7' if kind=='del' else '8');rev.set(qn('w:author'),'Amelia Quill');rev.set(qn('w:date'),'2026-01-01T00:00:00Z')
        run=OxmlElement('w:r');text=OxmlElement('w:delText' if kind=='del' else 'w:t');text.text=content;run.append(text);rev.append(run);para._p.append(rev)
    d.add_paragraph('A retained ordinary sentence.');d.save(p);r=engine.process(p,tmp_path/'out');assert r['status'] in {'completed','review_required'},r
    parts=package(tmp_path/'out/sanitised.docx');root=E.fromstring(parts['word/document.xml'])
    for kind in ['del','ins']:
        nodes=root.xpath('//*[local-name()="'+kind+'"]');assert len(nodes)==1
        assert nodes[0].get(qn('w:date'))=='2026-01-01T00:00:00Z'
        assert nodes[0].get(qn('w:author')).startswith('[PERSON_')
        assert '[EMAIL_ADDRESS_' in ''.join(nodes[0].itertext())
    assert 'Please keep this explanation.' in ''.join(root.itertext())
    assert b'old@example.test' not in b' '.join(parts.values()) and b'new@example.test' not in b' '.join(parts.values())
    assert not r['removed_under_policy']


def test_benign_word_comments_and_anchors_preserved(engine,dataset,tmp_path):
    p=tmp_path/'in.docx';p.write_bytes((dataset/'development/synthetic-00.docx').read_bytes())
    def patch(parts):
        root=E.fromstring(parts['word/comments.xml']);root.find('.//{'+W+'}t').text='Please verify these totals.';parts['word/comments.xml']=E.tostring(root)
    rewrite(p,patch);r=engine.process(p,tmp_path/'out');assert r['status'] in {'completed','review_required'},r
    parts=package(tmp_path/'out/sanitised.docx');assert 'word/comments.xml' in parts
    assert b'Please verify these totals.' in parts['word/comments.xml']
    assert b'commentReference' in parts['word/document.xml']
    assert b'comments.xml' in parts['word/_rels/document.xml.rels']
    assert b'<w:del' in parts['word/document.xml'] and b'<w:ins' in parts['word/document.xml']


@pytest.mark.parametrize('instruction,blocked',[(r' PAGE \* MERGEFORMAT ',False),(r' INCLUDETEXT "https://example.test/x" ',True)])
def test_word_fields_not_silently_deleted(engine,tmp_path,instruction,blocked):
    p=tmp_path/'in.docx';d=Document();para=d.add_paragraph();field=OxmlElement('w:fldSimple');field.set(qn('w:instr'),instruction);run=OxmlElement('w:r');text=OxmlElement('w:t');text.text='1';run.append(text);field.append(run);para._p.append(field);d.save(p)
    r=engine.process(p,tmp_path/'out')
    if blocked:assert r['status']=='blocked'
    else:
        assert r['status'] in {'completed','review_required'},r
        root=E.fromstring(package(tmp_path/'out/sanitised.docx')['word/document.xml']);assert root.find('.//{'+W+'}fldSimple').get(qn('w:instr'))==instruction


def test_named_dependent_cache_and_text_do_not_retain_redacted_number(engine,tmp_path):
    p=tmp_path/'in.xlsx';w=Workbook();s=w.active;s.title='Budget';s['A1']='Phone';s['B1']=81234567;s['C1']='=SUM(SensitiveInput)';s['D1']='=SUM(1,2)';w.defined_names.add(DefinedName('SensitiveInput',attr_text="'Budget'!$B$1"));w.save(p)
    rewrite(p,lambda parts:cache(parts,{'C1':('81234567','n'),'D1':('3','n')}))
    r=engine.process(p,tmp_path/'out');assert r['status']=='review_required',r
    out=tmp_path/'out/sanitised.xlsx';v=load_workbook(out);data=load_workbook(out,data_only=True)
    assert v.active['C1'].value=='=SUM(SensitiveInput)' and data.active['C1'].value is None
    assert data.active['D1'].value==3
    assert '81234567' not in (tmp_path/'out/sanitised.txt').read_text()
    assert any(a['action']=='formula_cache_invalidated' and a['location'].endswith('C1') for a in r['content_adjustments'])


def test_benign_revision_text_preserved_and_shared_pii_runs_sanitised(engine,tmp_path):
    p=tmp_path/'in.docx';d=Document();plain=d.add_paragraph('Keep this: ')
    for kind,content in [('del','earlier delivery date'),('ins','revised delivery date')]:
        rev=OxmlElement('w:'+kind);r=OxmlElement('w:r');t=OxmlElement('w:delText' if kind=='del' else 'w:t');t.text=content;r.append(t);rev.append(r);plain._p.append(rev)
    para=d.add_paragraph('Name: Amelia ')
    for kind,content in [('del','Quill'),('ins','Linden')]:
        rev=OxmlElement('w:'+kind);r=OxmlElement('w:r');t=OxmlElement('w:delText' if kind=='del' else 'w:t');t.text=content;r.append(t);rev.append(r);para._p.append(rev)
    d.save(p);r=engine.process(p,tmp_path/'out');assert r['status']=='review_required',r
    root=E.fromstring(package(tmp_path/'out/sanitised.docx')['word/document.xml']);content=''.join(root.itertext())
    assert 'earlier delivery date' in content and 'revised delivery date' in content
    assert all(v not in content for v in ['Amelia','Quill','Linden'])
    assert len(root.findall('.//{'+W+'}del'))==2 and len(root.findall('.//{'+W+'}ins'))==2
    assert any('revision views' in v for v in r['warnings'])


def test_powerpoint_comments_authors_and_relationships_survive(engine,tmp_path):
    from pptx import Presentation
    p=tmp_path/'in.pptx';prs=Presentation();prs.slides.add_slide(prs.slide_layouts[6]);prs.save(p)
    P='http://schemas.openxmlformats.org/presentationml/2006/main';PKG='http://schemas.openxmlformats.org/package/2006/relationships'
    def patch(parts):
        parts['ppt/comments/comment1.xml']=(f'<p:cmLst xmlns:p="{P}"><p:cm authorId="0" dt="2026-01-01T00:00:00Z" idx="1"><p:pos x="0" y="0"/><p:text>Keep this comment; contact slide@example.test.</p:text></p:cm></p:cmLst>').encode()
        parts['ppt/commentAuthors.xml']=(f'<p:cmAuthorLst xmlns:p="{P}"><p:cmAuthor id="0" name="Amelia Quill" initials="AQ" lastIdx="1" clrIdx="0"/></p:cmAuthorLst>').encode()
        for path,typ,target in [('ppt/slides/_rels/slide1.xml.rels','comments','../comments/comment1.xml'),('ppt/_rels/presentation.xml.rels','commentAuthors','commentAuthors.xml')]:
            root=E.fromstring(parts[path]);rel=E.SubElement(root,'{'+PKG+'}Relationship');rel.set('Id','rIdPreserveTest');rel.set('Type','http://schemas.openxmlformats.org/officeDocument/2006/relationships/'+typ);rel.set('Target',target);parts[path]=E.tostring(root)
        root=E.fromstring(parts['[Content_Types].xml'])
        for name,mime in [('ppt/comments/comment1.xml','comments'),('ppt/commentAuthors.xml','commentAuthors')]:
            n=E.SubElement(root,'{http://schemas.openxmlformats.org/package/2006/content-types}Override');n.set('PartName','/'+name);n.set('ContentType','application/vnd.openxmlformats-officedocument.presentationml.'+mime+'+xml')
        parts['[Content_Types].xml']=E.tostring(root)
    rewrite(p,patch);r=engine.process(p,tmp_path/'out');assert r['status'] in {'completed','review_required'},r
    out=tmp_path/'out/sanitised.pptx';parts=package(out);assert len(Presentation(out).slides)==1
    assert b'Keep this comment' in parts['ppt/comments/comment1.xml'] and b'slide@example.test' not in parts['ppt/comments/comment1.xml']
    author=E.fromstring(parts['ppt/commentAuthors.xml'])[0];assert author.get('name').startswith('[PERSON_') and author.get('initials')=='REDACTED'
    assert b'comment1.xml' in parts['ppt/slides/_rels/slide1.xml.rels']
    assert r['removed_under_policy']=={'saved_printer_settings':1}
    assert not any('/printerSettings/' in n for n in parts)


def test_sensitive_hyperlink_target_disabled_but_label_retained(engine,tmp_path):
    from docx.opc.constants import RELATIONSHIP_TYPE
    p=tmp_path/'in.docx';d=Document();para=d.add_paragraph();rid=d.part.relate_to('mailto:link@example.test',RELATIONSHIP_TYPE.HYPERLINK,is_external=True)
    link=OxmlElement('w:hyperlink');link.set(qn('r:id'),rid);run=OxmlElement('w:r');t=OxmlElement('w:t');t.text='Contact support';run.append(t);link.append(run);para._p.append(link);d.save(p)
    r=engine.process(p,tmp_path/'out');assert r['status'] in {'completed','review_required'},r
    parts=package(tmp_path/'out/sanitised.docx');assert b'Contact support' in parts['word/document.xml']
    assert b'about:blank' in parts['word/_rels/document.xml.rels'] and b'link@example.test' not in parts['word/_rels/document.xml.rels']


def test_bare_chinese_formula_literal_uses_known_identity_without_erasing_formula(engine,tmp_path):
    p=tmp_path/'in.xlsx';w=Workbook();s=w.active;s['A1']='姓名';s['B1']='张伟';s['D1']='="张伟"';w.save(p)
    r=engine.process(p,tmp_path/'out');assert r['status']=='completed',r
    out=tmp_path/'out/sanitised.xlsx';v=load_workbook(out)
    assert v.active['D1'].value=='="[PERSON_001]"' and v.active['B1'].value=='[PERSON_001]'
    assert '张伟' not in (tmp_path/'out/sanitised.txt').read_text()
    assert '张伟' not in ' '.join(data.decode() for name,data in package(out).items() if name.endswith('.xml'))
