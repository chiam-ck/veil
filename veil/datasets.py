"""Forty synthetic documents, deterministic source groups and held-out split."""
import hashlib
import io
import json
import random
import zipfile
from pathlib import Path
from lxml import etree as E
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
import pymupdf as fitz
from docx import Document
from docx.shared import Inches
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from pptx import Presentation
from pptx.util import Inches as PInches, Pt
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.drawing.image import Image as XLImage

FONT='/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
CJK='/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
NAMES=['Amelia Quill','Jasper Linden','Mei Fern','Noah Vale','Priya Elm','Ethan Wren','Clara Moss','Oliver Finch','Zoe Willow','Lucas Reed']

def record(i,negative=False):
    if negative: return [('REFERENCE','SYNTHETIC TEST MATERIAL'),('REFERENCE','Invoice total 123.45; quantity 42; year 2026; reference 1234567; part AB123456Z')]
    name=NAMES[i]
    rows=[('PERSON',name),('EMAIL_ADDRESS',f'person{i:02d}@example.test'),('PHONE_NUMBER',f'+65 8{i:03d} {1200+i:04d}'),('NRIC_FIN',f'S{i+1000000:07d}Z'),('ADDRESS',f'{i+10} Fictional Crescent, Singapore {100000+i:06d}'),('ACCOUNT_ID',f'VEIL-{i:04d}-TEST')]
    return rows

def lines_for(rows,i):
    if len(rows)==2:return [v for _,v in rows]
    values=dict(rows)
    # Include unlabelled prose and labels with synonyms across sources.
    if i%2:
        return [f'{values["PERSON"]} requested a copy of the quarterly report.',f'Reply to {values["EMAIL_ADDRESS"]}.',f'Contact: {values["PHONE_NUMBER"]}',f'Identity reference {values["NRIC_FIN"]}',f'Mailing address: {values["ADDRESS"]}',f'Client ID: {values["ACCOUNT_ID"]}']
    return [f'Name: {values["PERSON"]}',f'Email: {values["EMAIL_ADDRESS"]}',f'Phone: {values["PHONE_NUMBER"]}',f'NRIC: {values["NRIC_FIN"]}',f'Address: {values["ADDRESS"]}',f'Account number: {values["ACCOUNT_ID"]}']

def picture(lines,variant=0):
    font=ImageFont.truetype(FONT,27 if variant!=8 else 13)
    image=Image.new('RGB',(1200,700),'white');d=ImageDraw.Draw(image)
    if variant==2:
        d.rectangle((15,65,1185,670),outline='#809e9e',width=3);d.rectangle((15,65,1185,95),fill='#d7e8e2')
    d.text((45,24),'SYNTHETIC TEST MATERIAL',font=ImageFont.truetype(FONT,23),fill='#4a6460')
    for n,line in enumerate(lines):
        selected=ImageFont.truetype(CJK,22) if any('\u3400'<=c<='\u9fff' for c in line) else font
        d.text((45,100+n*70),line,font=selected,fill='black')
    if variant==4: image=image.rotate(90,expand=True,fillcolor='white')
    if variant==5:
        # A shallow projective warp simulates a photographed form, followed by blur.
        image=image.transform(image.size,Image.Transform.PERSPECTIVE,(1,.025,-8,.015,1,-5,.00002,.000015),resample=Image.Resampling.BICUBIC,fillcolor='#e5e2dc')
        image=image.filter(ImageFilter.GaussianBlur(1.6))
    if variant==6: image=ImageEnhance.Contrast(image).enhance(.22)
    return image

def entities_for(lines,rows,loc,source='native',page=1):
    result=[]
    for kind,value in rows:
        if kind=='REFERENCE':continue
        for n,line in enumerate(lines):
            start=line.find(value)
            if start>=0:result.append({'type':kind,'value':value,'location_hint':loc,'line':n,'start_in_line':start,'end_in_line':start+len(value),'source':source,'page':page})
    return result

def patch_docx(path):
    with zipfile.ZipFile(path) as z: parts={n:z.read(n) for n in z.namelist()}
    root=E.fromstring(parts['word/document.xml']);body=root.find(qn('w:body'))
    p=OxmlElement('w:p');dele=OxmlElement('w:del');dele.set(qn('w:author'),'Synthetic Author');r=OxmlElement('w:r');t=OxmlElement('w:delText');t.text='deleted@example.test';r.append(t);dele.append(r);p.append(dele);body.insert(0,p)
    ins_p=OxmlElement('w:p');ins=OxmlElement('w:ins');ins.set(qn('w:author'),'Synthetic Author');run=OxmlElement('w:r');txt=OxmlElement('w:t');txt.text='inserted@example.test';run.append(txt);ins.append(run);ins_p.append(ins);body.insert(1,ins_p)
    cp=OxmlElement('w:p');cr=OxmlElement('w:r');ref=OxmlElement('w:commentReference');ref.set(qn('w:id'),'0');cr.append(ref);cp.append(cr);body.insert(2,cp)
    parts['word/comments.xml']=b'<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:comment w:id="0" w:author="Synthetic"><w:p><w:r><w:t>docx-comment@example.test</w:t></w:r></w:p></w:comment></w:comments>'
    rels=E.fromstring(parts['word/_rels/document.xml.rels']);rel=E.SubElement(rels,'{http://schemas.openxmlformats.org/package/2006/relationships}Relationship');rel.set('Id','rIdSyntheticComment');rel.set('Type','http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments');rel.set('Target','comments.xml');parts['word/_rels/document.xml.rels']=E.tostring(rels)
    types=E.fromstring(parts['[Content_Types].xml']);ov=E.SubElement(types,'{http://schemas.openxmlformats.org/package/2006/content-types}Override');ov.set('PartName','/word/comments.xml');ov.set('ContentType','application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml');parts['[Content_Types].xml']=E.tostring(types)
    parts['word/document.xml']=E.tostring(root)
    # Orphaned XML tests final full-package inspection, not just relationship traversal.
    parts['word/orphan.xml']=b'<root><text>orphan@example.test</text></root>'
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        for n,d in parts.items():z.writestr(n,d)

def generate(output,seed=20261111):
    output=Path(output)
    if output.exists() and any(output.iterdir()):raise FileExistsError('Dataset output must be empty; choose a new directory')
    output.mkdir(parents=True,exist_ok=True);rng=random.Random(seed)
    manifest={'seed':seed,'synthetic':True,'version':3,'profile':'veil-preserve-content-v4','matching':'Exact entity type and complete normalized value at an extracted segment; partial overlap is a miss; removed-policy-only entities evaluated separately','documents':[]}
    for i in range(10):
        subset='heldout' if i in {3,6,9} else 'development'
        folder=output/subset;folder.mkdir(exist_ok=True)
        rows=record(i,i==9);lines=lines_for(rows,i)
        if i==7:
            lines+=['中文姓名：林小明；地址：虚构路88号；English baseline limitation sample']
            rows += [('PERSON','林小明'),('ADDRESS','虚构路88号')]
        image=picture(lines,i);buf=io.BytesIO();image.save(buf,format='PNG');image_bytes=buf.getvalue()
        for fmt in ('docx','pptx','xlsx','pdf'):
            path=folder/f'synthetic-{i:02d}.{fmt}'
            image_heavy=i in {2,4,5,6,8};mixed=i==7
            pages=50 if i==8 else 10 if i==1 else 1
            kind_title=['Customer letter','Invoice','Customer record screenshot','Quarterly report','Rotated customer form','Photographed form simulation','Low contrast scan','Bilingual report','Small print register','Negative control'][i]
            expected=[];removed=['metadata']
            if fmt=='docx':
                doc=Document();doc.add_heading('Synthetic test material',0);doc.add_paragraph(kind_title)
                doc.core_properties.author='synthetic-author@example.test'
                for page in range(pages):
                    if page:doc.add_page_break()
                    if image_heavy or mixed:doc.add_picture(io.BytesIO(image_bytes),width=Inches(6));expected+=entities_for(lines,rows,'word/media',source='ocr',page=page+1)
                    if not image_heavy:
                        for line in lines:
                            p=doc.add_paragraph();cut=max(1,len(line)//2);p.add_run(line[:cut]).bold=True;p.add_run(line[cut:])
                        expected+=entities_for(lines,rows,'word/document',page=page+1)
                if i==0:
                    doc.sections[0].header.paragraphs[0].text='Header: header@example.test';doc.sections[0].footer.paragraphs[0].text='Footer: footer@example.test'
                    tab=doc.add_table(rows=1,cols=2);tab.cell(0,0).text='Email';tab.cell(0,1).text='table@example.test'
                    for v,h in [('header@example.test','header'),('footer@example.test','footer'),('table@example.test','table'),('orphan@example.test','orphan')]:expected.append({'type':'EMAIL_ADDRESS','value':v,'source':'native','location_hint':h})
                    expected += [{'type':'EMAIL_ADDRESS','value':v,'source':'native','location_hint':'preserved revision/comment'} for v in ['deleted@example.test','docx-comment@example.test']]
                    expected.append({'type':'EMAIL_ADDRESS','value':'inserted@example.test','source':'native','location_hint':'accepted tracked insertion'})
                doc.save(path)
                if i==0:patch_docx(path)
            elif fmt=='pptx':
                prs=Presentation();prs.slide_width=PInches(13.33);prs.slide_height=PInches(7.5)
                for page in range(pages):
                    slide=prs.slides.add_slide(prs.slide_layouts[6]);title=slide.shapes.add_textbox(PInches(.4),PInches(.2),PInches(12),PInches(.6));title.text='SYNTHETIC TEST MATERIAL · '+kind_title
                    if image_heavy or mixed:slide.shapes.add_picture(io.BytesIO(image_bytes),PInches(.3),PInches(1),height=PInches(6));expected+=entities_for(lines,rows,'ppt/media',source='ocr',page=page+1)
                    if not image_heavy:
                        box=slide.shapes.add_textbox(PInches(.5),PInches(1),PInches(12),PInches(6));tf=box.text_frame
                        for n,line in enumerate(lines):
                            p=tf.paragraphs[0] if n==0 else tf.add_paragraph();p.font.size=Pt(20);cut=len(line)//2;r=p.add_run();r.text=line[:cut];r.font.bold=True;p.add_run().text=line[cut:]
                        expected+=entities_for(lines,rows,'ppt/slides',page=page+1)
                if i==0:
                    slide.element.set('show','0');slide.notes_slide.notes_text_frame.text='Notes: notes@example.test';expected.append({'type':'EMAIL_ADDRESS','value':'notes@example.test','source':'native','location_hint':'notes'})
                    table=slide.shapes.add_table(1,2,PInches(.5),PInches(6.5),PInches(8),PInches(.5)).table;table.cell(0,0).text='Contact';table.cell(0,1).text='table@example.test';expected.append({'type':'EMAIL_ADDRESS','value':'table@example.test','source':'native','location_hint':'table'})
                prs.core_properties.author='synthetic-author@example.test';prs.save(path)
            elif fmt=='xlsx':
                wb=Workbook();ws=wb.active;ws.title='Synthetic'
                ws.append(['SYNTHETIC TEST MATERIAL']);ws.append([kind_title]);
                for page in range(pages):
                    if image_heavy or mixed:
                        img=XLImage(io.BytesIO(image_bytes));img.width=800;img.height=460;ws.add_image(img,f'D{page*28+2}');expected+=entities_for(lines,rows,'xl/media',source='ocr',page=page+1)
                    if not image_heavy:
                        for line in lines:ws.append([line])
                        expected+=entities_for(lines,rows,'xl/worksheets',page=page+1)
                if i==0:
                    hidden=wb.create_sheet('Hidden');hidden.sheet_state='hidden';hidden['A1']='hidden@example.test';ws.row_dimensions[2].hidden=True;ws.column_dimensions['A'].hidden=True
                    expected.append({'type':'EMAIL_ADDRESS','value':'hidden@example.test','source':'native','location_hint':'hidden sheet'})
                    ws['B2']='="formula@example.test"';ws['B3']='=A2';ws['B4']='=SUM(1,2)';ws['B5'].comment=Comment('comment@example.test','Synthetic');expected += [{'type':'EMAIL_ADDRESS','value':v,'source':'native','location_hint':'preserved formula/comment'} for v in ['formula@example.test','comment@example.test']]
                ws.column_dimensions['A'].width=90;wb.properties.creator='synthetic-author@example.test';wb.save(path)
            else:
                pdf=fitz.open()
                for page in range(pages):
                    p=pdf.new_page(width=600,height=800)
                    if image_heavy or mixed:p.insert_image(fitz.Rect(0,60,600,460),stream=image_bytes);expected+=entities_for(lines,rows,'render',source='ocr',page=page+1)
                    if not image_heavy:
                        p.insert_text((30,30),'SYNTHETIC TEST MATERIAL',fontsize=14)
                        for n,line in enumerate(lines):p.insert_text((30,(500 if mixed else 100)+n*35),line,fontsize=11,fontname='china-s' if any('\u3400'<=c<='\u9fff' for c in line) else 'helv')
                        expected+=entities_for(lines,rows,'page',page=page+1)
                pdf.set_metadata({'author':'synthetic-author@example.test','title':'Synthetic test material'});pdf.save(path,no_new_id=True);pdf.close()
            if fmt!='pdf':
                # Normalize generated timestamps/ZIP metadata for byte reproducibility.
                with zipfile.ZipFile(path) as z: parts={n:z.read(n) for n in z.namelist()}
                if 'docProps/core.xml' in parts:
                    root=E.fromstring(parts['docProps/core.xml'])
                    for node in root.iter():
                        if E.QName(node).localname in {'created','modified'}:node.text='2026-01-01T00:00:00Z'
                    parts['docProps/core.xml']=E.tostring(root)
                with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
                    for name,data in sorted(parts.items()):
                        info=zipfile.ZipInfo(name,(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;z.writestr(info,data)
            if fmt!='pdf':expected.append({'type':'EMAIL_ADDRESS','value':'synthetic-author@example.test','source':'native','location_hint':'metadata author canary'})
            manifest['documents'].append({'path':str(path.relative_to(output)),'source_group':f'identity-{i:02d}','subset':subset,'format':fmt,'document_kind':kind_title,'workload_units':pages,'image_heavy':image_heavy,'mixed':mixed,'degradation':{4:'90-degree rotation',5:'projective photographed-form simulation + Gaussian blur 1.6',6:'low contrast 0.22',8:'small text 13px'}.get(i,'none'),'negative_control':i==9,'entities':expected,'removed_under_policy':[], 'preservation_profile':'v3','sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False))
    return {'documents':40,'manifest':str(output/'manifest.json'),'seed':seed}
