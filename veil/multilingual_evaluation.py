"""OCR character error, language latency and optical residual audits (not certification)."""
import io
import json
import unicodedata
import statistics
from collections import defaultdict,Counter
from PIL import Image
import pymupdf as fitz
from .engine import package
from .languages import T2S

def compact(text):return ''.join(unicodedata.normalize('NFKC',text).split())

def distance(a,b):
    if len(a)<len(b):a,b=b,a
    row=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        current=[i]
        for j,y in enumerate(b,1):current.append(min(current[-1]+1,row[j]+1,row[j-1]+(x!=y)))
        row=current
    return row[-1]

def evaluate(dataset,output,manifest,results):
    from .ocr import LocalOCR
    import numpy as np
    ocr=LocalOCR();groups=defaultdict(lambda:{'ocr_edits':0,'ocr_characters':0,'scriptfolded_ocr_edits':0,'ocr_documents':0,'optical_residual_expected_occurrences':0,'optical_audited_documents':0,'native_extraction_value_misses':0,'residual_extracted_or_package_occurrences':0,'ocr_value_misses':0,'detection_value_misses':0,'seconds':[],'stages':Counter()})
    audits=[]
    for i,(doc,result) in enumerate(zip(manifest['documents'],results)):
        group=groups[doc.get('language','unspecified')];folder=output/'single'/f'{i:02d}'
        extraction=folder/'original/extracted.json';segments=json.loads(extraction.read_text()) if extraction.exists() else []
        native='\n'.join(x['text'] for x in segments if x['source']=='native')
        group['native_extraction_value_misses']+=sum(e['value'] not in native for e in doc['entities'] if e['source']=='native')
        if doc.get('ocr_expected_text'):
            actual=compact('\n'.join(x['text'] for x in segments if x['source']=='ocr'));expected=compact(doc['ocr_expected_text'])
            group['ocr_edits']+=distance(expected,actual);group['ocr_characters']+=len(expected);group['ocr_documents']+=1
            group['scriptfolded_ocr_edits']+=distance(T2S.convert(expected),T2S.convert(actual))
        for key in ['ocr_value_misses','detection_value_misses']:group[key]+=result[key]
        group['residual_extracted_or_package_occurrences']+=result['residual_occurrences']
        group['seconds'].append(result['seconds']);group['stages'].update(result['stage_seconds'])
        path=folder/'output'/('sanitised.'+doc['format']);images=[]
        if path.exists():
            if doc['format']=='pdf':
                with fitz.open(path) as pdf:
                    for p in pdf:
                        pix=p.get_pixmap(matrix=fitz.Matrix(2,2),alpha=False);images.append(Image.frombytes('RGB',[pix.width,pix.height],pix.samples))
            else:
                for name,data in package(path).items():
                    if '/media/' in name:
                        with Image.open(io.BytesIO(data)) as im:images.append(im.convert('RGB'))
        if images:
            observed='\n'.join(r[1] for im in images for r in (ocr(np.asarray(im))[0] or []));value=compact(observed)
            # Repeat expected occurrences count once per declared entity. Optical audit can
            # itself miss glyphs, so zero is evidence, not proof of absent residual PII.
            residual=sum(compact(e['value']) in value or compact(T2S.convert(e['value'])) in compact(T2S.convert(observed)) for e in doc['entities'])
            group['optical_residual_expected_occurrences']+=residual;group['optical_audited_documents']+=1
            audits.append({'index':i,'language':doc['language'],'format':doc['format'],'images':len(images),'residual_expected_occurrences':residual})
    for g in groups.values():
        g['ocr_character_error_rate']=round(g['ocr_edits']/g['ocr_characters'],4) if g['ocr_characters'] else None
        g['scriptfolded_ocr_character_error_rate']=round(g['scriptfolded_ocr_edits']/g['ocr_characters'],4) if g['ocr_characters'] else None
        g['median_seconds']=round(statistics.median(g.pop('seconds')),4)
    return {'by_language':dict(groups),'optical_audits':audits,'method':'OCR CER uses edit distance after NFKC and whitespace removal, preserving Chinese script; secondary CER folds Traditional to Simplified. Exact complete-value entity scores stay strict. Native extraction misses scored separately. Optical audit reruns local OCR over final PDF pixels and Office embedded images; native Office rendering is inspected separately. Optical zero does not prove zero PII. No manual review is implied.'}


def markdown(measurements):
    lines=['','## Multilingual OCR and latency','', '| Cohort | OCR CER | Scriptfolded CER | Median job seconds | Extracted/package residual flags | Optical residual flags |','|---|---:|---:|---:|---:|---:|']
    for language,g in measurements['by_language'].items():
        lines.append(f'| {language} | {g["ocr_character_error_rate"]} | {g["scriptfolded_ocr_character_error_rate"]} | {g["median_seconds"]} | {g["residual_extracted_or_package_occurrences"]} | {g["optical_residual_expected_occurrences"]} |')
    lines+=['',measurements['method'],'','Residual flags count expected occurrences whose value appears anywhere in an audited output. Repeated gold values can produce multiple flags from one visible residual; these are not counts of unique visible redactions.']
    return lines
