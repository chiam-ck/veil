"""Local exact-text corrections and retained findings for immutable revisions.

Rule values live only beside retained originals, never in public reports/SQLite.
"""
import json
import re
from collections import defaultdict
from .detection import Hit
from .llm import merge_hits, KINDS

RULE_KINDS = KINDS | {'MANUAL', 'SENSITIVE_DATA'}


def previous_rules(folder):
    """Support results created before private redaction snapshots were added."""
    snapshot = folder/'original'/'redaction-values.json'
    if snapshot.exists():
        return json.loads(snapshot.read_text())
    report = json.loads((folder/'output'/'report.json').read_text())
    segments = json.loads((folder/'original'/'extracted.json').read_text())
    by_location = defaultdict(set)
    for segment in segments:
        by_location[(segment['location'], segment['source'])].add(segment['text'])
    rules = {}
    for entity in report.get('entities', []):
        texts = by_location[(entity['location'], entity['source'])]
        if len(texts) != 1:
            raise ValueError('Retained inspection data is incomplete. Upload the original again.')
        text = next(iter(texts))[entity['start']:entity['end']]
        if not text or entity['type'] not in RULE_KINDS:
            raise ValueError('Retained inspection data is incomplete. Upload the original again.')
        rules[(entity['type'], text)] = {'kind': entity['type'], 'text': text}
    return list(rules.values())


class RevisionDetector:
    """Union mandatory earlier findings with manual phrases and optional inference."""
    def __init__(self, base, rules, rerun):
        self.base, self.rules, self.rerun = base, rules, rerun
        self.cache = {}

    def reset(self):
        self.cache.clear()
        self.base.reset()

    def prefetch(self, values):
        if self.rerun:self.base.prefetch(values)

    def find(self, text, structured=False, context=None):
        hits = list(self.base.find(text, structured=structured, context=context)) if self.rerun else []
        for rule in self.rules:
            # Phrases are literal, ignore case, and have no word boundary
            # assumption; this also covers Chinese and substrings of identifiers.
            for match in re.finditer(re.escape(rule['text']), text, re.I):
                hits.append(Hit(match.start(), match.end(), rule['kind'], 1.0))
        return merge_hits(hits)

    def describe(self):
        result = self.base.describe() if self.rerun else dict(mode='manual', calls=0, seconds=0.0)
        return {**result, 'retained_redactions': True,
                'manual_rule_count': sum(r['kind']=='MANUAL' for r in self.rules)}
