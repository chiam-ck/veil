"""Preserve supported Office structures, edit PII in place, never evaluate formulas.

Formula support is deliberately explicit. Unknown text reconstruction, external
sources and dynamic formula constructs block output rather than being deleted.
"""
import os
import re
import shlex
from urllib.parse import unquote, urlsplit
from lxml import etree as E
from openpyxl.formula import Tokenizer
from openpyxl.formula.translate import Translator
from openpyxl.utils.cell import range_boundaries, coordinate_to_tuple

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
S='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'

class PreservationError(Exception):pass

def tag(node):return E.QName(node).localname if isinstance(node.tag,str) else ''

def revision_views(leaves):
    views=[]
    for excluded in ({'del','moveFrom'},{'ins','moveTo'}):
        view=[n for n in leaves if not any(tag(p) in excluded for p in n.iterancestors())]
        if view and view not in views:views.append(view)
    return views

def replace_group(leaves,session,location,context=None):
    """Analyze before writing. Both revision views contribute edits to shared runs."""
    views=revision_views(leaves)
    edits={}
    for v,view in enumerate(views):
        original=''.join(n.text or '' for n in view)
        _,hits=session.redact(original,location+f':view:{v}',context=context)
        offset=0
        for leaf in view:
            end=offset+len(leaf.text or '')
            for h in hits:
                if h.start<end and h.end>offset:
                    placeholder=session.mapping[(h.kind,original[h.start:h.end].casefold())] if offset<=h.start<end else ''
                    edits.setdefault(leaf,[]).append((max(0,h.start-offset),min(end-offset,h.end-offset),placeholder))
            offset=end
    for leaf,operations in edits.items():
        merged=[]
        for a,b,p in sorted(set(operations)):
            if merged and a<merged[-1][1]:
                x,y,q=merged[-1]
                if p and q and p!=q:
                    session.issue('revision_identity_conflict','review','Tracked revision views share a PII span with different identities; review identifiers',location,'Compare original and final revision views for consistent replacement identifiers.')
                merged[-1]=(x,max(y,b),q or p)
            else:merged.append((a,b,p))
        raw=leaf.text;chunks=[];last=0
        for a,b,p in merged:chunks.extend([raw[last:a],p]);last=b
        chunks.append(raw[last:]);leaf.text=''.join(chunks)
        leaf.set('{http://www.w3.org/XML/1998/namespace}space','preserve')

# No data-bound, executable, environment/user or external fields are admitted.
SAFE_FIELDS={'PAGE','NUMPAGES','SECTION','SECTIONPAGES','DATE','TIME','CREATEDATE','SAVEDATE','PRINTDATE'}
def inspect_fields(root,session):
    def check(code):
        try:parts=shlex.split(code,posix=False)
        except ValueError:raise PreservationError('Malformed Word field instruction') from None
        if not parts or parts[0].upper() not in SAFE_FIELDS:
            raise PreservationError('Unsupported Word field cannot be safely preserved; no field instructions were deleted')
        rest=parts[1:]
        while rest:
            if len(rest)<2 or rest[0] not in {r'\*',r'\@',r'\#'}:
                raise PreservationError('Unsupported Word field formatting; intact processing blocked')
            value=rest[1]
            if rest[0]==r'\*' and not re.fullmatch(r'[A-Za-z]+',value):
                raise PreservationError('Unsupported Word field number format')
            if session.detector.find(value):
                raise PreservationError('Sensitive Word field formatting cannot be preserved safely')
            rest=rest[2:]
    for node in root.iter('{'+W+'}fldSimple'):check(node.get('{'+W+'}instr',''))
    stack=[];claimed=set()
    for node in root.iter():
        if tag(node)=='fldChar':
            kind=node.get('{'+W+'}fldCharType')
            if kind=='begin':stack.append([])
            elif kind=='separate' and stack:
                check(''.join(n.text or '' for n in stack[-1]));claimed.update(stack[-1]);stack[-1]=[]
            elif kind=='end' and stack:
                code=stack.pop()
                if code:check(''.join(n.text or '' for n in code));claimed.update(code)
        elif tag(node) in {'instrText','delInstrText'} and stack:stack[-1].append(node)
    if stack:raise PreservationError('Unbalanced Word fields cannot be safely preserved')
    for node in root.iter():
        if tag(node) in {'instrText','delInstrText'} and node not in claimed:check(node.text or '')


def metadata_context(node,key=None):
    relevant=key in {'author','lastModifiedBy','displayName'} or (key=='name' and tag(node) in {'cmAuthor','person','author'}) or (key is None and tag(node) in {'creator','lastModifiedBy','author'})
    if not relevant:return None
    value=(node.get(key,'') if key else node.text or '').strip()
    if re.search(r'(?i)\b(team|department|office|system|service|robot|user|synthetic)\b|团队|團隊|财务部|財務部',value):return ''
    if re.search(r'[\u3400-\u9fff]',value):return '姓名'
    return 'Name' if len(value.split())>=2 else ''


def inspect_relationships(xml,session,part_ids):
    for name,root in xml.items():
        if not name.endswith('.rels'):continue
        for i,rel in enumerate(root):
            if any(k in rel.get('Type','').lower() for k in ('vbaproject','oleobject','activex','control')):
                raise PreservationError('Executable or embedded relationship cannot be safely preserved')
            if rel.get('TargetMode')!='External':continue
            if not rel.get('Type','').endswith('/hyperlink'):
                raise PreservationError('External linked content cannot be inspected locally while preserving it')
            target=rel.get('Target','');decoded=unquote(target)
            if urlsplit(decoded).scheme.lower() not in {'http','https','mailto'}:
                raise PreservationError('Unsupported external hyperlink protocol; intact processing blocked')
            _,hits=session.redact(decoded,f'{part_ids[name]}:hyperlink:{i}',structured=True,include_text=False)
            if hits:
                rel.set('Target','about:blank')
                session.changes['PII hyperlink targets disabled']+=1
                session.adjustments.append({'location':f'{part_ids[name]}:hyperlink:{i}','action':'pii_hyperlink_target_disabled'})

# Numeric/selection operations with inspected literals and local references.
# No CHAR, text concatenation, INDIRECT/OFFSET, external lookup, UDF or macros.
SAFE_FUNCTIONS=set('SUM AVERAGE MIN MAX COUNT COUNTA COUNTBLANK COUNTIF COUNTIFS SUMIF SUMIFS AVERAGEIF AVERAGEIFS IF IFERROR IFNA AND OR NOT TRUE FALSE ABS SIGN ROUND ROUNDUP ROUNDDOWN INT TRUNC MOD SQRT POWER CEILING FLOOR SUMPRODUCT PRODUCT MEDIAN STDEV STDEV.S STDEV.P VAR VAR.S VAR.P SUBTOTAL AGGREGATE'.split())

class SpreadsheetPreserver:
    def __init__(self,xml,session,part_ids,contexts):
        self.xml,self.s,self.ids=xml,session,part_ids
        self.sheets={};self.sheet_names={};self.formulas=[];self.before={};self.names={};self.checks=0;self.needs_recalc=False
        workbook=xml.get('xl/workbook.xml')
        if workbook is None:return
        rels=xml.get('xl/_rels/workbook.xml.rels')
        targets={r.get('Id'):os.path.normpath('xl/'+r.get('Target','')) if not r.get('Target','').startswith('/') else r.get('Target').lstrip('/') for r in rels} if rels is not None else {}
        if workbook.find('{'+S+'}externalReferences') is not None:
            raise PreservationError('External workbook references cannot be inspected without fetching data')
        for sh in workbook.iter('{'+S+'}sheet'):
            path=targets.get(sh.get('{'+R+'}id'))
            if path not in xml:raise PreservationError('Unresolved worksheet relationship')
            title=sh.get('name','')
            if session.detector.find(title):
                raise PreservationError('PII in worksheet identifiers requires coordinated renaming; intact processing blocked')
            self.sheets[title]=path;self.sheet_names[path]=title
        for index,node in enumerate(workbook.iter('{'+S+'}definedName')):
            identifier=node.get('name','')
            if session.detector.find(identifier,structured=True):
                raise PreservationError('Sensitive defined-name identifiers cannot be safely renamed')
            scope=node.get('localSheetId');key=(int(scope) if scope is not None else None,identifier.casefold())
            self.names[key]=node
            self.inspect_formula(node.text or '',f'part:workbook:defined-name:{index}',None,redact=False)
        for key,node in self.names.items():
            scope=key[0] if key[0] is not None else 0
            try:self.references(Tokenizer('='+node.text).items,list(self.sheet_names)[scope])
            except IndexError:raise PreservationError('Invalid defined-name worksheet scope') from None
        for path in self.sheet_names:
            root=xml[path];hints=contexts(root,xml);anchors={}
            for cell in root.iter('{'+S+'}c'):
                self.before[(path,cell.get('r'))]=self.cell_value(cell)
                f=cell.find('{'+S+'}f')
                if f is not None and f.get('t')=='shared' and f.text:anchors[f.get('si')]=(cell.get('r'),f.text)
            for cell in root.iter('{'+S+'}c'):
                coord=cell.get('r','')
                if not re.fullmatch(r'[A-Z]{1,3}[1-9]\d{0,6}',coord):raise PreservationError('Invalid worksheet cell reference')
                f=cell.find('{'+S+'}f');v=cell.find('{'+S+'}v');context=hints.get(cell)
                loc=f'{part_ids[path]}:cell:{coord}'
                if f is None:
                    if v is not None and cell.get('t')!='s' and v.text:
                        clean,hits=session.redact(v.text,loc,structured=cell.get('t') not in {'str'} and not context,context=context)
                        if hits:
                            cell.remove(v);cell.set('t','inlineStr');is_=E.SubElement(cell,'{'+S+'}is');E.SubElement(is_,'{'+S+'}t').text=clean
                    continue
                if len(self.formulas)>=5000:raise PreservationError('Formula inspection limit exceeded')
                if f.get('t','normal') not in {'normal','shared'}:
                    raise PreservationError('Array, spill or data-table formulas are not yet supported for intact redaction')
                expr=f.text
                if not expr:
                    if f.get('t')!='shared' or f.get('si') not in anchors:raise PreservationError('Unresolved shared formula')
                    anchor,master=anchors[f.get('si')]
                    try:expr=Translator('='+master,origin=anchor).translate_formula(coord)[1:]
                    except Exception:raise PreservationError('Invalid shared formula references') from None
                _,tokens=self.inspect_formula(expr,loc,context,redact=False)
                if v is None or not v.text:session.issue('existing_formula_cache_missing','info','Source formula has no cached result; formula retained without evaluation',loc,'Open in a trusted spreadsheet application if calculated values are needed. Veil did not create this missing cache.')
                self.formulas.append((path,coord,cell,tokens,expr,context))
        # Validate references now, including names. No spreadsheet evaluation occurs.
        for path,coord,cell,tokens,expr,context in self.formulas:self.references(tokens,path)

    def cell_value(self,cell):
        f=cell.find('{'+S+'}f');v=cell.find('{'+S+'}v')
        if f is not None:return ('formula',f.text,tuple(sorted(f.attrib.items())),cell.get('t'),v.text if v is not None else None)
        if cell.get('t')=='s':
            try:
                shared=self.xml['xl/sharedStrings.xml'][int(v.text)]
                return ('text',''.join(n.text or '' for n in shared.iter('{'+S+'}t')))
            except (KeyError,ValueError,IndexError,AttributeError):raise PreservationError('Invalid shared string reference') from None
        if cell.get('t') in {'inlineStr','str'}:
            return ('text',''.join(n.text or '' for n in cell.iter('{'+S+'}t')) if cell.get('t')=='inlineStr' else v.text if v is not None else '')
        return (cell.get('t','n'),v.text if v is not None else '')

    def inspect_formula(self,expr,location,context,redact=True):
        if len(expr)>8192:raise PreservationError('Formula length limit exceeded')
        try:tokens=Tokenizer('='+expr).items
        except Exception:raise PreservationError('Formula syntax could not be inspected') from None
        if len(tokens)>512:raise PreservationError('Formula token limit exceeded')
        depth=0;text_tokens=[];has_changes=False
        for i,t in enumerate(tokens):
            if t.type in {'ARRAY','LITERAL'}:raise PreservationError('Unsupported formula expression; no formula was deleted')
            if t.type=='FUNC' and t.subtype=='OPEN':
                if t.value[:-1].upper() not in SAFE_FUNCTIONS:raise PreservationError('Unsupported formula function could reconstruct or fetch PII; intact processing blocked')
                depth+=1
            elif t.type=='PAREN' and t.subtype=='OPEN':depth+=1
            elif t.type in {'FUNC','PAREN'} and t.subtype=='CLOSE':depth-=1
            if depth<0:raise PreservationError('Unbalanced formula syntax')
            if t.type=='OPERATOR-INFIX' and t.value=='&':raise PreservationError('Text-concatenating formulas are not yet supported for intact redaction')
            if t.type.startswith('OPERATOR') and t.value not in {'+','-','*','/','^','%','=','<>','<','>','<=','>='}:
                raise PreservationError('Unsupported formula operator')
            if redact and t.type=='OPERAND' and t.subtype=='TEXT':
                raw=t.value[1:-1].replace('""','"')
                clean,hits=self.s.redact(raw,location+f':literal:{i}',context=context,known_values=True)
                if hits:t.value='"'+clean.replace('"','""')+'"';has_changes=True
                text_tokens.append(t.value)
        if depth:raise PreservationError('Unbalanced formula syntax')
        if not has_changes:return expr,tokens
        # Locate quoted literals without normalising formula whitespace. Apostrophe-
        # quoted worksheet identifiers can themselves contain double quotes.
        spans=[];i=0
        while i<len(expr):
            if expr[i] in {"'",'"'}:
                quote=expr[i];a=i;i+=1
                while i<len(expr):
                    if expr[i]==quote:
                        if i+1<len(expr) and expr[i+1]==quote:i+=2;continue
                        i+=1;break
                    i+=1
                if quote=='"':spans.append((a,i))
            else:i+=1
        if len(spans)!=len(text_tokens):raise PreservationError('Formula literals could not be mapped without changing its syntax')
        result=[];last=0
        for (a,b),literal in zip(spans,text_tokens):result.extend([expr[last:a],literal]);last=b
        result.append(expr[last:])
        return ''.join(result),tokens

    def references(self,tokens,path,seen=()):
        refs=[]
        for token in tokens:
            if token.type!='OPERAND' or token.subtype!='RANGE':continue
            value=token.value
            if any(x in value for x in ('[',']','|',':!')):raise PreservationError('External, structured or dynamic formula references are not yet supported')
            target=path
            if '!' in value:
                title,value=value.rsplit('!',1);title=title.strip("'").replace("''", "'")
                if title not in self.sheets:raise PreservationError('Unresolved formula worksheet reference')
                target=self.sheets[title]
            try:
                bounds=range_boundaries(value)
                if not any(x is not None for x in bounds):raise ValueError()
                lo_x,lo_y,hi_x,hi_y=bounds
                bounds=(lo_x or 1,lo_y or 1,hi_x or 16384,hi_y or 1048576)
                if not (1<=bounds[0]<=bounds[2]<=16384 and 1<=bounds[1]<=bounds[3]<=1048576):raise ValueError()
                refs.append((target,bounds));continue
            except ValueError:pass
            index=list(self.sheet_names).index(target)
            key=(index,value.casefold()) if (index,value.casefold()) in self.names else (None,value.casefold())
            if key not in self.names or key in seen or len(seen)>32:raise PreservationError('Unresolved or cyclic defined-name formula reference')
            try:named=Tokenizer('='+self.names[key].text).items
            except Exception:raise PreservationError('Invalid defined-name expression') from None
            refs.extend(self.references(named,target,seen+(key,)));refs.append((self.names[key],(1,1,1,1)))
        return refs

    def affected(self,refs,dirty):
        for source,bounds in refs:
            for other,ref in dirty:
                if other!=source:continue
                self.checks+=1
                if self.checks>2_000_000:raise PreservationError('Formula dependency inspection limit exceeded')
                y,x=coordinate_to_tuple(ref)
                if bounds[0]<=x<=bounds[2] and bounds[1]<=y<=bounds[3]:return True
        return False

    def clear_cache(self,path,coord,cell,action):
        v=cell.find('{'+S+'}v')
        if v is not None:
            cell.remove(v);self.s.changes[action]+=1
            self.s.adjustments.append({'location':f'{self.ids[path]}:cell:{coord}','action':'formula_cache_invalidated','reason':action,'formula_retained':True})
        self.needs_recalc=True
        self.s.issue('formula_recalculation','review','Redacted inputs affect formula results; dependent caches cleared, formulas retained for recalculation/review',f'{self.ids[path]}:cell:{coord}','Recalculate in a trusted spreadsheet application and check this cell before relying on its result.')
        location=f'{self.ids[path]}:cell:{coord}:cached'
        for segment in self.s.sanitised:
            if segment['location']==location:segment['text']='[FORMULA_RESULT_PENDING_RECALCULATION]'

    def finish(self):
        if not self.formulas and not self.names:return
        dirty=set()
        # Seed PII found in any formula literal/cache before rewriting any formula.
        # This also covers a bare literal whose name is recognised only with a label
        # elsewhere in the document, without mutating calculation syntax.
        for path,coord,cell,tokens,expr,context in self.formulas:
            for t in tokens:
                if t.type=='OPERAND' and t.subtype=='TEXT':
                    raw=t.value[1:-1].replace('""','"')
                    for hit in self.s.detector.find(raw,context=context):self.s.placeholder(hit.kind,raw[hit.start:hit.end])
            v=cell.find('{'+S+'}v')
            if v is not None and v.text:
                for hit in self.s.detector.find(v.text,structured=cell.get('t')!='str' and not context,context=context):self.s.placeholder(hit.kind,v.text[hit.start:hit.end])
        for index,node in enumerate(self.names.values()):
            for t in Tokenizer('='+node.text).items:
                if t.type=='OPERAND' and t.subtype=='TEXT':
                    raw=t.value[1:-1].replace('""','"')
                    for hit in self.s.detector.find(raw):self.s.placeholder(hit.kind,raw[hit.start:hit.end])
        for index,node in enumerate(self.names.values()):
            original=node.text;node.text,_=self.inspect_formula(original,f'part:workbook:defined-name:{index}',None)
            if node.text!=original:
                dirty.add((node,'A1'));self.s.changes['PII defined-name literals redacted; name retained']+=1
                self.s.adjustments.append({'location':f'part:workbook:defined-name:{index}','action':'defined_name_literal_redacted'})
        for path in self.sheet_names:
            for cell in self.xml[path].iter('{'+S+'}c'):
                key=(path,cell.get('r'))
                if self.cell_value(cell)!=self.before.get(key):dirty.add(key)
        for path,coord,cell,tokens,expr,context in self.formulas:
            loc=f'{self.ids[path]}:cell:{coord}';f=cell.find('{'+S+'}f');v=cell.find('{'+S+'}v')
            clean,_=self.inspect_formula(expr,loc,context)
            if clean!=expr and f.text:
                f.text=clean;self.s.changes['PII formula literals redacted; formula retained']+=1
                self.s.adjustments.append({'location':loc,'action':'formula_literals_redacted','formula_retained':True})
            literal=len([t for t in tokens if t.type!='WHITE-SPACE'])==1 and any(t.subtype=='TEXT' for t in tokens)
            if v is not None and v.text:
                cached,hits=self.s.redact(v.text,loc+':cached',structured=cell.get('t')!='str' and not context,context=context,known_values=True)
                if hits:
                    if literal:v.text=cached;cell.set('t','str')
                    elif clean!=expr or all(t.type in {'OPERAND','WHITE-SPACE'} and t.subtype in {'RANGE',''} for t in tokens) or self.affected(self.references(tokens,path),dirty):
                        self.clear_cache(path,coord,cell,'PII formula caches cleared; formula retained')
                    else:raise PreservationError('Sensitive formula result cannot be redacted while preserving its calculation')
                    self.s.changes['PII formula caches sanitised']+=1
                    self.s.adjustments.append({'location':loc,'action':'formula_cache_sanitised','formula_retained':True})
            if clean!=expr and not literal:
                self.clear_cache(path,coord,cell,'Formula caches invalidated after PII argument redaction')
        for path in self.sheet_names:
            for cell in self.xml[path].iter('{'+S+'}c'):
                key=(path,cell.get('r'))
                if self.cell_value(cell)!=self.before.get(key):dirty.add(key)
        pending=[(p,r,c,self.references(t,p)) for p,r,c,t,expr,context in self.formulas]
        while pending:
            next_pending=[];new=False
            for path,coord,cell,refs in pending:
                if self.affected(refs,dirty):
                    self.clear_cache(path,coord,cell,'Dependent formula caches invalidated; formula retained')
                    dirty.add((path,coord));new=True
                else:next_pending.append((path,coord,cell,refs))
            if not new:break
            pending=next_pending
        if self.needs_recalc:
            workbook=self.xml['xl/workbook.xml'];calc=workbook.find('{'+S+'}calcPr')
            if calc is None:calc=E.SubElement(workbook,'{'+S+'}calcPr')
            calc.set('fullCalcOnLoad','1');calc.set('forceFullCalc','1')
