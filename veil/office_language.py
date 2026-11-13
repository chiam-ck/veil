"""Neighbouring table labels and local CJK fallback fonts, without rewriting Office files."""
from copy import deepcopy
from lxml import etree as E
from .languages import HAN, T2S

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
A='http://schemas.openxmlformats.org/drawingml/2006/main'
S='http://schemas.openxmlformats.org/spreadsheetml/2006/main'

def text(node):return ''.join(n.text or '' for n in node.iter() if isinstance(n.tag,str) and E.QName(n).localname=='t')

def field_label(value):
    import re
    label=T2S.convert(value.strip()).rstrip(':：')
    if re.fullmatch(r'(?:full name|customer name|name|contact person|phone|telephone|mobile|email|e-mail|address|postal address|mailing address|account(?: (?:id|number|no\.?))?|customer(?: (?:id|number|no\.?))?|client id|member id)',label,re.I):return label
    return label if re.fullmatch(r'(?:中文姓名|客户姓名|客户名称|收件人|联系人|联络人|姓名|通讯地址|通信地址|邮寄地址|收件地址|联系地址|联络地址|住宅地址|居住地址|地址|住址|客户编号|客户号码|客户号|账户编号|账户号码|账户号|账号|帐户编号|帐户号码|帐户号|帐号|会员编号|会员号)',label) else None

def contexts(root,xml):
    hints={}
    headers={}
    for row in root.xpath("//*[local-name()='tr']"):
        cells=[n for n in row if isinstance(n.tag,str) and E.QName(n).localname=='tc']
        labels=[field_label(text(c)) for c in cells]
        table=row.getparent()
        if sum(bool(x) for x in labels)>=2:headers[table]=labels
        elif table in headers:
            for label,cell in zip(headers[table],cells):
                if label:
                    for group in cell.xpath(".//*[local-name()='p']"):hints[group]=label
        for prev,cell in zip(cells,cells[1:]):
            label=field_label(text(prev))
            if label:
                for group in cell.xpath(".//*[local-name()='p']"):hints[group]=label
    shared=xml.get('xl/sharedStrings.xml')
    strings=[text(n) for n in shared] if shared is not None else []
    def value(cell):
        if cell.get('t')=='s':
            v=cell.find('{'+S+'}v')
            try:return strings[int(v.text)]
            except (TypeError,ValueError,IndexError,AttributeError):return ''
        return text(cell)
    column_labels={}
    for row in root.xpath("//*[local-name()='row']"):
        cells=[n for n in row if E.QName(n).localname=='c']
        labels={cell.get('r','').rstrip('0123456789'):field_label(value(cell)) for cell in cells}
        if sum(bool(v) for v in labels.values())>=2:column_labels=labels;continue
        assignments={cell:column_labels.get(cell.get('r','').rstrip('0123456789')) for cell in cells}
        for prev,cell in zip(cells,cells[1:]):
            label=field_label(value(prev))
            if label:assignments[cell]=label
        for cell,label in assignments.items():
            if not label:continue
            hints[cell]=label
            if cell.get('t')=='s':
                v=value(cell)
                for child in list(cell):cell.remove(child)
                cell.set('t','inlineStr');is_=E.SubElement(cell,'{'+S+'}is');E.SubElement(is_,'{'+S+'}t').text=v
                hints[is_]=label
            else:
                for group in cell.findall('{'+S+'}is'):hints[group]=label
    return hints

def fonts(root):
    for run in root.xpath("//*[local-name()='r']"):
        value=text(run)
        if not HAN.search(value):continue
        font='Noto Sans CJK TC' if T2S.convert(value)!=value else 'Noto Sans CJK SC'
        ns=E.QName(run).namespace
        if ns not in {W,A}:continue
        prop=run.find('{'+ns+'}rPr')
        if prop is None:prop=E.Element('{'+ns+'}rPr');run.insert(0,prop)
        tag='rFonts' if ns==W else 'ea'
        f=prop.find('{'+ns+'}'+tag)
        if f is None:f=E.SubElement(prop,'{'+ns+'}'+tag)
        f.set('{'+W+'}eastAsia' if ns==W else 'typeface',font)

def spreadsheet_fonts(xml):
    styles=xml.get('xl/styles.xml')
    if styles is None:return
    fonts_=styles.find('{'+S+'}fonts');xfs=styles.find('{'+S+'}cellXfs')
    if fonts_ is None or xfs is None:return
    shared=xml.get('xl/sharedStrings.xml');strings=[text(n) for n in shared] if shared is not None else []
    cache={}
    for path,root in xml.items():
        if not path.startswith('xl/worksheets/'):continue
        for cell in root.iter('{'+S+'}c'):
            value=text(cell)
            if cell.get('t')=='s':
                try:value=strings[int(cell.find('{'+S+'}v').text)]
                except (ValueError,IndexError,AttributeError):continue
            if not HAN.search(value):continue
            original=int(cell.get('s','0'));font='Noto Sans CJK TC' if T2S.convert(value)!=value else 'Noto Sans CJK SC'
            key=(original,font)
            if key not in cache:
                style=deepcopy(xfs[original]);f=deepcopy(fonts_[int(style.get('fontId','0'))]);n=f.find('{'+S+'}name')
                if n is None:n=E.SubElement(f,'{'+S+'}name')
                n.set('val',font);style.set('fontId',str(len(fonts_)));style.set('applyFont','1');fonts_.append(f)
                cache[key]=len(xfs);xfs.append(style)
            cell.set('s',str(cache[key]))
    fonts_.set('count',str(len(fonts_)));xfs.set('count',str(len(xfs)))


def prune_shared_strings(xml,session):
    shared=xml.get('xl/sharedStrings.xml')
    if shared is None:return
    references=[]
    for path,root in xml.items():
        if not path.startswith('xl/worksheets/'):continue
        for cell in root.iter('{'+S+'}c'):
            if cell.get('t')=='s':
                v=cell.find('{'+S+'}v')
                if v is not None:references.append(v)
    used=sorted({int(v.text) for v in references});entries=list(shared)
    if any(i<0 or i>=len(entries) for i in used):raise ValueError('Invalid shared string reference')
    mapping={old:new for new,old in enumerate(used)}
    for child in entries:shared.remove(child)
    for old in used:shared.append(entries[old])
    for v in references:v.text=str(mapping[int(v.text)])
    shared.set('count',str(len(references)));shared.set('uniqueCount',str(len(used)))
    if len(entries)>len(used):session.removals['unused spreadsheet shared strings discarded']+=len(entries)-len(used)
