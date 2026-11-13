import io
import json
import socket
from pathlib import Path
import pytest
from docx import Document
from veil.engine import package
from veil.languages import AlignedText, profile


@pytest.mark.parametrize('text,values',[
 ('😀姓名：陳怡君；地址：臺北市大安區仁愛路８８號；客戶編號：ＡＢＣ－１２３４；電話＋６５ ８１２３ ４５６７',[('PERSON','陳怡君'),('ADDRESS','臺北市大安區仁愛路８８號'),('ACCOUNT_ID','ＡＢＣ－１２３４'),('PHONE_NUMBER','＋６５ ８１２３ ４５６７')]),
 ('𠮷提示 ﬁ 姓名: 张伟；邮箱test@example.test，身份证S1234567D',[('PERSON','张伟'),('EMAIL_ADDRESS','test@example.test'),('NRIC_FIN','S1234567D')]),
 ('李文静住在上海市浦东新区丁香路88号。',[('PERSON','李文静'),('ADDRESS','上海市浦东新区丁香路88号')]),
 ('姓名: 张 伟；地址: 上海 市浦东新区丁香路88号',[('PERSON','张 伟'),('ADDRESS','上海 市浦东新区丁香路88号')]),
])
def test_source_codepoint_offsets(engine,text,values):
    hits=engine.detector.find(text)
    actual={(h.kind,text[h.start:h.end]) for h in hits}
    assert set(values)<=actual
    assert all(0<=h.start<h.end<=len(text) for h in hits)

@pytest.mark.parametrize('original',['😀ＡＢﬁ臺北市','𠮷张 伟','陳怡君臺灣','ﬁ姓名张伟'])
def test_normalization_alignment(original):
    view=AlignedText(original)
    assert len(view.text)==len(view.spans)
    for a,b in view.spans:assert 0<=a<b<=len(original)
    a,b=view.original_span(0,len(view.text));assert original[a:b]==original

def test_chinese_table_context(engine):
    for label,value,kind in [('姓名','陳怡君','PERSON'),('地址','臺北市大安區仁愛路88號','ADDRESS'),('客戶編號','TEST-CN-3242','ACCOUNT_ID')]:
        hits=engine.detector.find(value,context=label)
        assert any(h.start==0 and h.end==len(value) and h.kind==kind for h in hits)

@pytest.mark.parametrize('cohort',['zh-Hans','zh-Hant','mixed-Hans','mixed-Hant'])
@pytest.mark.parametrize('fmt',['docx','pptx','xlsx','pdf'])
def test_native_multilingual_all_formats(engine,tmp_path,cohort,fmt,caplog,multilingual_dataset):
    source=multilingual_dataset/cohort/'development/native'/('synthetic.'+fmt)
    r=engine.process(source,tmp_path/'out');assert r['status']=='completed',r
    assert r['entity_counts'].get('PERSON',0)>=1 and r['entity_counts'].get('ADDRESS',0)>=1
    assert not any(k.startswith('unsupported') for k in r['language_coverage']['observed_segment_counts'])
    clean=(tmp_path/'out/sanitised.txt').read_text()
    for value in ['张伟','陳怡君','synthetic@example.test','S1234567D','VEIL-CN-4281','上海市浦东新区丁香路88号','臺北市大安區仁愛路88號']:
        assert value not in clean
        assert value not in json.dumps(r,ensure_ascii=False)+caplog.text
    if fmt!='pdf':
        xml=' '.join(d.decode('utf8') for n,d in package(tmp_path/'out'/('sanitised.'+fmt)).items() if n.endswith('.xml'))
        assert 'Noto Sans CJK' in xml
        assert '张伟' not in xml and '陳怡君' not in xml

@pytest.mark.parametrize('fmt',['docx','pptx','xlsx','pdf'])
def test_traditional_scan_is_detected_and_masked(engine,tmp_path,fmt,multilingual_dataset):
    r=engine.process(multilingual_dataset/'zh-Hant/development/scan'/('synthetic.'+fmt),tmp_path/'out')
    assert r['status']=='completed'
    assert r['entity_counts'].get('PERSON',0)>=1
    assert r['entity_counts'].get('ADDRESS',0)>=1
    assert any(row['lines']>0 for row in r['ocr_observations'])
    text=(tmp_path/'out/sanitised.txt').read_text();assert '陳怡君' not in text

def test_unsupported_script_requires_review(engine,tmp_path):
    d=Document();d.add_paragraph('Name: Amelia Quill');d.add_paragraph('Имя: Александр Иванов');p=tmp_path/'in.docx';d.save(p)
    r=engine.process(p,tmp_path/'out');assert r['status']=='review_required'
    assert any('Unsupported language' in w for w in r['warnings'])

def test_chinese_runs_hidden_and_no_network(engine,tmp_path,monkeypatch):
    d=Document();p=d.add_paragraph('姓名：');p.add_run('陳').bold=True;p.add_run('怡君');d.sections[0].header.paragraphs[0].text='地址：臺北市大安區仁愛路88號'
    hidden=d.add_paragraph('客戶編號：CN-99887');hidden.runs[0].font.hidden=True
    source=tmp_path/'in.docx';d.save(source)
    monkeypatch.setattr(socket.socket,'connect',lambda *a,**k:pytest.fail('Outbound network attempted'))
    r=engine.process(source,tmp_path/'out');assert r['status']=='completed'
    parts=package(tmp_path/'out/sanitised.docx');xml=' '.join(x.decode() for n,x in parts.items() if n.endswith('.xml'))
    assert '怡君' not in xml and '臺北市大安區仁愛路88號' not in xml and 'CN-99887' not in xml
    assert '[PERSON_001]' in xml

@pytest.mark.parametrize('text',['合成测试材料：总金额123.45，数量42，年份2026，订单参考1234567','合成測試材料：金額123.45，數量42，年份2026'])
def test_chinese_negative_numbers(engine,text):
    assert not engine.detector.find(text)


def test_mixed_english_chinese_entities_same_segment(engine):
    text='姓名：张伟；Contact person: Amelia Quill; Email: mixed@example.test；地址：上海市浦东新区丁香路88号；电话+65 8123 4567'
    values={(h.kind,text[h.start:h.end]) for h in engine.detector.find(text)}
    assert {('PERSON','张伟'),('PERSON','Amelia Quill'),('EMAIL_ADDRESS','mixed@example.test'),('ADDRESS','上海市浦东新区丁香路88号'),('PHONE_NUMBER','+65 8123 4567')}<=values


def test_native_pdf_chinese_coordinates_are_opaque(engine,tmp_path):
    import pymupdf as fitz
    from PIL import Image
    pdf=fitz.open();page=pdf.new_page();page.insert_text((45,100),'姓名：张伟',fontname='china-s',fontsize=24)
    rects=page.search_for('张伟');assert rects
    source=tmp_path/'in.pdf';pdf.save(source);pdf.close()
    result=engine.process(source,tmp_path/'out');assert result['entity_counts']['PERSON']==1
    with fitz.open(tmp_path/'out/sanitised.pdf') as output:
        pix=output[0].get_pixmap(matrix=fitz.Matrix(2,2),alpha=False);im=Image.frombytes('RGB',[pix.width,pix.height],pix.samples)
        for rect in rects:
            crop=im.crop((int(rect.x0*2),int(rect.y0*2),int(rect.x1*2),int(rect.y1*2)))
            import numpy as np
            assert (np.asarray(crop)<20).all(axis=2).mean()>.98


def test_cer_matching_and_group_split(multilingual_dataset):
    from veil.multilingual_evaluation import distance
    assert distance('陳怡君','陈怡君')==1
    assert distance('姓名张伟','姓名张伟')==0
    manifest=json.loads((multilingual_dataset/'manifest.json').read_text())
    groups={}
    for doc in manifest['documents']:
        groups.setdefault(doc['source_group'],set()).add(doc['subset'])
    assert all(len(s)==1 for s in groups.values())


def test_excel_shared_strings_and_column_labels(engine,tmp_path):
    import xlsxwriter
    from openpyxl import load_workbook
    source=tmp_path/'in.xlsx';w=xlsxwriter.Workbook(source);s=w.add_worksheet()
    s.write_row(0,0,['姓名','地址','客戶編號']);s.write_row(1,0,['陳怡君','臺北市大安區仁愛路88號','CUST-998877'])
    w.close()
    result=engine.process(source,tmp_path/'out');assert result['status']=='completed',result
    output=tmp_path/'out/sanitised.xlsx';wb=load_workbook(output)
    assert [wb.active.cell(2,c).value for c in [1,2,3]]==['[PERSON_001]','[ADDRESS_001]','[ACCOUNT_ID_001]']
    parts=package(output);xml=' '.join(v.decode() for n,v in parts.items() if n.endswith('.xml'))
    for value in ['陳怡君','臺北市大安區仁愛路88號','CUST-998877']:assert value not in xml


def test_llm_only_chinese_detections(engine):
    assert engine.detector.describe()['mode']=='llm'
    assert not hasattr(engine.detector,'analyzer')
    text='姓名：张伟\nInvoice total 123.45; quantity 42\n合成测试材料'
    hits=engine.detector.find(text)
    assert [(h.kind,text[h.start:h.end]) for h in hits]==[('PERSON','张伟')]


def test_phone_matching_is_complete_not_partial():
    from veil.benchmark import entity_normal
    assert entity_normal('PHONE_NUMBER','+65 8123 4567')==entity_normal('PHONE_NUMBER','+6581234567')
    assert entity_normal('PHONE_NUMBER','8123 4567')!=entity_normal('PHONE_NUMBER','+6581234567')


def test_traditional_account_and_contact_labels(engine):
    text='聯絡人：陳怡君；帳號：ACCT-TW-90321；通訊地址：臺北市大安區仁愛路88號'
    actual={(h.kind,text[h.start:h.end]) for h in engine.detector.find(text)}
    assert {('PERSON','陳怡君'),('ACCOUNT_ID','ACCT-TW-90321'),('ADDRESS','臺北市大安區仁愛路88號')}<=actual


@pytest.mark.parametrize('text,expected',[
 ('患者赵晴，手机138 2468 1357',[('PERSON','赵晴'),('PHONE_NUMBER','138 2468 1357')]),
 ('收到 蒋文 13924681357',[('PERSON','蒋文'),('PHONE_NUMBER','13924681357')]),
 ('Ms.晓雨 Rowan Vale（测试身份）',[('PERSON','晓雨 Rowan Vale')]),
 ('请发送至合成邮箱@protonmail.com进行备档。',[('EMAIL_ADDRESS','合成邮箱@protonmail.com')]),
 ('旅客梁清，客户数量42，数量2026',[('PERSON','梁清')]),
 ('居住于虚构大道号1234，合成测试',[('ADDRESS','虚构大道号1234')]),
])
def test_non_template_chinese_contexts(engine,text,expected):
    found={(h.kind,text[h.start:h.end]) for h in engine.detector.find(text)}
    assert set(expected)<=found


def test_unsupported_script_in_hidden_sheet_name_requires_review(engine,tmp_path):
    from openpyxl import Workbook
    w=Workbook();w.active['A1']='SYNTHETIC TEST MATERIAL';s=w.create_sheet('秘密カナ');s.sheet_state='hidden';s['A1']='year 2026'
    path=tmp_path/'in.xlsx';w.save(path);r=engine.process(path,tmp_path/'out')
    assert r['status']=='review_required'
    assert any('Unsupported language' in x for x in r['warnings'])


def test_unicode_email_local_part_is_not_cut_at_a_name_character(engine):
    text='电邮：至诚@example.test'
    assert any(h.kind=='EMAIL_ADDRESS' and text[h.start:h.end]=='至诚@example.test' for h in engine.detector.find(text))
