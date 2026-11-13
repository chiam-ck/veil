"""Imported corpus safety and annotation semantics; no network access."""
import importlib.util
import json
import sys
from pathlib import Path
import pytest

TOOLS=Path(__file__).resolve().parents[2]/'data'/'tools'

@pytest.fixture(scope='module')
def corpus():
    if not (TOOLS/'build_corpus.py').exists():pytest.skip('Optional sibling data corpus is absent')
    spec=importlib.util.spec_from_file_location('corpus_builder',TOOLS/'build_corpus.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def test_upstream_offset_mismatches_are_not_silently_gold(corpus):
    issues=[]
    spans=corpus.raw_spans('openpii',{'privacy_mask':[{'start':0,'end':4,'label':'GIVENNAME','value':'Wrong'}]},'Alice',issues)
    assert spans==[] and issues[0]['code']=='source_value_offset_mismatch'

def test_name_fragments_merge_but_ids_keep_locale_scope(corpus):
    text='Alice Tan'
    spans=[{'start':0,'end':5,'type':'PERSON','value':'Alice','source_labels':['GIVENNAME'],'source':'native'}, {'start':6,'end':9,'type':'PERSON','value':'Tan','source_labels':['SURNAME'],'source':'native'}]
    assert corpus.merge_spans(text,spans)[0]['value']=='Alice Tan'
    assert corpus.label_type('IDCARDNUM','S1234567D','SG')=='NRIC_FIN'
    assert corpus.label_type('ID_NUMBER','110101199003071234','CN') is None
    assert corpus.label_type('CITY','Singapore','SG') is None

def test_source_formula_strings_are_never_created_as_formulas(corpus,tmp_path):
    from openpyxl import load_workbook
    record={'text':'=WEBSERVICE("https://example.test/private")\n+SUM(1,2)'}
    output=tmp_path/'source.xlsx';corpus.xlsx_text(record,output)
    wb=load_workbook(output,data_only=False)
    assert wb.active['A3'].data_type=='s' and wb.active['A3'].value.startswith('=WEBSERVICE')
    assert not any(c.data_type=='f' for row in wb.active for c in row)

def test_pdf_cjk_punctuation_and_emoji_survive(corpus,tmp_path):
    import pymupdf
    text='中文姓名：林小明；地址：虚构路88号。🔥 Contact: Alice Tan'
    output=tmp_path/'unicode.pdf';corpus.pdf_text({'text':text},output)
    with pymupdf.open(output) as pdf:
        recovered=''.join(pdf[0].get_text().split())
        assert ''.join(text.split()) in recovered

def test_partial_annotation_scope_does_not_count_unknown_classes(tmp_path):
    from veil.benchmark import score
    original=tmp_path/'original';original.mkdir();out=tmp_path/'output';out.mkdir()
    text='Email: sample@example.test Phone: 81234567'
    (original/'extracted.json').write_text(json.dumps([{'location':'p','text':text,'source':'native'}]))
    start=text.index('sample');end=text.index(' Phone')
    doc={'format':'pdf','entities':[{'type':'EMAIL_ADDRESS','value':'sample@example.test','source':'native'}],'scored_types':['EMAIL_ADDRESS']}
    report={'entities':[{'type':'EMAIL_ADDRESS','start':start,'end':end,'location':'p','source':'native'}, {'type':'PHONE_NUMBER','start':text.index('8123'),'end':len(text),'location':'p','source':'native'}]}
    result=score(doc,report,tmp_path)
    assert sum(r['tp'] for r in result['rows'])==1
    assert sum(r['fp'] for r in result['rows'])==0
    # For a fully annotated negative control, the same unwanted detection must count as a false positive.
    doc.update(entities=[],scored_types=['PHONE_NUMBER'])
    assert sum(r['fp'] for r in score(doc,report,tmp_path)['rows'])==1
