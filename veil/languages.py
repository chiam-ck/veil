"""Script observations and reversible model views; public offsets are Python codepoints."""
import unicodedata
from difflib import SequenceMatcher
from collections import Counter
import regex
from opencc import OpenCC

HAN = regex.compile(r'\p{Script=Han}')
T2S = OpenCC('t2s')
S2T = OpenCC('s2t')
SUPPORTED = {'Latin', 'Han', 'Common', 'Inherited'}
SCRIPTS = ['Arabic','Cyrillic','Devanagari','Hiragana','Katakana','Hangul','Thai','Hebrew','Greek','Bengali','Tamil']

def profile(text):
    counts=Counter()
    if regex.search(r'\p{Script=Latin}',text): counts['English/Latin']=1
    if HAN.search(text):
        simple=T2S.convert(text)
        counts['Chinese-Traditional' if simple!=text else 'Chinese-Simplified' if S2T.convert(text)!=text else 'Chinese-shared-script']=1
    # Any letter outside supported scripts is a coverage gap, including scripts not listed above.
    unsupported=''.join(c for c in text if c.isalpha() and not regex.fullmatch(r'[\p{Script=Latin}\p{Script=Han}\p{Script=Common}\p{Script=Inherited}]',c))
    if unsupported:
        observed=[s for s in SCRIPTS if regex.search(fr'\p{{Script={s}}}',unsupported)]
        counts['unsupported:'+','.join(observed or ['other'])]=1
    return counts

class AlignedText:
    """Compose transformations while conservatively projecting changed spans to the source.

    SequenceMatcher operates on Unicode codepoints. Length-changing normalization and
    OpenCC phrase changes map to the entire corresponding source interval, never bytes.
    """
    def __init__(self,text):
        self.text=text
        self.spans=[(i,i+1) for i in range(len(text))]
        normalized=[];spans=[]
        for m in regex.finditer(r'\X',text):
            value=unicodedata.normalize('NFKC',m.group())
            normalized.append(value);spans.extend([m.span()]*len(value))
        self.text=''.join(normalized);self.spans=spans
        self.transform(T2S.convert(self.text))
        # Native PDF word extraction and OCR may insert spaces between Han characters.
        self.transform(regex.sub(r'(?<=\p{Han})[ \t]+(?=\p{Han})','',self.text))

    def transform(self,new):
        spans=[]
        for op,a,b,c,d in SequenceMatcher(None,self.text,new,autojunk=False).get_opcodes():
            if op=='equal': spans.extend(self.spans[a:b])
            elif op=='replace' and b-a==d-c:
                spans.extend(self.spans[a:b])
            elif d>c:
                source=self.spans[a:b]
                fallback=self.spans[min(a,len(self.spans)-1)] if self.spans else (0,0)
                span=(min(x[0] for x in source),max(x[1] for x in source)) if source else fallback
                spans.extend([span]*(d-c))
        self.text,self.spans=new,spans

    def original_span(self,start,end):
        spans=self.spans[start:end]
        return min(x[0] for x in spans),max(x[1] for x in spans)
