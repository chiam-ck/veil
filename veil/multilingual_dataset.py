"""Deterministic multilingual fixtures with independent gold and matched native/scanned groups."""
import io
import json
import hashlib
import zipfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import pymupdf as fitz
from docx import Document
from docx.shared import Inches
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from pptx import Presentation
from pptx.util import Inches as PI, Pt
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.drawing.image import Image as XI
from .datasets import CJK, FONT, patch_docx

COHORTS=['en','zh-Hans','zh-Hant','mixed-Hans','mixed-Hant']

def source(cohort,heldout):
    english=cohort=='en';trad=cohort.endswith('Hant');mixed=cohort.startswith('mixed')
    name=('Jasper Linden' if heldout else 'Amelia Quill') if english else (('林志明' if heldout else '陳怡君') if trad else ('李文静' if heldout else '张伟'))
    address='18 Fictional Crescent, Singapore 123456' if english else ('臺北市大安區仁愛路88號' if trad else '上海市浦东新区丁香路88号')
    if heldout:
        address='29 Imaginary Lane, Singapore 654321' if english else '高雄市苓雅區四維路36號' if trad else '北京市朝阳区望京路18号'
    labels=['Name','Email','Phone','NRIC','Address','Account number'] if english else (['姓名','電郵','電話','身份證','地址','客戶編號'] if trad else ['姓名','电邮','电话','身份证','地址','客户编号'])
    rows=list(zip(['PERSON','EMAIL_ADDRESS','PHONE_NUMBER','NRIC_FIN','ADDRESS','ACCOUNT_ID'],[name,'heldout@example.test' if heldout else 'synthetic@example.test','+65 9234 5678' if heldout else '+65 8123 4567','F7654321N' if heldout else 'S1234567D',address,'VEIL-HOLD-9024' if heldout else 'VEIL-CN-4281']))
    lines=[label+': '+value for label,(_,value) in zip(labels,rows)]
    if heldout:
        lines[0]=f'{name} requested a copy of this report.' if english else f'{name}申请了这份合成测试报告。'
        lines[4]=f'Mailing address: {address}' if english else f'{name}的联系地址为{address}。'
        # Second appearance is a genuine additional gold occurrence, independently counted.
    if mixed:
        other_name='Jasper Linden' if heldout else 'Amelia Quill'
        other_address='29 Imaginary Lane, Singapore 654321' if heldout else '18 Fictional Crescent, Singapore 123456'
        lines+=['Contact person: '+other_name,'Address: '+other_address]
        rows += [('PERSON',other_name),('ADDRESS',other_address)]
    lines+=['Invoice total 123.45; quantity 42; year 2026; ref 1234567','Synthetic test material' if english else '合成测试材料' if not trad else '合成測試材料']
    return rows,lines

def image(lines,traditional=False,degraded=False):
    im=Image.new('RGB',(1500,1000),'white');d=ImageDraw.Draw(im)
    font=ImageFont.truetype(CJK,30,index=3 if traditional else 2)
    d.text((45,25),'SYNTHETIC TEST MATERIAL',font=font,fill='#365550')
    for n,line in enumerate(lines):d.text((45,95+75*n),line,font=font,fill='black')
    if degraded:im=im.rotate(1.2,resample=Image.Resampling.BICUBIC,fillcolor='white').filter(ImageFilter.GaussianBlur(.35))
    return im

def expected(rows,lines,kind):
    return [{'type':t,'value':v,'source':kind,'line':i,'start_in_line':start,'end_in_line':start+len(v),'location_hint':'body'} for t,v in rows for i,line in enumerate(lines) for start in [line.find(v)] if start>=0]

def generate(output,seed=20261111):
    output=Path(output)
    if output.exists() and any(output.iterdir()):raise FileExistsError('Dataset output must be empty')
    output.mkdir(parents=True,exist_ok=True)
    manifest={'seed':seed,'synthetic':True,'version':3,'profile':'veil-preserve-content-v4','ground_truth_status':'Synthetic multilingual fixtures; labelled development and unlabelled heldout; no model-derived gold','documents':[]}
    for cohort in COHORTS:
      for heldout in [False,True]:
        subset='heldout' if heldout else 'development';rows,lines=source(cohort,heldout)
        for mode in ['native','scan','hybrid']:
          if heldout and mode=='hybrid':continue
          im=image(lines,cohort.endswith('Hant'),heldout);buf=io.BytesIO();im.save(buf,format='PNG');raw=buf.getvalue()
          folder=output/cohort/subset/mode;folder.mkdir(parents=True,exist_ok=True)
          for fmt in ['docx','pptx','xlsx','pdf']:
            path=folder/('synthetic.'+fmt);gold=[]
            if mode!='scan':gold+=expected(rows,lines,'native')
            if mode!='native':gold+=expected(rows,lines,'ocr')
            hidden='姓名: '+rows[0][1] if cohort!='en' else 'Name: '+rows[0][1]
            if fmt=='docx':
                doc=Document();doc.add_heading('Synthetic multilingual test material',0)
                if mode!='scan':
                    for line in lines:
                        p=doc.add_paragraph();cut=line.find(rows[0][1])+1 if rows[0][1] in line else len(line)//2;p.add_run(line[:cut]).bold=True;p.add_run(line[cut:])
                    tab=doc.add_table(rows=1,cols=2);tab.cell(0,0).text='姓名' if cohort!='en' else 'Name';tab.cell(0,1).text=rows[0][1]
                    gold.append({'type':'PERSON','value':rows[0][1],'source':'native','location_hint':'table label/value'})
                    doc.sections[0].header.paragraphs[0].text=hidden;gold.append({'type':'PERSON','value':rows[0][1],'source':'native','location_hint':'header'})
                    p=doc.add_paragraph(hidden);p.runs[0].font.hidden=True;gold.append({'type':'PERSON','value':rows[0][1],'source':'native','location_hint':'hidden run'})
                if mode!='native':doc.add_picture(io.BytesIO(raw),width=Inches(6.1))
                doc.core_properties.author='synthetic@example.test';doc.save(path)
                # Exercise comments, tracked changes, orphaned XML alongside Chinese.
                patch_docx(path)
                gold += [{'type':'EMAIL_ADDRESS','value':v,'source':'native','location_hint':'revision/orphan'} for v in ['inserted@example.test','orphan@example.test','deleted@example.test','docx-comment@example.test']]
            elif fmt=='pptx':
                prs=Presentation();prs.slide_width=PI(13.33);prs.slide_height=PI(9)
                slide=prs.slides.add_slide(prs.slide_layouts[6])
                title=slide.shapes.add_textbox(PI(.4),PI(.1),PI(12),PI(.5));title.text='SYNTHETIC TEST MATERIAL'
                if mode!='scan':
                    box=slide.shapes.add_textbox(PI(.4),PI(.8),PI(12),PI(7.5));tf=box.text_frame
                    for i,line in enumerate(lines):
                        p=tf.paragraphs[0] if i==0 else tf.add_paragraph();p.font.size=Pt(20);cut=len(line)//2;p.add_run().text=line[:cut];p.add_run().text=line[cut:]
                    table=slide.shapes.add_table(1,2,PI(.4),PI(8),PI(10),PI(.5)).table;table.cell(0,0).text='姓名' if cohort!='en' else 'Name';table.cell(0,1).text=rows[0][1]
                    gold.append({'type':'PERSON','value':rows[0][1],'source':'native','location_hint':'table'})
                    slide.notes_slide.notes_text_frame.text=hidden;gold.append({'type':'PERSON','value':rows[0][1],'source':'native','location_hint':'speaker notes'})
                    slide.element.set('show','0')
                if mode!='native':
                    scan=prs.slides.add_slide(prs.slide_layouts[6]);scan.shapes.add_picture(io.BytesIO(raw),PI(.2),PI(.5),width=PI(12.7))
                prs.save(path)
            elif fmt=='xlsx':
                wb=Workbook();ws=wb.active;ws.title='Synthetic';ws.append(['SYNTHETIC TEST MATERIAL'])
                if mode!='scan':
                    for line in lines:ws.append([line])
                    ws.append(['姓名' if cohort!='en' else 'Name',rows[0][1]]);gold.append({'type':'PERSON','value':rows[0][1],'source':'native','location_hint':'table'})
                    hs=wb.create_sheet('Hidden');hs.sheet_state='hidden';hs['A1']=hidden;gold.append({'type':'PERSON','value':rows[0][1],'source':'native','location_hint':'hidden sheet'})
                    ws.row_dimensions[3].hidden=True;ws.column_dimensions['B'].hidden=True
                    ws['D1']='="'+rows[0][1]+'"';ws['D2'].comment=Comment('姓名：王小华','Synthetic')
                    gold += [{'type':'PERSON','value':rows[0][1],'source':'native','location_hint':'preserved formula'},{'type':'PERSON','value':'王小华','source':'native','location_hint':'preserved comment'}]
                if mode!='native':
                    pic=XI(io.BytesIO(raw));pic.width=900;pic.height=600;ws.add_image(pic,'F3')
                ws.column_dimensions['A'].width=90;wb.save(path)
            else:
                pdf=fitz.open()
                if mode!='scan':
                    p=pdf.new_page(width=760,height=950);p.insert_text((30,35),'SYNTHETIC TEST MATERIAL')
                    for n,line in enumerate(lines):p.insert_text((30,90+48*n),line,fontsize=15,fontname='china-t' if cohort.endswith('Hant') else 'china-s' if cohort!='en' else 'helv')
                    p.insert_text((30,850),hidden,fontsize=15,fontname='china-s' if cohort!='en' else 'helv',render_mode=3);gold.append({'type':'PERSON','value':rows[0][1],'source':'native','location_hint':'invisible text'})
                    p.add_text_annot((700,30),'姓名：王小华')
                if mode!='native':
                    p=pdf.new_page(width=750,height=500);p.insert_image(p.rect,stream=raw)
                pdf.set_metadata({'title':'SYNTHETIC TEST MATERIAL','author':'synthetic@example.test'});pdf.save(path,no_new_id=True);pdf.close()
            if fmt!='pdf':
                with zipfile.ZipFile(path) as z:parts={n:z.read(n) for n in z.namelist()}
                from lxml import etree as E
                if 'docProps/core.xml' in parts:
                    root=E.fromstring(parts['docProps/core.xml'])
                    for n in root.iter():
                        if E.QName(n).localname in {'created','modified'}:n.text='2026-01-01T00:00:00Z'
                    parts['docProps/core.xml']=E.tostring(root)
                with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
                    for n,data in sorted(parts.items()):info=zipfile.ZipInfo(n,(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;z.writestr(info,data)
            if fmt=='docx':gold.append({'type':'EMAIL_ADDRESS','value':'synthetic@example.test','source':'native','location_hint':'metadata author canary'})
            manifest['documents'].append({'path':str(path.relative_to(output)),'language':cohort,'source_group':cohort+'-'+subset,'subset':subset,'format':fmt,'mode':mode,'image_heavy':mode=='scan','mixed':cohort.startswith('mixed'),'workload_units':1,'degradation':'rotation 1.2deg + blur .35' if heldout and mode!='native' else 'none','source_font_limitation':'Traditional PDF CID font omits some Simplified prose glyphs; source context loss is a known challenge' if fmt=='pdf' and cohort.endswith('Hant') and heldout and mode!='scan' else None,'entities':gold,'ocr_expected_text':'SYNTHETIC TEST MATERIAL\n'+'\n'.join(lines) if mode!='native' else None,'removed_under_policy':[],'preservation_profile':'v3','sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    return {'documents':len(manifest['documents']),'manifest':str(output/'manifest.json'),'seed':seed}
