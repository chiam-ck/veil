"""Reusable document engine with local OCR/editing and configured LLM detection."""
import io
import hashlib
import importlib
import json
import os
import re
import time
import zipfile
import warnings
from collections import Counter
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from lxml import etree as ET
from PIL import Image, ImageDraw
import numpy as np
import pymupdf as fitz
from . import config, __version__
from .detection import Session
from .llm import create_detector, LLMError, ProviderError
from .office_preservation import (PreservationError, SpreadsheetPreserver, replace_group, revision_views, inspect_fields, inspect_relationships, metadata_context)
from .languages import HAN
from .office_language import contexts, fonts, spreadsheet_fonts, prune_shared_strings
from .office_printers import remove_pptx_printer_settings
from .office_structure import standard_package_path, structural_value

Image.MAX_IMAGE_PIXELS = 24_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)
NS = {"w":"http://schemas.openxmlformats.org/wordprocessingml/2006/main", "a":"http://schemas.openxmlformats.org/drawingml/2006/main", "s":"http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
fitz.TOOLS.mupdf_display_errors(False)
fitz.TOOLS.mupdf_display_warnings(False)
class Blocked(Exception): pass

def local(tag): return ET.QName(tag).localname if isinstance(tag,str) else ""

def parse(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper(): raise Blocked("XML entities/DTDs are unsupported")
    return ET.fromstring(data, ET.XMLParser(resolve_entities=False, no_network=True, huge_tree=False))

def package(path):
    if not zipfile.is_zipfile(path): raise Blocked("Not an Office ZIP package; encrypted Office files are unsupported")
    with zipfile.ZipFile(path) as z:
        infos=z.infolist()
        if len(infos)>config.MAX_PARTS: raise Blocked("Archive part limit exceeded")
        if len({i.filename for i in infos}) != len(infos): raise Blocked("Duplicate archive paths")
        size=0
        for i in infos:
            p=PurePosixPath(i.filename)
            if p.is_absolute() or ".." in p.parts or "\\" in i.filename or "\x00" in i.filename: raise Blocked("Unsafe archive path")
            if i.flag_bits & 1: raise Blocked("Encrypted archive is unsupported")
            if (i.external_attr >> 16) & 0o170000 == 0o120000: raise Blocked("Archive symbolic links are unsupported")
            size+=i.file_size
            if size>config.MAX_EXPANDED or i.file_size>30_000_000 or (i.file_size>1_000_000 and i.file_size/max(1,i.compress_size)>200): raise Blocked("Archive expansion limit exceeded")
        return {i.filename:z.read(i) for i in infos if not i.is_dir()}

class Engine:
    def __init__(self):
        started=time.perf_counter()
        lock=json.loads((Path(__file__).parent/'model-lock.json').read_text())
        self.model_assets=lock['models']
        for asset in self.model_assets:
            base=Path(importlib.import_module(asset['package']).__file__).parent
            if hashlib.sha256((base/asset['path']).read_bytes()).hexdigest()!=asset['sha256']:
                raise RuntimeError('Local model checksum mismatch; reinstall pinned assets')
        self.detector=create_detector()
        from .ocr import LocalOCR
        self.ocr=LocalOCR()
        self.cold_start=time.perf_counter()-started

    def image(self, image, session, location):
        started=time.perf_counter()
        image=image.convert("RGB")
        if image.width*image.height>Image.MAX_IMAGE_PIXELS: raise Blocked("Image pixel limit exceeded")
        result,_=self.ocr(np.asarray(image))
        session.timings["ocr"]+=time.perf_counter()-started
        rows=result or []
        text="\n".join(r[1] for r in rows)
        _,hits=session.redact(text,location,"ocr")
        low=sum(float(r[2])<0.8 for r in rows)
        small=sum(int(min(np.linalg.norm(np.asarray(r[0][0])-r[0][3]),np.linalg.norm(np.asarray(r[0][1])-r[0][2]))<12) for r in rows)
        rotated=sum(int(abs(np.degrees(np.arctan2(r[0][1][1]-r[0][0][1],r[0][1][0]-r[0][0][0])))>10) for r in rows)
        # Only an exactly uniform image is proven blank here. A nonblank image
        # with zero recognized lines is an explicit coverage gap, not a logo guess.
        uniform=all(a==b for a,b in image.getextrema())
        session.ocr_observations.append({"location":location,"lines":len(rows),"low_confidence_lines":low,"small_text_lines":small,"rotated_lines":rotated,"uniform_image":uniform})
        if not rows and not uniform:
            session.issue('ocr_no_text','review','OCR found no text in a nonblank image; text coverage is uncertain',location,'Inspect this image for text that OCR missed.')
        elif low or small or rotated:
            session.issue('ocr_quality','review',f'OCR needs checking: {low} low-confidence, {small} small-text and {rotated} rotated lines',location,'Compare the image with the sanitised result for missed or incomplete redactions.')
        elif rows:
            session.issue('ocr_processed','info','Image text processed by OCR without quality flags',location,'No specific review task identified; OCR confidence is not proof of complete detection.')
        else:
            session.issue('blank_image','info','Uniform blank image inspected; no text content',location,'No action needed.')
        draw=ImageDraw.Draw(image)
        offset=0
        for box,word,confidence in rows:
            if any(h.start < offset+len(word) and h.end > offset for h in hits):
                # Full OCR line + margin, not inferred character widths.
                xs=[p[0] for p in box]; ys=[p[1] for p in box]
                draw.rectangle((max(0,min(xs)-4),max(0,min(ys)-4),min(image.width,max(xs)+4),min(image.height,max(ys)+4)),fill="black")
            offset+=len(word)+1
        return image

    def pdf(self,path,out,s):
        started=time.perf_counter()
        with fitz.open(path) as doc:
            if doc.needs_pass: raise Blocked("Password-protected or encrypted PDF is unsupported")
            if doc.is_encrypted or doc.xref_get_key(-1,"Encrypt")[0] != "null": raise Blocked("Encrypted PDF is unsupported, including files with an empty user password")
            if not 0<len(doc)<=config.MAX_PAGES: raise Blocked("PDF page limit exceeded")
            s.removals["PDF metadata, attachments, annotations, links, forms, layers and original object history discarded"]+=1
            clean=fitz.open()
            for n,page in enumerate(doc):
                t=time.perf_counter()
                # The output already excludes annotations. Remove them in memory
                # before inspecting drawings: annotation icons otherwise look like
                # unexplained vector text and trigger unnecessary OCR/review.
                for annotation in list(page.annots() or []):page.delete_annot(annotation)
                words=page.get_text("words",sort=True)
                text=""
                for wi,w in enumerate(words):
                    if wi: text += "\n" if w[5:7] != words[wi-1][5:7] else " "
                    text += w[4]
                s.timings["extraction"]+=time.perf_counter()-t
                _,hits=s.redact(text,f"page:{n+1}")
                if page.rect.width*page.rect.height*4>Image.MAX_IMAGE_PIXELS: raise Blocked("PDF page pixel limit exceeded")
                pix=page.get_pixmap(matrix=fitz.Matrix(2,2),alpha=False,annots=False)
                image=Image.frombytes("RGB",[pix.width,pix.height],pix.samples)
                # OCR a rendered page if it contains images or no extractable text.
                if page.get_images() or not words:
                    image=self.image(image,s,f"page:{n+1}:render")
                elif page.get_drawings():
                    # Plain stroked borders/table rules are not an inspection gap.
                    simple=all(not d.get('fill') and all(item[0] in {'l','re'} for item in d['items']) for d in page.get_drawings())
                    if simple:
                        s.issue('pdf_rules','info','Straight PDF rules/borders retained in the rendered output',f'page:{n+1}','No action needed.')
                    else:
                        image=self.image(image,s,f'page:{n+1}:vector-render')
                draw=ImageDraw.Draw(image)
                offset=0
                for w in words:
                    if any(h.start<offset+len(w[4]) and h.end>offset for h in hits):
                        # Word coordinates are unrotated; transform into displayed page coordinates.
                        r=fitz.Rect(w[:4])*page.rotation_matrix
                        draw.rectangle((max(0,r.x0*2-2),max(0,r.y0*2-2),min(image.width,r.x1*2+2),min(image.height,r.y1*2+2)),fill="black")
                    offset+=len(w[4])+1
                t=time.perf_counter()
                buf=io.BytesIO(); image.save(buf,format="PNG")
                new=clean.new_page(width=page.rect.width,height=page.rect.height)
                new.insert_image(new.rect,stream=buf.getvalue())
                s.timings["writing"]+=time.perf_counter()-t
            t=time.perf_counter()
            clean.set_metadata({"producer":"Veil / PyMuPDF (AGPL)","title":"Sanitised document"})
            clean.save(out,garbage=2,deflate=True,clean=True)
            clean.close()
            s.timings["writing"]+=time.perf_counter()-t
        # This output has no retained text, annotation or attachment objects.
        with fitz.open(out) as check:
            if len({p.xref for p in check}) != len(check): raise Blocked("Final PDF has repeated page objects")
            if check.embfile_count() or any(p.get_text().strip() or list(p.annots() or []) for p in check): raise Blocked("Final PDF structural audit failed")

    @staticmethod
    def office_detection_values(xml):
        """Warm the same independent values used by the preservation passes.

        Any value not represented here still takes the ordinary detector path;
        batching changes request overhead, never whether content is inspected.
        """
        from openpyxl.formula import Tokenizer
        from urllib.parse import unquote
        for root in xml.values():
            hints=contexts(root,xml)
            grouped=set()
            for group in root.xpath('//w:p | //a:p | //s:si | //s:is',namespaces=NS):
                leaves=[n for n in group.iter() if local(n.tag) in {'t','delText'} and n.text]
                grouped.update(leaves)
                for view in revision_views(leaves):
                    yield (''.join(n.text or '' for n in view),False,hints.get(group))
            for node in root.iter():
                if not isinstance(node.tag,str):continue
                kind=local(node.tag)
                if node.text and node.text.strip():
                    context=metadata_context(node)
                    spreadsheet_value=ET.QName(node).namespace==NS['s'] and kind in {'f','v','definedName'}
                    if node not in grouped and kind not in {'instrText','delInstrText'} and not spreadsheet_value:
                        yield (node.text,kind in {'sz','numFmtId'},context)
                    if ET.QName(node).namespace==NS['s'] and kind=='v':
                        cell=node.getparent();hint=hints.get(cell)
                        yield (node.text,cell.get('t')!='str' and not hint,hint)
                    if kind in {'f','definedName'}:
                        try:
                            for token in Tokenizer('='+node.text).items:
                                if token.type=='OPERAND' and token.subtype=='TEXT':
                                    yield (token.value[1:-1].replace('""','"'),False,hints.get(node.getparent()))
                        except Exception:
                            pass  # The normal formula pass will block malformed inputs.
                if node.tail and node.tail.strip():yield (node.tail,False,None)
                for key,value in node.attrib.items():
                    k=local(key)
                    if k in {'Id','id','type','Type','ContentType','Extension','PartName','Target','TargetMode','space'}:continue
                    if structural_value(node,key):continue
                    context=metadata_context(node,k)
                    yield (value,not(context is not None or (kind=='sheet' and k=='name')),context)
                if node.get('TargetMode')=='External':
                    yield (unquote(node.get('Target','')),True,None)
            for node in root.xpath('//comment()'):
                yield (node.text or '',False,None)

    def office(self,path,out,s):
        t=time.perf_counter(); parts=package(path)
        part_ids={name:f"part:{i:04d}" for i,name in enumerate(sorted(parts))}
        for name,identifier in part_ids.items():
            low=name.lower()
            label='Office package part'
            if low.startswith('docprops/') and low.endswith(('.jpeg','.jpg','.png')):label='Office preview thumbnail'
            elif '/media/' in low:label='Embedded image'
            elif '/printersettings/' in low:label='Printer settings'
            elif low.startswith('customxml/'):label='Custom XML'
            elif low=='word/document.xml':label='Word document'
            elif re.fullmatch(r'ppt/slides/slide\d+\.xml',low):label='Slide '+re.search(r'(\d+)\.xml$',low)[1]
            elif re.fullmatch(r'xl/worksheets/sheet\d+\.xml',low):label='Worksheet part '+re.search(r'(\d+)\.xml$',low)[1]
            s.locations[identifier]=label+' ('+identifier+')'
        s.timings["extraction"]+=time.perf_counter()-t
        ext=path.suffix.lower()
        required={".docx":"word/document.xml",".pptx":"ppt/presentation.xml",".xlsx":"xl/workbook.xml"}[ext]
        if required not in parts: raise Blocked("Office package does not match declared format")
        xml={}
        if hasattr(self.detector, 'prefetch'):
            started=time.perf_counter()
            try:self.detector.prefetch((name,True,None) for name in parts if not standard_package_path(name))
            finally:s.timings['detection']+=time.perf_counter()-started
        for name,data in parts.items():
            low=name.lower()
            if standard_package_path(name):s.local_structure_values+=1
            elif self.detector.find(name,structured=True): raise Blocked("PII-like internal package filename unsupported")
            if re.fullmatch(r'(?:ppt|word|xl)/printersettings/printersettings[0-9]+\.bin',low):
                if ext!='.pptx':
                    s.issue('opaque_printer_settings','review','Saved printer settings were kept, but Veil could not check them for personal data.',part_ids[name],'Remove saved printer settings in an Office application before sharing, or ask the administrator for help.')
                    s.uninspected.append({'location':part_ids[name],'type':'printer_settings','reason':'opaque_binary_retained'})
                continue
            if any(x in low for x in ("vbaproject","embeddings/","activex/","customui/","externallinks/","connections.xml","querytables/","webextensions/")):
                raise Blocked("Embedded objects, macros, external data or executable Office content unsupported")
            if low.endswith((".xml",".rels",".vml")):
                xml[name]=parse(data)
                if low.endswith(".vml"):
                    clients=xml[name].xpath("//*[local-name()='ClientData']")
                    if not clients or any(n.get("ObjectType")!="Note" for n in clients): raise Blocked("Non-comment VML drawing unsupported")
                    # Comment VML is retained and inspected, including its text and attributes.
            elif not (low.endswith((".png",".jpg",".jpeg")) and ("/media/" in low or low.startswith("docprops/"))):
                raise Blocked("Unsupported binary or image part in Office package")
            if "comment" in low and low.endswith(".xml") and ET.QName(xml[name]).namespace not in {NS["w"],NS["s"],"http://schemas.openxmlformats.org/presentationml/2006/main"}:
                s.issue('extended_comments','review','Extended comment schema retained and text-inspected; structural coverage is incomplete',part_ids[name],'Check extended comments and their associated content in an Office application.')
            if low.startswith("customxml/"):
                root=xml[name]
                bib='http://schemas.openxmlformats.org/officeDocument/2006/bibliography'
                ds='http://schemas.openxmlformats.org/officeDocument/2006/customXml'
                empty_bib=root.tag=='{'+bib+'}Sources' and not len(root) and not (root.text or '').strip() and set(root.attrib)<={'SelectedStyle','StyleName','Version'}
                schema_metadata=root.tag=='{'+ds+'}datastoreItem' and all(n.tag in {'{'+ds+'}datastoreItem','{'+ds+'}schemaRefs','{'+ds+'}schemaRef'} and not (n.text or '').strip() and not (n.tail or '').strip() and all(k in {'{'+ds+'}itemID','{'+ds+'}uri'} for k in n.attrib) for n in root.iter()) and all(n.get('{'+ds+'}uri')==bib for n in root.iter('{'+ds+'}schemaRef')) and bool(list(root.iter('{'+ds+'}schemaRef')))
                relationships=low.endswith('.rels') and root.tag=='{http://schemas.openxmlformats.org/package/2006/relationships}Relationships' and all(n.tag=='{http://schemas.openxmlformats.org/package/2006/relationships}Relationship' and not len(n) and set(n.attrib)<={'Id','Type','Target','TargetMode'} for n in root)
                if empty_bib or schema_metadata or relationships:
                    s.issue('office_xml_scaffolding','info','Standard empty bibliography or schema/relationship metadata inspected',part_ids[name],'No action needed; text and attributes still pass through PII inspection.')
                else:
                    s.issue('custom_xml','review','Custom XML text inspected; arbitrary-schema semantics are not fully supported',part_ids[name],'Check custom data and any data-bound document content in the source application.')
        if ext==".pptx":
            remove_pptx_printer_settings(parts,xml,s,part_ids)
            if sum(n.startswith("ppt/slides/slide") and n.endswith(".xml") for n in parts)>config.MAX_PAGES: raise Blocked("Slide limit exceeded")
        if hasattr(self.detector, 'prefetch'):
            started=time.perf_counter()
            try:self.detector.prefetch(self.office_detection_values(xml))
            finally:s.timings['detection']+=time.perf_counter()-started
        spreadsheet=SpreadsheetPreserver(xml,s,part_ids,contexts)
        for name,root in xml.items():
            if root.xpath("//*[local-name()='documentProtection' or local-name()='workbookProtection' or local-name()='sheetProtection' or local-name()='fileSharing'][@*]"):
                raise Blocked("Password/protection settings in Office file are unsupported")
            # Known complex content is never silently declared complete.
            if root.xpath("//*[local-name()='object' or local-name()='oleObj' or local-name()='control' or local-name()='altChunk' or local-name()='contentPart']"):
                raise Blocked("Unsupported embedded or control content")
            if any(x in name.lower() for x in ("charts/","diagrams/","pivot","slicer","printersettings","threaded")):
                raise Blocked("Unsupported charts, diagrams, pivot or threaded content")
            # Comment anchors, insertions, deletions, moves and formatting revisions
            # stay in their original structures. Inspect both revision text views.
            inspect_fields(root,s)
            # Group text across formatting runs, including hidden/header/footer/notes/master parts.
            hints=contexts(root,xml)
            seen=set()
            groups=root.xpath("//w:p | //a:p | //s:si | //s:is",namespaces=NS)
            for index,node in enumerate(groups):
                leaves=[n for n in node.iter() if local(n.tag) in {"t","delText"} and n.text]
                if not leaves: continue
                replace_group(leaves,s,f"{part_ids[name]}:group:{index}",hints.get(node))
                seen.update(leaves)
            # Every XML part, including orphaned XML: inspect residual text and attributes.
            for index,node in enumerate(root.iter()):
                if not isinstance(node.tag,str): continue
                if node not in seen and node.text and node.text.strip() and local(node.tag) not in {"instrText","delInstrText"} and not (ET.QName(node).namespace==NS["s"] and local(node.tag) in {"f","v","definedName"}):
                    node.text,_=s.redact(node.text,f"{part_ids[name]}:text:{index}",structured=local(node.tag) in {"sz","numFmtId"},context=metadata_context(node))
                if node.tail and node.tail.strip(): node.tail,_=s.redact(node.tail,f"{part_ids[name]}:tail:{index}")
                for key,value in list(node.attrib.items()):
                    k=local(key)
                    if k in {"Id","id","type","Type","ContentType","Extension","PartName","Target","TargetMode","space"}:continue
                    if k=="initials" and node.attrib.get(key)=="REDACTED":continue
                    if structural_value(node,key):
                        s.local_structure_values+=1
                        continue
                    context=metadata_context(node,k)
                    clean,hits=s.redact(value,f"{part_ids[name]}:attribute:{index}:{k}",structured=not(context is not None or (local(node.tag)=="sheet" and k=="name")),include_text=False,context=context)
                    node.attrib[key]=clean
                    if hits and (k=="author" or context in {"Name","姓名"}):
                        initials="{"+ET.QName(key).namespace+"}initials" if ET.QName(key).namespace else "initials"
                        if initials in node.attrib:
                            node.set(initials,"REDACTED");s.changes['PII author initials sanitised']+=1
        for root in xml.values(): fonts(root)
        spreadsheet_fonts(xml)
        prune_shared_strings(xml,s)
        spreadsheet.finish()
        inspect_relationships(xml,s,part_ids)
        for name,data in list(parts.items()):
            if name in xml:
                # Preserve benign XML comments; redact any detected PII in place.
                root=xml[name]
                if root.xpath('//processing-instruction()'):
                    raise Blocked('Unsupported XML processing instructions cannot be safely preserved')
                for index,node in enumerate(root.xpath('//comment()')):
                    node.text,_=s.redact(node.text or '',f'{part_ids[name]}:xml-comment:{index}')
                parts[name]=ET.tostring(root,xml_declaration=True,encoding="UTF-8",standalone=True)
            elif "/media/" in name or name.lower().startswith("docprops/"):
                with Image.open(io.BytesIO(data)) as im:
                    if getattr(im,"n_frames",1)>1: raise Blocked("Animated Office images unsupported")
                    clean=self.image(im,s,part_ids[name])
                t=time.perf_counter()
                buf=io.BytesIO(); clean.save(buf,format="PNG" if name.lower().endswith(".png") else "JPEG",quality=95)
                parts[name]=buf.getvalue(); s.timings["writing"]+=time.perf_counter()-t
        s.sanitise_previews()
        text_audit="\n".join(item["text"] for item in s.sanitised).casefold()
        if any((len(value)>=4 or HAN.search(value)) and value in text_audit for _,value in s.mapping):
            raise Blocked("Known PII residual in sanitised Office text; intact processing could not be verified")
        # Audit all final XML bytes for every original detection, including orphaned parts.
        t=time.perf_counter()
        for name,data in parts.items():
            if name.endswith((".xml",".rels")):
                root=parse(data)
                values=" ".join(root.itertext())+" "+" ".join(v for n in root.iter() if isinstance(n.tag,str) for v in n.attrib.values())
                normal=values.casefold()
                if any((len(value)>=4 or HAN.search(value)) and value in normal for _,value in s.mapping): raise Blocked("Known PII residual in final Office package")
        with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as z:
            for name,data in parts.items(): z.writestr(name,data)
        package(out)
        s.timings["writing"]+=time.perf_counter()-t

    def process(self,path,output, private_preview=False):
        path=Path(path); output=Path(output)
        if output.exists() and any(output.iterdir()): raise FileExistsError("Output directory must be empty; refusing to overwrite or remove existing files")
        output.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.detector.cache.clear()
        if hasattr(self.detector, 'reset'): self.detector.reset()
        s=Session(self.detector); start=time.perf_counter()
        report={"policy":config.POLICY,"version":__version__,"device":"CPU","status":"processing","models":{"detector":"Configured OpenAI-compatible LLM; exact source quotes", "normalization":"Original Unicode sent to LLM; OpenCC used for script and font routing","ocr":f"rapidocr-onnxruntime=={version('rapidocr-onnxruntime')}","runtime":f"onnxruntime=={version('onnxruntime')}","ocr_models":"PP-OCRv4 English baseline + PP-OCRv5 mobile Chinese/Simplified/Traditional/English recognition; CPU only"},"model_assets":self.model_assets,"meaning":"Completed means supported pipeline completed, not zero PII or manual review"}
        out=output/("sanitised"+path.suffix.lower())
        if out.exists(): raise FileExistsError("Refusing to overwrite output")
        try:
            if path.stat().st_size>config.MAX_BYTES: raise Blocked("File size limit exceeded")
            if path.suffix.lower() not in config.SUPPORTED: raise Blocked("Unsupported format; convert legacy DOC/PPT/XLS locally to DOCX/PPTX/XLSX first")
            if path.suffix.lower()==".pdf": self.pdf(path,out,s)
            else: self.office(path,out,s)
            report["status"]="review_required" if s.warnings else "completed"
            (output/"sanitised.txt").write_text("\n\n".join(x["text"] for x in s.sanitised),encoding="utf8")
            if private_preview:
                (output.parent/"original"/"extracted.json").write_text(json.dumps(s.originals,ensure_ascii=False),encoding="utf8")
                (output.parent/"original"/"redaction-values.json").write_text(json.dumps(s.redaction_values,ensure_ascii=False),encoding="utf8")
            report["artifacts"]=[out.name,"sanitised.txt","report.json"]
        except ProviderError as e:
            report.update(status='waiting_provider',reason=str(e),provider_error=e.code,artifacts=['report.json'])
            for p in output.iterdir():
                if p.is_file():p.unlink()
        except (Blocked,PreservationError,LLMError) as e:
            report.update(status="blocked",reason=str(e),artifacts=["report.json"])
            for p in output.iterdir():
                if p.is_file(): p.unlink()
        except Exception:
            # Exception messages from parsers can contain document text. Never serialize them.
            report.update(status="failed",reason="Processing error; malformed content or model/runtime failure",artifacts=["report.json"])
            for p in output.iterdir():
                if p.is_file(): p.unlink()
        report["policy_explanation"]="Versioned Veil implementation profile; no external or organizational policy is implied"
        report["preservation"]={"mode":"preserve-supported-content", "printer_settings":"PPTX saved printer settings removed; visible slide content and layout preserved", "formula_evaluation":False, "comments":"retained; detected PII redacted", "revisions":"insertions/deletions/moves and markers retained; both text views inspected", "unsupported":"blocked or review_required, never silently stripped", "counts":"revision views may count shared text more than once", "pdf":"existing image-only reconstruction; searchable text and interactive features are not retained"}
        report["language_coverage"]={"supported":["English","Chinese Simplified","Chinese Traditional","mixed English–Chinese"],"observed_segment_counts":dict(s.languages),"routing":"per segment; English and Chinese detectors combined where Han occurs", "offset_unit":"Unicode codepoints in original extracted segment", "traditional_policy":"aligned OpenCC t2s view for Chinese NER; original script retained", "fonts":"Noto Sans CJK SC/TC local fallback; fonts not embedded in Office"}
        report.update(entity_counts=dict(s.counts),entities=s.entities,warnings=sorted(s.warnings),removed_under_policy=dict(s.removals),content_changes=dict(s.changes),content_adjustments=s.adjustments,uninspected_content=s.uninspected,ocr_observations=s.ocr_observations,timings_seconds={k:round(v,4) for k,v in s.timings.items()},total_seconds=round(time.perf_counter()-start,4),cold_model_load_seconds=round(self.cold_start,4))
        report.update(issues=s.issues,notices=sorted(s.notices),review_policy={'version':'veil-evidence-review-v1','ocr_confidence_threshold':0.8,'minimum_text_height_pixels':12,'rotation_threshold_degrees':10,'meaning':'Only actionable review findings gate completion; information does not. Thresholds are heuristics, not calibrated probabilities.'})
        report['review_summary']={'required':report['status']=='review_required','review_items':sum(i['severity']=='review' for i in s.issues),'information_items':sum(i['severity']=='info' for i in s.issues)}
        report['detector']=self.detector.describe()
        report['inspection']={'locally_validated_structure_values':s.local_structure_values}
        report['language_coverage']['routing']='Local validation of recognised Office structure; configured LLM for remaining text and values'
        report['language_coverage']['traditional_policy']='Original Unicode text sent to the configured LLM'
        (output/"report.json").write_text(json.dumps(report,indent=2),encoding="utf8")
        self.detector.cache.clear()
        if hasattr(self.detector, 'reset'): self.detector.reset()
        return report
