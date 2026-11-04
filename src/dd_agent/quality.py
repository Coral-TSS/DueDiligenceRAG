"""Strict, source-labelled field metrics. Missing ground truth is never a pass."""
# 开发侧离线质量评测辅助，不是产品要求用户逐页标注／复核的工作流。
# 参考答案须来自独立原图／原文核对；不能用模型自评或旧报告答案替代。
# 自动处理跑通、机器输出相关分数高，都不能直接作为这里的字段正确率。
from collections import defaultdict


def evaluate_fields(fields):
    # 按字段类别分别统计；重复 ID 会污染分母，因此直接拒绝。
    categories = defaultdict(list)
    ids = set()
    for field in fields:
        if field['id'] in ids:
            raise ValueError('参考字段 ID 重复。')
        ids.add(field['id'])
        categories[field['category']].append(field)

    def metrics(items):
        # reference=None 表示缺少可判读真值，不能算作“预测正确”。
        readable = [f for f in items if f.get('reference') is not None]
        correct = sum(f.get('prediction', '') == f['reference'] for f in readable)
        predicted = sum(bool(f.get('prediction', '')) for f in readable)
        return dict(readable_fields=len(readable), correct=correct,
                    exact_accuracy=correct/len(readable) if readable else None,
                    prediction_coverage=predicted/len(readable) if readable else None,
                    unknown_reference_fields=len(items)-len(readable),
                    unverifiable_adoptions=sum(bool(f.get('adopted')) and f.get('reference') is None for f in items),
                    wrong_adoptions=sum(bool(f.get('adopted')) and f.get('prediction', '') != f['reference'] for f in readable))

    return {**metrics(fields), 'by_category': {name:metrics(items) for name, items in categories.items()}}
