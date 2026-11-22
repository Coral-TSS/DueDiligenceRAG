"""材料页批量入队规则：只决定哪些已登记材料可以加入现有队列，不启动模型。"""

ACTIVE_JOBS = frozenset({'queued', 'running', 'pause_requested'})
PAUSED_JOBS = frozenset({'paused'})
SUCCEEDED_JOBS = frozenset({'completed', 'succeeded'})
SKIP_LABELS = {
    'already_done': '已完成',
    'already_active': '已在队列或正在处理',
    'paused': '已暂停（未自动继续）',
    'unsupported': '暂不支持',
    'invalid': '无效',
}


def batch_enqueue_decision(document_status, latest_job_status=None):
    """返回 enqueue，或 skip_*。已暂停任务不会被自动继续；已成功的默认不再入队。"""
    if document_status == 'unsupported':
        return 'skip_unsupported'
    if document_status == 'invalid':
        return 'skip_invalid'
    if document_status != 'registered':
        return 'skip_invalid'
    if latest_job_status in ACTIVE_JOBS:
        return 'skip_already_active'
    if latest_job_status in PAUSED_JOBS:
        return 'skip_paused'
    if latest_job_status in SUCCEEDED_JOBS:
        return 'skip_already_done'
    return 'enqueue'


def batch_enqueue_report(counts, category=None):
    skipped = {key: int(counts.get(key, 0) or 0) for key in SKIP_LABELS}
    queued = int(counts.get('queued', 0) or 0)
    skip_bits = [f'{label} {skipped[key]} 份' for key, label in SKIP_LABELS.items() if skipped[key]]
    if queued:
        message = f'已将 {queued} 份材料加入处理队列。'
        if skip_bits:
            message += '跳过：' + '，'.join(skip_bits) + '。'
    else:
        message = '没有需要新处理的已登记材料。'
        if skip_bits:
            message += '跳过：' + '，'.join(skip_bits) + '。'
    return {'queued': queued, 'skipped': skipped, 'message': message, 'category': category}
