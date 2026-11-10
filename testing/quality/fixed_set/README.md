# RG-04 固定集格式

这里只冻结清单、字段槽位和报告怎么写。仓库里的样例是合成的，没有参考答案，也没有质量数字。

开发集和保留集由使用者在本机准备。同一 `content_family` 不能同时出现在两边，同一页或同一内容的不同裁剪也不能拆开。调参只用开发集。真实模型和整卷不在 `testing/run.py` 里跑，结果也不要提交进 git，更不要把客户原件或答案写进数据库。

字段槽位在 `field-template.json`。`expected` 保持空，直到本机对照原件填写。看不清的字段把 `readable` 改成 false，不要用推断值当答案。

```text
.venv-dev/bin/python testing/scripts/quality/run_fixed_set.py \
  --fields testing/quality/fixed_set/field-template.json \
  --development <本机开发集.json> \
  --holdout <本机保留集.json> \
  --predictions <本机预测.jsonl> \
  --out <本机报告.json>
```

不传 `--predictions` 时，报告状态是 `not_run`，各项指标为 null。报告始终分开写 OCR、召回、重排、来源和未知。重排数字不能写成召回提高。未达到 B-Q 门槛时 `accepted` 为 false，并列出差距。
