"""版面切片、跨页回链，以及默认开启的父子索引。"""
from types import SimpleNamespace
from uuid import uuid4

from dd_agent.chunks import layout_chunks, split_text
from dd_agent.config import Settings
from dd_agent.pipeline import ProcessingPipeline
from test_retrieval import setup
from test_search_path import CountingModels


def test_clauses_lists_and_tables_stay_intact_and_cover_the_page():
    clauses = '第一条 公司名称。\n第二条 注册资本为2000万元。'
    parts = split_text(clauses)
    assert [part['text'] for part in parts] == ['第一条 公司名称。', '第二条 注册资本为2000万元。']
    assert [part['kind'] for part in parts] == ['clause', 'clause']
    table = '项目\t金额\n注册资本\t2000万元\n实收资本\t1000万元'
    assert split_text(table)[0]['text'] == table and split_text(table)[0]['kind'] == 'table'
    listing = '（一）股东名称\n（二）出资额'
    assert [part['text'] for part in split_text(listing)] == ['（一）股东名称', '（二）出资额']
    long = '公司基本情况\n\n' + '注册资本与股东信息。' * 160 + '\n下一段。'
    chunks = split_text(long)
    assert len(chunks) > 2
    covered = set()
    for chunk in chunks:
        assert 0 < len(chunk['text']) <= 700
        assert chunk['text'] == long[chunk['start_offset']:chunk['end_offset']]
        covered.update(range(chunk['start_offset'], chunk['end_offset']))
    assert all(index in covered for index, char in enumerate(long) if not char.isspace())
    assert split_text(' \n\t') == []


def test_line_gap_starts_a_new_paragraph_and_pages_keep_their_offsets():
    text = '公司应提供资料\n注册资本2000万元'
    lines = [
        dict(text='公司应提供资料', box=[[0, 0], [80, 0], [80, 10], [0, 10]]),
        dict(text='注册资本2000万元', box=[[0, 40], [90, 40], [90, 50], [0, 50]]),
    ]
    parts = split_text(text, lines)
    assert [part['text'] for part in parts] == ['公司应提供资料', '注册资本2000万元']
    left = dict(page_number=1, text='公司应提供下列资料，', lines=None)
    right = dict(page_number=2, text='并继续说明出资。', lines=None)
    owned = layout_chunks([left, right])
    merged = owned[1][0]
    assert merged['text'] == '公司应提供下列资料，\n并继续说明出资。'
    assert merged['spans'] == [
        dict(page_number=1, start=0, end=len(left['text'])),
        dict(page_number=2, start=0, end=len(right['text'])),
    ]
    assert owned[2] == []
    separate = layout_chunks([
        dict(page_number=1, text='公司住所位于吉林省。'),
        dict(page_number=2, text='第二条 注册资本。'),
    ])
    assert separate[1][0]['spans'] == [dict(page_number=1, start=0, end=len('公司住所位于吉林省。'))]
    assert separate[2][0]['text'].startswith('第二条')


def test_parent_index_is_on_by_default_and_search_keeps_page_spans(tmp_path, database_url, monkeypatch):
    assert Settings().parent_index_mode == 'on'
    monkeypatch.setattr('dd_agent.config.load_dotenv', lambda *args, **kwargs: None)
    monkeypatch.delenv('DDA_PARENT_INDEX_MODE', raising=False)
    assert Settings.from_env().parent_index_mode == 'on'
    pipeline = ProcessingPipeline(Settings(data_dir=tmp_path, parent_index_mode='on'))
    stages = SimpleNamespace(database=pipeline.database)
    assert pipeline.assembly_for({'document_id': str(uuid4())}, stages) is not None
    pipeline.settings = Settings(data_dir=tmp_path, parent_index_mode='off')
    assert pipeline.assembly_for({'document_id': str(uuid4())}, stages) is None
    app, project_id, document_id, service, _ = setup(tmp_path, database_url)
    service.models = CountingModels()
    database = app.state.database
    database.set_reading(document_id, 2, 'scan-v1')
    database.save_page(document_id, 'scan-v1', dict(page_number=1, status='succeeded', method='ocr', text='公司应提供下列资料，'))
    database.save_page(document_id, 'scan-v1', dict(page_number=2, status='succeeded', method='ocr', text='并继续说明出资。'))
    outcome = service.reindex_document(project_id, document_id, progress=lambda done, total: None)
    assert outcome['failed_pages'] == [] and outcome['pages'] == 2
    result = service.search(project_id, '出资')
    hit = result['results'][0]
    assert hit['text'] == '公司应提供下列资料，\n并继续说明出资。'
    assert hit['source_spans'][0]['page_number'] == 1 and hit['source_spans'][1]['page_number'] == 2
    assert hit['page_number'] == 1
    assert 'keyword' in hit['recall']['channels']
    again = service.reindex_document(project_id, document_id)
    assert again['failed_pages'] == [] and again['indexed_pages'] == []
