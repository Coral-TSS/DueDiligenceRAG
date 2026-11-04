"""按 token 预算切批。超预算的单条仍单独成批，避免整批被一条长文本卡住。"""


def pack_ranges(sizes, budget, limit):
    """返回半开区间。每一段至少包含一条；条数不超过 limit，总和尽量不超过 budget。"""
    if budget < 1 or limit < 1:
        raise ValueError('批预算无效。')
    ranges = []
    start = 0
    total = len(sizes)
    while start < total:
        used = 0
        end = start
        while end < total and end - start < limit:
            size = sizes[end]
            if end > start and used + size > budget:
                break
            used += size
            end += 1
        ranges.append((start, end))
        start = end
    return ranges
