"""批量入队选择规则，不连接 PostgreSQL，也不启动工作进程。"""
from dd_agent.batch_processing import batch_enqueue_decision, batch_enqueue_report


def test_batch_enqueue_skips_unsupported_invalid_succeeded_and_paused():
    assert batch_enqueue_decision('registered', None) == 'enqueue'
    assert batch_enqueue_decision('registered', 'failed') == 'enqueue'
    assert batch_enqueue_decision('registered', 'partial') == 'enqueue'
    assert batch_enqueue_decision('unsupported', None) == 'skip_unsupported'
    assert batch_enqueue_decision('invalid', None) == 'skip_invalid'
    assert batch_enqueue_decision('registered', 'queued') == 'skip_already_active'
    assert batch_enqueue_decision('registered', 'running') == 'skip_already_active'
    assert batch_enqueue_decision('registered', 'pause_requested') == 'skip_already_active'
    assert batch_enqueue_decision('registered', 'paused') == 'skip_paused'
    assert batch_enqueue_decision('registered', 'completed') == 'skip_already_done'
    assert batch_enqueue_decision('registered', 'succeeded') == 'skip_already_done'
    # 活动任务即使材料状态异常也不重新入队。
    assert batch_enqueue_decision('unsupported', 'running') == 'skip_unsupported'


def test_batch_enqueue_report_is_chinese_and_counts_skips():
    report = batch_enqueue_report(
        {'queued': 3, 'already_done': 2, 'already_active': 1, 'paused': 1, 'unsupported': 4, 'invalid': 1},
        category='finance',
    )
    assert report['queued'] == 3
    assert report['skipped'] == {
        'already_done': 2, 'already_active': 1, 'paused': 1, 'unsupported': 4, 'invalid': 1,
    }
    assert report['category'] == 'finance'
    assert '已将 3 份材料加入处理队列' in report['message']
    assert '已完成 2 份' in report['message']
    assert '已暂停' in report['message']
    empty = batch_enqueue_report({'queued': 0, 'already_done': 5})
    assert empty['queued'] == 0 and empty['skipped']['already_done'] == 5
    assert '没有需要新处理的已登记材料' in empty['message']
