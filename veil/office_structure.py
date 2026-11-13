"""Recognise narrowly defined Office structure without asking a text detector.

Decisions depend on namespace, element, attribute AND value. Never treat an
arbitrary number, identifier, font name, or custom XML value as harmless.
Unrecognised fields/values keep the ordinary inspection path.
"""
import re
from functools import lru_cache

from lxml import etree as E

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
M = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
VT = 'http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes'
MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'

_PATH = re.compile(
    r'(?:\[Content_Types\]\.xml|_rels/\.rels|docProps/(?:core|app|custom)\.xml|'
    r'docProps/thumbnail\.(?:jpeg|jpg|png)|'
    r'(?:word|xl|ppt)/(?:document|styles|stylesWithEffects|settings|webSettings|'
    r'fontTable|numbering|workbook|sharedStrings|presentation|commentAuthors)\.xml|'
    r'(?:word|xl|ppt)/(?:_rels/)?(?:document|workbook|presentation)\.xml\.rels|'
    r'(?:word|xl|ppt)/(?:theme/theme|comments/comment|comments|header|footer|'
    r'footnotes|endnotes|worksheets/sheet|slides/slide|slideLayouts/slideLayout|'
    r'slideMasters/slideMaster|notesSlides/notesSlide|notesMasters/notesMaster)\d*\.xml|'
    r'(?:word|xl|ppt)/media/image\d+\.(?:png|jpeg|jpg)|'
    r'customXml/item(?:Props)?\d+\.xml|customXml/_rels/item\d+\.xml\.rels)\Z')


def standard_package_path(value):
    return _PATH.fullmatch(value) is not None


@lru_cache(maxsize=1)
def _builtin_styles():
    # Trusted library constants, never names learned from an uploaded file.
    from docx.enum.style import WD_BUILTIN_STYLE
    names = {re.sub(r'\s+', '', member.__doc__.rstrip('.')).casefold()
             for member in WD_BUILTIN_STYLE}
    names.update({'normal', 'tablenormal', 'defaultparagraphfont', 'tablegrid',
                  'placeholdertext', 'nospacing', 'revision', 'bibliography',
                  'tocheading', 'normaltable', 'nolist', 'macro'})
    return names


def _style(value):
    key = re.sub(r'\s+', '', value).casefold()
    key = re.sub(r'-(?=accent[1-6]$)', '', key)
    if key.endswith('char'):
        key = key[:-4]
    return key in _builtin_styles() or re.fullmatch(
        r'(?:lightshading|lightlist|lightgrid|mediumshading[12]|mediumlist[12]|'
        r'mediumgrid[123]|darklist|colorfulshading|colorfullist|colorfulgrid)'
        r'(?:accent[1-6])?', key) is not None


_NUMBERS = {
    W: {
        'pgSz': {'w', 'h', 'code'}, 'pgMar': {'top', 'right', 'bottom', 'left', 'header', 'footer', 'gutter'},
        'docGrid': {'linePitch', 'charSpace'}, 'spacing': {'before', 'after', 'line', 'val'},
        'ind': {'left', 'right', 'firstLine', 'hanging', 'start', 'end'},
        'tab': {'pos'}, 'tblInd': {'w'}, 'tblW': {'w'}, 'tcW': {'w'},
        'gridCol': {'w'}, 'cols': {'num', 'space'}, 'sz': {'val'}, 'szCs': {'val'},
        'kern': {'val'}, 'position': {'val'}, 'outlineLvl': {'val'}, 'uiPriority': {'val'},
        'defaultTabStop': {'val'}, 'tblStyleRowBandSize': {'val'}, 'tblStyleColBandSize': {'val'},
        'numId': {'val'}, 'abstractNumId': {'val'}, 'abstractNum': {'abstractNumId'},
        'num': {'numId'}, 'lvl': {'ilvl'}, 'ilvl': {'val'}, 'start': {'val'},
        'latentStyles': {'count', 'defUIPriority'}, 'lsdException': {'uiPriority'},
    },
    A: {
        'gs': {'pos'}, 'tint': {'val'}, 'shade': {'val'}, 'satMod': {'val'},
        'lumMod': {'val'}, 'lumOff': {'val'}, 'alpha': {'val'}, 'ln': {'w'},
        'lin': {'ang'}, 'rot': {'lat', 'lon', 'rev'}, 'fillToRect': {'l', 't', 'r', 'b'},
        'outerShdw': {'blurRad', 'dist', 'dir'}, 'bevelT': {'w', 'h'},
        'off': {'x', 'y'}, 'ext': {'cx', 'cy'}, 'chOff': {'x', 'y'}, 'chExt': {'cx', 'cy'},
        'rPr': {'sz', 'kern', 'spc', 'baseline'}, 'defRPr': {'sz', 'kern', 'spc', 'baseline'},
        'lnRef': {'idx'}, 'fillRef': {'idx'}, 'effectRef': {'idx'},
    },
    S: {
        'fontId': {'val'}, 'numFmtId': {'val'}, 'xf': {'numFmtId', 'fontId', 'fillId', 'borderId', 'xfId'},
        'sz': {'val'}, 'col': {'min', 'max', 'width'}, 'row': {'ht'},
        'sheetFormatPr': {'baseColWidth', 'defaultRowHeight', 'defaultColWidth'},
        'pageMargins': {'left', 'right', 'top', 'bottom', 'header', 'footer'},
    },
    P: {'sldSz': {'cx', 'cy'}, 'notesSz': {'cx', 'cy'}, 'pos': {'x', 'y'}},
    M: {'lMargin': {'val'}, 'rMargin': {'val'}, 'wrapIndent': {'val'}},
    VT: {'vector': {'size'}},
}
_BORDER = {'top', 'bottom', 'left', 'right', 'insideH', 'insideV', 'start', 'end'}
_THEME = {'accent1', 'accent2', 'accent3', 'accent4', 'accent5', 'accent6',
          'background1', 'background2', 'text1', 'text2', 'dark1', 'dark2',
          'light1', 'light2', 'hyperlink', 'followedHyperlink', 'none'}
_BOOL = {'0', '1', 'true', 'false', 'on', 'off'}
_FONTS = {'Arial', 'Calibri', 'Cambria', 'Cambria Math', 'Courier', 'Courier New',
          'Symbol', 'Times New Roman', 'ＭＳ ゴシック', 'ＭＳ 明朝', '맑은 고딕', '宋体',
          '新細明體', 'Angsana New', 'Nyala', 'Vrinda', 'Shruti', 'MoolBoran', 'Tunga',
          'Raavi', 'Euphemia', 'Plantagenet Cherokee', 'Microsoft Yi Baiti',
          'Microsoft Himalaya', 'MV Boli', 'Mangal', 'Gautami', 'Latha',
          'Estrangelo Edessa', 'Kalinga', 'Kartika', 'DokChampa', 'Iskoola Pota',
          'Mongolian Baiti', 'Microsoft Uighur', 'Sylfaen', 'Cordia New', 'DaunPenh',
          'Noto Sans CJK SC', 'Noto Sans CJK TC', 'Liberation Sans', 'Liberation Serif'}


def structural_value(node, key=None):
    """True only for a recognised field containing a permitted structural value."""
    if not isinstance(node.tag, str):
        return False
    q = E.QName(node)
    ns, tag = q.namespace, q.localname
    if key is None:
        # Text in cells, metadata, custom XML and field instructions is never
        # excluded here, even when it is numeric or resembles a standard name.
        return False
    attr = E.QName(key)
    value = node.get(key, '')
    k = attr.localname
    if attr.namespace == MC and k == 'Ignorable':
        return all(re.fullmatch(r'(?:w|wp|a|p|x)\d+', v) and node.nsmap.get(v)
                   for v in value.split()) and bool(value.split())
    if attr.namespace not in ({W, None} if ns == W else {M, None} if ns == M else {None}):
        return False
    if k in _NUMBERS.get(ns, {}).get(tag, set()):
        return re.fullmatch(r'-?\d{1,9}(?:\.\d{1,4})?', value) is not None
    parent = node.getparent()
    parent_tag = parent.tag if parent is not None else None
    if (ns == W and ((tag == 'rFonts' and k in {'ascii', 'hAnsi', 'eastAsia', 'cs'}) or
                     (tag == 'font' and k == 'name'))) or (ns == A and tag in {'font', 'latin', 'ea', 'cs'} and k == 'typeface') or (ns == M and tag == 'mathFont' and k == 'val') or (ns == S and tag == 'name' and k == 'val'):
        return (ns != S or parent_tag == '{'+S+'}font') and (value in _FONTS or value == '')
    if ns == W:
        if tag == 'rFonts' and k in {'asciiTheme', 'hAnsiTheme', 'eastAsiaTheme', 'cstheme', 'csTheme'}:
            return value in {'majorHAnsi', 'minorHAnsi', 'majorEastAsia', 'minorEastAsia', 'majorBidi', 'minorBidi'}
        if tag == 'clrSchemeMapping' and k in {'bg1', 't1', 'bg2', 't2', 'accent1', 'accent2', 'accent3', 'accent4', 'accent5', 'accent6', 'hyperlink', 'followedHyperlink'}:
            return value in _THEME
        if tag in _BORDER and k in {'sz', 'space', 'w'}:
            return re.fullmatch(r'\d{1,6}', value) is not None
        if (tag in _BORDER | {'color', 'shd'} and k in {'color', 'fill'}) or (tag == 'color' and k == 'val'):
            return value == 'auto' or re.fullmatch(r'[0-9A-Fa-f]{6}', value) is not None
        if tag in _BORDER | {'color', 'shd'} and k in {'themeTint', 'themeShade', 'themeFillTint', 'themeFillShade'}:
            return re.fullmatch(r'[0-9A-Fa-f]{2}', value) is not None
        if tag in _BORDER | {'color', 'shd'} and k in {'themeColor', 'themeFill'}:
            return value in _THEME
        if (tag in {'rsid', 'rsidRoot', 'nsid', 'tmpl'} and k == 'val') or (tag in {'p', 'r', 'sectPr'} and k in {'rsidR', 'rsidRPr', 'rsidSect', 'rsidDel', 'rsidP'}):
            return re.fullmatch(r'[0-9A-Fa-f]{8}', value) is not None
        if tag == 'sig' and k in {'usb0', 'usb1', 'usb2', 'usb3', 'csb0', 'csb1'}:
            return re.fullmatch(r'[0-9A-Fa-f]{8}', value) is not None
        if tag == 'panose1' and k == 'val':
            return re.fullmatch(r'[0-9A-Fa-f]{20}', value) is not None
        if tag == 'charset' and k == 'val':
            return re.fullmatch(r'[0-9A-Fa-f]{2}', value) is not None
        if (tag == 'style' and k == 'styleId') or (tag in {'name', 'basedOn', 'next', 'link', 'pStyle', 'rStyle', 'tblStyle'} and k == 'val') or (tag == 'lsdException' and k == 'name'):
            return (tag != 'name' or parent_tag == '{'+W+'}style') and _style(value)
        if tag in {'b', 'bCs', 'i', 'iCs', 'vanish', 'keepNext', 'keepLines', 'widowControl'} and k == 'val':
            return value in _BOOL
        if tag == 'style' and k in {'default', 'customStyle'} or tag == 'latentStyles' and k in {'defLockedState', 'defSemiHidden', 'defUnhideWhenUsed', 'defQFormat'} or tag == 'lsdException' and k in {'locked', 'semiHidden', 'unhideWhenUsed', 'qFormat'}:
            return value in _BOOL
        if tag in {'lang', 'themeFontLang'} and k in {'val', 'eastAsia', 'bidi'}:
            return re.fullmatch(r'[a-z]{2,3}(?:-[A-Za-z]{2,4})?', value) is not None
        if tag in _BORDER and k == 'val':
            return value in {'nil', 'none', 'single', 'double', 'dotted', 'dashed', 'thick'}
        enums = {('spacing', 'lineRule'): {'auto', 'exact', 'atLeast'},
                 ('shd', 'val'): {'clear', 'nil', 'solid'}, ('tab', 'val'): {'left', 'right', 'center', 'num', 'decimal', 'bar'},
                 ('family', 'val'): {'auto', 'roman', 'swiss', 'modern', 'script', 'decorative'},
                 ('pitch', 'val'): {'default', 'fixed', 'variable'}, ('numFmt', 'val'): {'bullet', 'decimal', 'lowerLetter', 'upperLetter', 'lowerRoman', 'upperRoman'},
                 ('multiLevelType', 'val'): {'singleLevel', 'multilevel', 'hybridMultilevel'},
                 ('lvlJc', 'val'): {'left', 'right', 'center'}, ('u', 'val'): {'single', 'double', 'none'}}
        if (tag, k) in enums:
            return value in enums[tag, k]
    if ns == A:
        if (tag == 'srgbClr' and k == 'val') or (tag == 'sysClr' and k == 'lastClr'):
            return re.fullmatch(r'[0-9A-Fa-f]{6}', value) is not None
        if tag == 'schemeClr' and k == 'val':
            return value in {'accent1', 'accent2', 'accent3', 'accent4', 'accent5', 'accent6', 'lt1', 'lt2', 'dk1', 'dk2', 'tx1', 'tx2', 'bg1', 'bg2', 'phClr', 'hlink', 'folHlink'}
        if tag == 'font' and k == 'script':
            return re.fullmatch(r'[A-Z][a-z]{3}', value) is not None
        if (tag, k) in {('lin', 'scaled'), ('gradFill', 'rotWithShape'), ('outerShdw', 'rotWithShape')}:
            return value in _BOOL
    return False
