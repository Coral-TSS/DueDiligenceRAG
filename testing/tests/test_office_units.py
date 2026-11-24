"""Office 检索单元要带上同一行或同一节的上下文。夹具都在测试里生成，不使用客户文件。"""
from io import BytesIO
import hashlib
import zipfile

from docx import Document
from openpyxl import Workbook
from pptx import Presentation

from dd_agent.chunks import MAX_CHARS
from dd_agent.office import INDEX_UNIT_VERSION, locator_label, office_version, parse_office


def _save(workbook):
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _docx_bytes(builder):
    document = Document()
    builder(document)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _texts(filename, blob):
    return parse_office(filename, blob)


def test_office_index_version_changes_with_the_unit_shape():
    assert INDEX_UNIT_VERSION == 'office-unit-v2'
    current = office_version('synthetic-profile')
    previous = hashlib.sha256(b'office-unit-v1synthetic-profile').hexdigest()
    assert current == hashlib.sha256(b'office-unit-v2synthetic-profile').hexdigest()
    assert current != previous
    assert office_version('') is None


def test_xlsx_pairs_each_data_row_with_sheet_title_and_headers():
    book = Workbook()
    sheet = book.active
    sheet.title = '利润表'
    sheet['A1'] = '利润表'
    sheet.merge_cells('A1:C1')
    sheet['A2'] = '项目'
    sheet['B2'] = 2023
    sheet['C2'] = 2024
    sheet['A3'] = '营业收入'
    sheet['B3'] = 100
    sheet['C3'] = 200
    sheet['A4'] = '营业成本'
    sheet['B4'] = 40
    sheet['C4'] = 50
    units = _texts('利润表.xlsx', _save(book))
    assert [unit['text'] for unit in units] == [
        '工作表利润表 | 利润表 | 项目: 营业收入 | 2023: 100 | 2024: 200',
        '工作表利润表 | 利润表 | 项目: 营业成本 | 2023: 40 | 2024: 50',
    ]
    assert units[0]['locator'] == {
        'kind': 'xlsx_rows', 'sheet': '利润表', 'row_from': 3, 'row_to': 3, 'columns': ['A', 'B', 'C'],
    }
    assert locator_label(units[0]['locator']) == '工作表利润表 行3 列A–C'
    assert all('项目: 2023' not in unit['text'] for unit in units)


def test_xlsx_keeps_two_column_facts_when_the_first_row_is_data():
    book = Workbook()
    sheet = book.active
    sheet.title = '基本信息'
    sheet['A1'] = '注册资本'
    sheet['B1'] = '1000万元'
    sheet['A2'] = '成立日期'
    sheet['B2'] = '2020年1月1日'
    units = _texts('基本信息.xlsx', _save(book))
    assert [unit['text'] for unit in units] == [
        '工作表基本信息 | 注册资本: 1000万元',
        '工作表基本信息 | 成立日期: 2020年1月1日',
    ]


def test_xlsx_repeats_a_vertical_merge_once_per_row():
    book = Workbook()
    sheet = book.active
    sheet.title = '分部'
    sheet['A1'] = '分部'
    sheet.merge_cells('A1:A2')
    sheet['B1'] = '一部'
    sheet['C1'] = 1
    sheet['B2'] = '二部'
    sheet['C2'] = 2
    units = _texts('分部.xlsx', _save(book))
    assert units[0]['text'] == '工作表分部 | A: 分部 | B: 一部 | C: 1'
    assert units[1]['text'] == '工作表分部 | A: 分部 | B: 二部 | C: 2'
    assert units[0]['text'].count('A: 分部') == 1
    assert units[1]['locator']['row_from'] == 2 and units[1]['locator']['row_to'] == 2


def test_xlsx_unknown_formula_is_not_invented_and_cached_value_stays_marked():
    book = Workbook()
    sheet = book.active
    sheet.title = '公式'
    sheet['A1'] = '营业收入'
    sheet['B1'] = '=1+1'
    sheet['C1'] = '=9+9'
    buffer = BytesIO()
    book.save(buffer)
    edited = BytesIO()
    with zipfile.ZipFile(BytesIO(buffer.getvalue())) as source, zipfile.ZipFile(edited, 'w') as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == 'xl/worksheets/sheet1.xml':
                data = data.replace(b'<f>1+1</f>', b'<f>1+1</f><v>2</v>', 1)
            target.writestr(info, data)
    units = _texts('公式.xlsx', edited.getvalue())
    assert len(units) == 1
    assert units[0]['text'] == '工作表公式 | A: 营业收入 | B: 2'
    assert units[0]['formula_state'] == 'not_recalculated'
    assert units[0]['locator']['unknown_columns'] == ['C']
    assert '=9+9' not in units[0]['text'] and '18' not in units[0]['text']
    assert '未重算' in locator_label(units[0]['locator'], units[0]['formula_state'])
    assert '部分公式未知' in locator_label(units[0]['locator'], units[0]['formula_state'])


def test_xlsx_only_unknown_formulas_do_not_become_text():
    book = Workbook()
    sheet = book.active
    sheet.title = '空公式'
    sheet['A1'] = '=9+9'
    units = _texts('空公式.xlsx', _save(book))
    assert len(units) == 1
    assert units[0]['text'] == ''
    assert units[0]['formula_state'] == 'unknown'
    assert units[0]['locator']['sheet'] == '空公式'
    assert '=9+9' not in units[0]['text']


def test_xlsx_hidden_sheet_is_skipped_and_single_column_notes_group_by_row_range():
    book = Workbook()
    notes = book.active
    notes.title = '说明'
    notes['A1'] = '补充说明一'
    notes['A2'] = '补充说明二'
    hidden = book.create_sheet('隐藏')
    hidden['A1'] = '秘密数字'
    hidden.sheet_state = 'hidden'
    units = _texts('说明.xlsx', _save(book))
    assert len(units) == 1
    assert units[0]['text'] == '工作表说明\n补充说明一\n补充说明二'
    assert units[0]['locator'] == {
        'kind': 'xlsx_rows', 'sheet': '说明', 'row_from': 1, 'row_to': 2, 'columns': ['A'],
    }
    assert all('秘密' not in unit['text'] for unit in units)


def test_xlsx_wide_row_splits_without_dropping_values():
    book = Workbook()
    sheet = book.active
    sheet.title = '宽表'
    sheet['A1'] = '项目'
    for column in range(2, 8):
        sheet.cell(1, column, f'列{column}')
        sheet.cell(2, column, '值' * 200)
    sheet['A2'] = '营业收入'
    units = _texts('宽表.xlsx', _save(book))
    assert len(units) > 1
    blob = '\n'.join(unit['text'] for unit in units)
    assert all(len(unit['text']) <= MAX_CHARS for unit in units)
    assert all(unit['text'].startswith('工作表宽表') and '项目: 营业收入' in unit['text'] for unit in units)
    for column in range(2, 8):
        assert (f'列{column}: ' + ('值' * 200)) in blob
    assert units[0]['locator']['row_from'] == units[-1]['locator']['row_to'] == 2
    assert units[0]['locator']['part'] == 1 and units[-1]['locator']['parts'] == len(units)


def test_docx_merges_body_under_a_heading_and_stops_at_the_next_heading():
    def build(document):
        document.add_paragraph('开篇。')
        document.add_heading('第一条 出资', level=1)
        document.add_paragraph('甲公司出资一百万。')
        document.add_paragraph('乙公司出资二百万。')
        document.add_heading('第二条 治理', level=1)
        document.add_paragraph('设董事会。')

    units = [unit for unit in _texts('章程.docx', _docx_bytes(build)) if unit['locator']['kind'] == 'docx_paragraphs']
    assert [unit['text'] for unit in units] == [
        '开篇。',
        '第一条 出资\n甲公司出资一百万。\n乙公司出资二百万。',
        '第二条 治理\n设董事会。',
    ]
    assert units[1]['locator'] == {'kind': 'docx_paragraphs', 'paragraph_from': 2, 'paragraph_to': 4}
    assert '第二条' not in units[1]['text']
    assert locator_label(units[1]['locator']) == '段落 2–4'


def test_docx_long_paragraph_splits_without_losing_characters():
    def build(document):
        document.add_paragraph('甲' * 2000)

    units = _texts('长段.docx', _docx_bytes(build))
    assert len(units) > 1
    assert all(len(unit['text']) <= MAX_CHARS for unit in units)
    assert _covers('甲' * 2000, [unit['text'] for unit in units])
    assert units[0]['locator']['paragraph_from'] == 1
    assert units[-1]['locator']['parts'] == len(units)


def test_docx_table_row_keeps_headers_and_does_not_repeat_merged_cells():
    def build(document):
        table = document.add_table(rows=3, cols=3)
        table.cell(0, 0).text = '类别'
        table.cell(0, 1).text = '项目'
        table.cell(0, 2).text = '金额'
        table.cell(1, 0).text = '收入'
        table.cell(1, 1).text = '主营'
        table.cell(1, 2).text = '100'
        table.cell(2, 1).text = '其他'
        table.cell(2, 2).text = '20'
        table.cell(1, 0).merge(table.cell(2, 0))
        titled = document.add_table(rows=3, cols=2)
        titled.cell(0, 0).text = '股东情况'
        titled.cell(0, 0).merge(titled.cell(0, 1))
        titled.cell(1, 0).text = '股东'
        titled.cell(1, 1).text = '持股比例'
        titled.cell(2, 0).text = '甲公司'
        titled.cell(2, 1).text = '20%'
        pairs = document.add_table(rows=2, cols=2)
        pairs.cell(0, 0).text = '注册资本'
        pairs.cell(0, 1).text = '1000万元'
        pairs.cell(1, 0).text = '成立日期'
        pairs.cell(1, 1).text = '2020年1月1日'

    units = [unit for unit in _texts('表.docx', _docx_bytes(build)) if unit['locator']['kind'] == 'docx_table_row']
    assert [unit['text'] for unit in units] == [
        '表1 | 类别: 收入 | 项目: 主营 | 金额: 100',
        '表1 | 类别: 收入 | 项目: 其他 | 金额: 20',
        '表2 | 股东情况 | 股东: 甲公司 | 持股比例: 20%',
        '表3 | 注册资本: 1000万元',
        '表3 | 成立日期: 2020年1月1日',
    ]
    assert all(unit['text'].count('收入') <= 1 for unit in units)
    assert all(unit['text'].count('股东情况') <= 1 for unit in units)
    assert units[1]['locator'] == {'kind': 'docx_table_row', 'table': 1, 'row': 3, 'columns': [1, 2, 3]}
    assert locator_label(units[1]['locator']) == '表1 行3 列1–3'


def test_pptx_merges_a_short_slide_and_splits_a_long_shape():
    short = Presentation()
    slide = short.slides.add_slide(short.slide_layouts[6])
    first = slide.shapes.add_textbox(0, 0, 100, 100)
    first.text_frame.text = '标题方案'
    second = slide.shapes.add_textbox(0, 0, 100, 100)
    second.text_frame.text = '第二段并购'
    buffer = BytesIO()
    short.save(buffer)
    units = _texts('短.pptx', buffer.getvalue())
    assert len(units) == 1
    assert units[0]['locator'] == {'kind': 'pptx_slide', 'slide': 1, 'shape_from': 1, 'shape_to': 2}
    assert units[0]['text'] == '标题方案\n第二段并购'
    assert locator_label(units[0]['locator']) == '幻灯片1 形状1–2'

    long = Presentation()
    slide = long.slides.add_slide(long.slide_layouts[6])
    shape = slide.shapes.add_textbox(0, 0, 100, 100)
    shape.text_frame.text = '甲' * 800
    shape.text_frame.add_paragraph().text = '乙' * 100
    buffer = BytesIO()
    long.save(buffer)
    units = _texts('长.pptx', buffer.getvalue())
    assert len(units) > 1
    assert all(len(unit['text']) <= MAX_CHARS for unit in units)
    assert all(unit['locator']['kind'] == 'pptx_shape' and unit['locator']['shape'] == 1 for unit in units)
    assert _covers('甲' * 800, [unit['text'] for unit in units if '甲' in unit['text']])
    assert any('乙' * 100 in unit['text'] for unit in units)


def _covers(source, pieces):
    """后一片可以重叠前一片，但不能把原文中间丢掉。"""
    cursor = 0
    for piece in pieces:
        found = source.find(piece, max(0, cursor - 80))
        if found < 0:
            return False
        cursor = max(cursor, found + len(piece))
    return cursor >= len(source)
