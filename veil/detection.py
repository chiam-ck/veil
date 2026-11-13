"""Source spans and per-document redaction state for LLM detections."""
import re
from dataclasses import dataclass
from collections import Counter
from .languages import HAN, profile


@dataclass(frozen=True)
class Hit:
    start: int
    end: int
    kind: str
    score: float


class Session:
    def __init__(self, detector):
        self.detector = detector
        self.mapping = {}  # Only in memory. Never serialised.
        self.redaction_values = []  # Private snapshot stored only beside originals.
        self.counts = Counter()
        self.entities = []
        self.originals = []
        self.sanitised = []
        self.timings = Counter()
        self.warnings = set()
        self.notices = set()
        self.issues = []
        self._issue_keys = set()
        self.locations = {}
        self.removals = Counter()
        self.changes = Counter()
        self.adjustments = []
        self.uninspected = []
        self.ocr_observations = []
        self.languages = Counter()
        self.local_structure_values = 0

    def issue(self, code, severity, message, location, action):
        """Value-free, actionable findings. Informational notices do not gate status."""
        if severity not in {'info','review'}:raise ValueError('Invalid issue severity')
        key=(code,severity,location)
        if key in self._issue_keys:return
        self._issue_keys.add(key)
        prefix=':'.join(location.split(':')[:2])
        label=self.locations.get(prefix,location)
        if prefix in self.locations and location!=prefix:label+=' · '+location[len(prefix)+1:]
        elif location.startswith('page:'):label='PDF '+location.replace(':',' ')
        self.issues.append(dict(code=code,severity=severity,message=message,location=location,location_label=label,action=action))
        (self.warnings if severity=='review' else self.notices).add(message)

    def placeholder(self,kind,value):
        key=(kind,value.casefold())
        if key not in self.mapping:
            n=1+sum(k[0]==kind for k in self.mapping)
            self.mapping[key]=f'[{kind}_{n:03d}]'
            self.redaction_values.append({'kind':kind,'text':value})
        return self.mapping[key]

    def known_hits(self,text,hits=None):
        hits=list(hits or [])
        protected=[m.span() for m in re.finditer(r"\[[A-Z_]+_\d{3,}\]",text)]
        for (kind,value) in sorted(self.mapping,key=lambda key:-len(key[1])):
            if len(value)<4 and not HAN.search(value):continue
            boundary='' if HAN.search(value) else r'(?<!\w)'
            end='' if HAN.search(value) else r'(?!\w)'
            for m in re.finditer(boundary+re.escape(value)+end,text,re.I):
                a,b=m.span()
                if any(a<y and b>x for x,y in protected) or any(h.start<=a and h.end>=b for h in hits):continue
                hits=[h for h in hits if not (h.start<b and h.end>a)]+[Hit(a,b,kind,.95)]
        hits.sort(key=lambda h:h.start)
        return hits

    def sanitise_previews(self):
        # Derived previews may include orphan strings inspected before a labelled
        # occurrence established their identity. Do not expose those original values.
        # This changes no source offsets or original-extraction records.
        for segment in self.sanitised:
            text=segment['text'];hits=self.known_hits(text)
            if not hits:continue
            chunks=[];last=0
            for hit in hits:
                chunks.extend([text[last:hit.start],self.placeholder(hit.kind,text[hit.start:hit.end])]);last=hit.end
            chunks.append(text[last:]);segment['text']=''.join(chunks)
            self.changes['Known PII propagated into sanitised text previews']+=1
            self.adjustments.append({'location':segment['location'],'action':'known_pii_preview_redacted'})

    def redact(self, text, location, source="native", structured=False, include_text=True, context=None, known_values=False):
        import time
        started = time.perf_counter()
        hits = self.detector.find(text,structured=structured,context=context) if text.strip() else []
        if known_values:hits=self.known_hits(text,hits)
        self.timings["detection"] += time.perf_counter()-started
        pieces, last = [], 0
        for h in hits:
            key = (h.kind, text[h.start:h.end].casefold())
            placeholder=self.placeholder(h.kind,text[h.start:h.end])
            pieces += [text[last:h.start], placeholder]
            last = h.end
            self.counts[h.kind] += 1
            self.entities.append({"type":h.kind,"start":h.start,"end":h.end,"location":location,"source":source,"score":round(h.score,3),"placeholder":placeholder})
        pieces.append(text[last:])
        clean = "".join(pieces)
        if include_text or not structured:
            observed=profile(text)
            self.languages.update(observed)
            if any(k.startswith('unsupported:') for k in observed):
                self.issue('unsupported_script','review','Unsupported language script observed; detection coverage incomplete',location,'Check this content with a detector or reviewer supporting its language.')
        self.originals.append({"location":location,"text":text,"source":source,"include_text":include_text})
        if include_text: self.sanitised.append({"location":location,"text":clean,"source":source})
        return clean, hits
