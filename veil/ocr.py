"""CPU-only ensemble: preserve English PP-OCRv4 lines, use v5 for Han lines."""
from pathlib import Path
from rapidocr_onnxruntime import RapidOCR
from .languages import HAN

def overlap(a,b):
    def rect(box):return min(x[0] for x in box),min(x[1] for x in box),max(x[0] for x in box),max(x[1] for x in box)
    x,y,u,v=rect(a);i,j,k,l=rect(b)
    intersection=max(0,min(u,k)-max(x,i))*max(0,min(v,l)-max(y,j))
    return intersection/max(1,min((u-x)*(v-y),(k-i)*(l-j)))

class LocalOCR:
    def __init__(self):
        options=dict(det_use_cuda=False,cls_use_cuda=False,rec_use_cuda=False,intra_op_num_threads=2,inter_op_num_threads=1)
        self.english=RapidOCR(**options)
        self.multilingual=RapidOCR(rec_model_path=str(Path(__file__).parent/'models/ch_PP-OCRv5_rec_mobile.onnx'),**options)

    def __call__(self,image):
        old,_=self.english(image)
        new,_=self.multilingual(image)
        # Run both even when v4 finds no Han: v4 output is not a language oracle.
        han=[r for r in (new or []) if HAN.search(r[1])]
        rows=[r for r in (old or []) if not any(overlap(r[0],h[0])>.35 for h in han)]
        rows.extend(han)
        # If v4 yielded nothing, don't discard other scripts found by v5.
        if not rows:rows=list(new or [])
        rows.sort(key=lambda r:(round(min(p[1] for p in r[0])/12),min(p[0] for p in r[0])))
        return rows,None
