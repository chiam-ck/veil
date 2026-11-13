"""Remove saved PPTX printer metadata without changing slide content or layout."""
import posixpath
import re
from urllib.parse import unquote

from lxml import etree as E

from .office_preservation import PreservationError

PRINTER_PART = re.compile(r'(?:ppt|word|xl)/printersettings/printersettings[0-9]+\.bin', re.I)
REL_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def remove_pptx_printer_settings(parts, xml, session, part_ids):
    removed = {name for name in parts if PRINTER_PART.fullmatch(name)}
    if not removed:
        return
    for name, root in xml.items():
        if not name.endswith('.rels'):
            continue
        if '/_rels/' in name:
            base, tail = name.rsplit('/_rels/', 1)
            owner = posixpath.join(base, tail[:-5])
        elif name == '_rels/.rels':
            owner = ''
        else:
            raise PreservationError('Unsupported relationship part while removing saved printer settings')
        for rel in list(root):
            if rel.get('TargetMode') == 'External':
                continue
            target = unquote(rel.get('Target', ''))
            target = posixpath.normpath(target.lstrip('/') if target.startswith('/')
                                      else posixpath.join(posixpath.dirname(owner), target))
            if target not in removed:
                continue
            if not rel.get('Type', '').endswith('/printerSettings'):
                raise PreservationError('Unexpected relationship to a saved printer settings part')
            rid = rel.get('Id')
            owner_root = xml.get(owner)
            if owner_root is not None:
                for node in owner_root.iter():
                    if not isinstance(node.tag, str):
                        continue
                    for key, value in list(node.attrib.items()):
                        if E.QName(key).namespace == REL_NS and value == rid:
                            if E.QName(node).localname != 'prnPr':
                                raise PreservationError('Unexpected document reference to saved printer settings')
                            del node.attrib[key]
            root.remove(rel)
    content_types = xml.get('[Content_Types].xml')
    if content_types is not None:
        remaining_bins = any(name.lower().endswith('.bin') and name not in removed for name in parts)
        for item in list(content_types):
            if (item.get('PartName', '').lstrip('/') in removed or
                    (item.get('Extension', '').lower() == 'bin' and not remaining_bins)):
                content_types.remove(item)
    for name in sorted(removed):
        del parts[name]
        location = part_ids[name]
        session.removals['saved_printer_settings'] += 1
        session.changes['Saved printer settings removed'] += 1
        session.adjustments.append({'location': location, 'action': 'saved_printer_settings_removed'})
        session.issue('printer_settings_removed', 'info',
                      'Saved printer settings removed from this copy; slide content and layout are preserved.',
                      location, 'No action needed. Select a printer normally when printing this copy.')
