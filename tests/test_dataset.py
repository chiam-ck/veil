import json
from veil.datasets import generate

def test_reproducible_split_and_coverage(tmp_path):
    a=tmp_path/'a';b=tmp_path/'b';generate(a);generate(b)
    x=json.loads((a/'manifest.json').read_text());y=json.loads((b/'manifest.json').read_text())
    assert x==y
    assert len(x['documents'])==40
    groups={}
    for d in x['documents']:
        groups.setdefault(d['source_group'],set()).add(d['subset'])
        assert (a/d['path']).read_bytes()==(b/d['path']).read_bytes()
    assert all(len(v)==1 for v in groups.values())
    assert {d['format'] for d in x['documents']}=={'pdf','docx','pptx','xlsx'}
    assert {d['workload_units'] for d in x['documents']}=={1,10,50}

def test_generation_refuses_existing_files(tmp_path):
    import pytest
    p=tmp_path/'output';p.mkdir();(p/'original.txt').write_text('keep')
    with pytest.raises(FileExistsError):generate(p)
    assert (p/'original.txt').read_text()=='keep'
    assert len(list(p.iterdir()))==1
