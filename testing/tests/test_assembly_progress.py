from test_parent_progress import view
import pytest


@pytest.mark.parametrize('phase,label',[
    ('probing','核对重建来源'),('scanning','定位变化窗口'),
    ('preparing','重算变化窗口'),('validating','校验重建结果'),
    ('adopting','接纳重建结果'),('done','重建已接纳'),('blocked','重建已停止'),
])
def test_shadow_repair_phase_is_visible_in_chinese(phase,label):
    text=view(dict(status='running',progress=dict(scheduler='stages-v1',parent_index_mode='shadow',
        assembly=dict(enabled=True,saved_pages=10,total_pages=13,waiting='repair_'+phase,
            repair=dict(phase=phase,checked_pages=8,attempts=2,failures=0,error_code='')))))
    assert '跨页索引试运行' in text and label in text
    assert '已核对 8 页' in text and 'repair_'+phase not in text


def test_assembly_progress_is_separate_from_indexed_page_counts():
    text=view(dict(status='running',progress=dict(scheduler='stages-v1',total_pages=13,
        stages=dict(embedding=dict(succeeded=3)),
        assembly=dict(enabled=True,saved_pages=10,total_pages=13,closed=False,waiting='backpressure',failed=0))))
    assert '向量入库 3/13 页' in text
    assert '跨页组装 10/13 页' in text and '等待工作包处理' in text


def test_stale_assembly_is_not_presented_as_successful_document_end():
    text=view(dict(status='partial',progress=dict(scheduler='stages-v1',total_pages=2,
        assembly=dict(enabled=True,saved_pages=2,total_pages=2,closed=True,waiting='stale_sources',failed=1))))
    assert '来源已变化，等待局部重建' in text
    assert '跨页组装 2/2 页（已封口）' not in text
