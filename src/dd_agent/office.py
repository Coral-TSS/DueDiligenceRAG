"""现代 DOCX／XLSX／PPTX 的登记、来源单元和向量。不把它们写成 PDF 页。"""
import json
import zipfile
from io import BytesIO
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .lexical import document_tsvector


PARSE_VERSION = 'office-v1'
_PACKAGE = {
    '.docx': 'word/document.xml',
    '.xlsx': 'xl/workbook.xml',
    '.pptx': 'ppt/presentation.xml',
}
_LEGACY = {'.doc': '旧版 Word', '.xls': '旧版 Excel', '.ppt': '旧版 PowerPoint'}


def _extension(filename):
    lower = (filename or '').lower()
    for ext in (*_PACKAGE, *_LEGACY):
        if lower.endswith(ext):
            return ext
    return ''


def classify_saved_file(filename, path):
    """登记阶段只看扩展名和压缩包结构，不抽取正文，也不启动转换器。"""
    ext = _extension(filename)
    if ext in _LEGACY:
        return 'unsupported', f'{_LEGACY[ext]} 暂不解析。请另存为 docx、xlsx 或 pptx，本系统不调用外部转换。'
    if ext not in _PACKAGE:
        return None
    try:
        with zipfile.ZipFile(path) as package:
            names = [item.replace('\\', '/') for item in package.namelist()]
    except zipfile.BadZipFile:
        return 'invalid', '不是有效的 Office 压缩包，未解析正文。'
    if any(name == 'EncryptionInfo' or name == 'EncryptedPackage' or name.endswith('/EncryptionInfo') for name in names):
        return 'unsupported', '文件已加密或设有密码，未解析正文。'
    if any(name.endswith('vbaProject.bin') for name in names):
        return 'unsupported', '文件含宏。未执行宏，也未解析正文。'
    if _PACKAGE[ext] not in names:
        return 'invalid', '压缩包里没有该格式的正文，未解析。'
    return 'registered', '已登记，待解析。尚未提取段落、表格、单元格或幻灯片文字。'


def locator_label(locator, formula_state=''):
    kind = locator.get('kind')
    if kind == 'docx_paragraph':
        label = f"段落 {locator['paragraph']}"
    elif kind == 'docx_table_cell':
        label = f"表{locator['table']} 行{locator['row']} 列{locator['column']}"
    elif kind == 'xlsx_cell':
        label = f"工作表{locator['sheet']} 行{locator['row']} 列{locator['column']}"
    elif kind == 'pptx_paragraph':
        label = f"幻灯片{locator['slide']} 形状{locator['shape']} 段落{locator['paragraph']}"
    elif kind == 'docx_image':
        label = f"段落附近的图片 {locator.get('ordinal', 1)}"
    else:
        label = '来源'
    if formula_state == 'not_recalculated':
        label += '（含公式，未重算）'
    return label


def _key(locator):
    return json.dumps(locator, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def parse_office(filename, blob):
    ext = _extension(filename)
    if ext == '.docx':
        return _parse_docx(blob)
    if ext == '.xlsx':
        return _parse_xlsx(blob)
    if ext == '.pptx':
        return _parse_pptx(blob)
    return []


def _parse_docx(blob):
    from docx import Document
    document = Document(BytesIO(blob))
    units = []
    for index, paragraph in enumerate(document.paragraphs, 1):
        text = paragraph.text.strip()
        if text:
            units.append(_unit({'kind': 'docx_paragraph', 'paragraph': index}, text))
    for table_index, table in enumerate(document.tables, 1):
        for row_index, row in enumerate(table.rows, 1):
            for column_index, cell in enumerate(row.cells, 1):
                paragraphs = [item.text.strip() for item in cell.paragraphs if item.text.strip()]
                for paragraph_index, text in enumerate(paragraphs, 1):
                    units.append(_unit({
                        'kind': 'docx_table_cell', 'table': table_index, 'row': row_index,
                        'column': column_index, 'paragraph': paragraph_index,
                    }, text))
    for ordinal, _shape in enumerate(getattr(document, 'inline_shapes', []), 1):
        units.append(_unit({'kind': 'docx_image', 'ordinal': ordinal}, '此处有图、未提取', status='succeeded'))
    return units


def _parse_xlsx(blob):
    from openpyxl import load_workbook
    formulas = load_workbook(BytesIO(blob), data_only=False, read_only=True)
    cached = load_workbook(BytesIO(blob), data_only=True, read_only=True)
    units = []
    try:
        for name in formulas.sheetnames:
            sheet = formulas[name]
            if getattr(sheet, 'sheet_state', 'visible') != 'visible':
                continue
            values = cached[name]
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is None:
                        continue
                    formula = isinstance(cell.value, str) and cell.value.startswith('=')
                    cached_cell = values.cell(cell.row, cell.column)
                    cached_value = cached_cell.value
                    locator = {'kind': 'xlsx_cell', 'sheet': name, 'row': cell.row, 'column': cell.column_letter}
                    if formula and (cached_value is None or (isinstance(cached_value, str) and cached_value.startswith('='))):
                        units.append(_unit(locator, '', formula_state='unknown'))
                    elif formula:
                        units.append(_unit(locator, str(cached_value), formula_state='not_recalculated'))
                    else:
                        text = str(cell.value).strip()
                        if text:
                            units.append(_unit(locator, text))
    finally:
        formulas.close()
        cached.close()
    return units


def _parse_pptx(blob):
    from pptx import Presentation
    deck = Presentation(BytesIO(blob))
    units = []
    for slide_index, slide in enumerate(deck.slides, 1):
        for shape_index, shape in enumerate(slide.shapes, 1):
            if not getattr(shape, 'has_text_frame', False):
                continue
            for paragraph_index, paragraph in enumerate(shape.text_frame.paragraphs, 1):
                text = paragraph.text.strip()
                if text:
                    units.append(_unit({
                        'kind': 'pptx_paragraph', 'slide': slide_index,
                        'shape': shape_index, 'paragraph': paragraph_index,
                    }, text))
    return units


def _unit(locator, text, *, formula_state='', status='succeeded', error_code=''):
    return {
        'locator': locator, 'locator_key': _key(locator), 'text': text,
        'formula_state': formula_state, 'status': status, 'error_code': error_code,
    }


class OfficeIndex:
    def __init__(self, settings, database, models):
        self.settings = settings
        self.database = database
        self.models = models

    def version(self):
        import hashlib
        return hashlib.sha256(('office-unit-v1' + self.models.profile()).encode()).hexdigest()

    def index(self, project_id, document_id):
        from .retrieval import RetrievalError, valid_vector
        project_id, document_id = UUID(str(project_id)), UUID(str(document_id))
        document = self.database.document(project_id, document_id)
        if document is None:
            raise RetrievalError('材料不存在或不属于当前项目。')
        filename = next((name for name in document['filenames'] if _extension(name) in _PACKAGE), '')
        if not filename:
            raise RetrievalError('这份材料不是可解析的现代 Office 文件。')
        from .storage import Storage
        blob = Storage(self.settings.data_dir, self.settings.max_upload_bytes).path(document['storage_key']).read_bytes()
        units = parse_office(filename, blob)
        version = self.version()
        failed = []
        indexed = []
        for unit in units:
            unit_id = self._store_unit(document_id, unit)
            if unit['status'] != 'succeeded' or not unit['text'] or unit['formula_state'] == 'unknown':
                self._clear_chunks(unit_id, version)
                if unit['status'] != 'succeeded':
                    failed.append(unit['locator'])
                continue
            if self._reuse(unit_id, version, unit['text']):
                indexed.append(unit['locator'])
                continue
            try:
                vector = valid_vector(self.models.embed([unit['text']])[0])
            except Exception:
                failed.append(unit['locator'])
                self._mark_failed(unit_id)
                self._clear_chunks(unit_id, version)
                continue
            self._publish_chunk(document_id, unit_id, version, unit, vector)
            indexed.append(unit['locator'])
        return {'indexed_units': indexed, 'failed_units': failed, 'index_version': version}

    def _store_unit(self, document_id, unit):
        with self.database.connect() as conn:
            row = conn.execute('''INSERT INTO dd_agent.source_units
                (id,document_id,locator,locator_key,text,formula_state,parse_version,status,error_code)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (document_id, locator_key, parse_version) DO UPDATE SET
                  text=EXCLUDED.text, formula_state=EXCLUDED.formula_state,
                  status=EXCLUDED.status, error_code=EXCLUDED.error_code, locator=EXCLUDED.locator
                RETURNING id''', (
                    uuid4(), document_id, Jsonb(unit['locator']), unit['locator_key'], unit['text'],
                    unit['formula_state'], PARSE_VERSION, unit['status'], unit['error_code'])).fetchone()
        return row['id']

    def _reuse(self, unit_id, version, text):
        with self.database.connect() as conn:
            row = conn.execute('''SELECT text FROM dd_agent.source_unit_chunks
                WHERE unit_id=%s AND index_version=%s AND ordinal=0''', (unit_id, version)).fetchone()
        return row is not None and row['text'] == text

    def _clear_chunks(self, unit_id, version):
        with self.database.connect() as conn:
            conn.execute('DELETE FROM dd_agent.source_unit_chunks WHERE unit_id=%s AND index_version=%s', (unit_id, version))

    def _mark_failed(self, unit_id):
        with self.database.connect() as conn:
            conn.execute("UPDATE dd_agent.source_units SET status='failed', error_code='embed_failed' WHERE id=%s", (unit_id,))

    def _publish_chunk(self, document_id, unit_id, version, unit, vector):
        with self.database.connect() as conn:
            conn.execute('DELETE FROM dd_agent.source_unit_chunks WHERE unit_id=%s AND index_version=%s', (unit_id, version))
            conn.execute('''INSERT INTO dd_agent.source_unit_chunks
                (id,unit_id,document_id,index_version,ordinal,text,embedding,lexemes,locator)
                VALUES (%s,%s,%s,%s,0,%s,%s::public.vector,%s::tsvector,%s)''', (
                    uuid4(), unit_id, document_id, version, unit['text'], vector,
                    document_tsvector(unit['text']), Jsonb(unit['locator'])))

    def candidates(self, project_id, version, *, vector=None, tsquery=None, needles=(), included=None, limit=12):
        clause = ''
        args = [project_id, version]
        if included is not None:
            clause = ' AND c.document_id=ANY(%s::uuid[])'
            args.append(list(included))
        rows = []
        with self.database.connect() as conn:
            if vector is not None:
                rows.extend(conn.execute('''SELECT c.id, c.unit_id, c.document_id, c.text, c.locator,
                    u.formula_state, 1-(c.embedding <=> %s::public.vector) AS similarity
                    FROM dd_agent.source_unit_chunks c
                    JOIN dd_agent.source_units u ON u.id=c.unit_id AND u.status='succeeded'
                    JOIN dd_agent.documents d ON d.id=c.document_id
                    WHERE d.project_id=%s AND c.index_version=%s''' + clause + '''
                    ORDER BY c.embedding <=> %s::public.vector, c.id LIMIT %s''',
                    [vector, *args, vector, limit]).fetchall())
            if tsquery:
                exact = ('(' + ' OR '.join(['strpos(lower(c.text),%s)>0'] * len(needles)) + ') DESC,') if needles else ''
                rows.extend(conn.execute('''WITH q AS (SELECT %s::tsquery AS tsq)
                    SELECT c.id, c.unit_id, c.document_id, c.text, c.locator, u.formula_state,
                      ts_rank_cd(c.lexemes,q.tsq) AS keyword_score
                    FROM q JOIN dd_agent.source_unit_chunks c ON c.lexemes @@ q.tsq
                    JOIN dd_agent.source_units u ON u.id=c.unit_id AND u.status='succeeded'
                    JOIN dd_agent.documents d ON d.id=c.document_id
                    WHERE d.project_id=%s AND c.index_version=%s''' + clause + '''
                    ORDER BY ''' + exact + ''' ts_rank_cd(c.lexemes,q.tsq) DESC, c.id LIMIT %s''',
                    [tsquery, *args, *needles, limit]).fetchall())
        hits = []
        for row in rows:
            locator = row['locator']
            hits.append(dict(row, index_kind='source_unit', locator_label=locator_label(locator, row['formula_state']),
                             similarity=row.get('similarity') or 0))
        return hits

    def count_chunks(self, project_id, version):
        with self.database.connect() as conn:
            row = conn.execute('''SELECT count(*) AS n FROM dd_agent.source_unit_chunks c
                JOIN dd_agent.documents d ON d.id=c.document_id
                WHERE d.project_id=%s AND c.index_version=%s''', (project_id, version)).fetchone()
        return row['n']

    def read(self, project_id, document_id, unit_id, index_version):
        project_id, document_id, unit_id = UUID(str(project_id)), UUID(str(document_id)), UUID(str(unit_id))
        with self.database.connect() as conn:
            row = conn.execute('''SELECT u.id, u.document_id, u.locator, u.text, u.formula_state, c.index_version
                FROM dd_agent.source_units u
                JOIN dd_agent.documents d ON d.id=u.document_id
                LEFT JOIN dd_agent.source_unit_chunks c ON c.unit_id=u.id AND c.index_version=%s
                WHERE d.project_id=%s AND d.id=%s AND u.id=%s''',
                (index_version, project_id, document_id, unit_id)).fetchone()
        if row is None:
            return None
        return dict(row, locator_label=locator_label(row['locator'], row['formula_state']))
