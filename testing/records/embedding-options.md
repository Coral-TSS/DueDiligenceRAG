# 向量模型选型观察（2026-10-21）

本机实际配置：Qwen3-Embedding-0.6B，1024 维，CPU float32；Qwen3-Reranker-0.6B 用于查询候选重排序。工作进程代码限制单次输入最多 4096 tokens，切片 700 字符、模型内部 batch=4。官方模型支持的上下文长度不等于本工程已启用的长度。

| 候选 | 特点 | 本工程改造点 |
| --- | --- | --- |
| Qwen3-Embedding-0.6B（当前） | 多语言，1024 维，0.6B；已完成工程集成 | 保持现有索引；CPU 载入和推理仍需实测 |
| BAAI/bge-small-zh-v1.5 | 中文轻量编码器，512 维；短文本检索候选 | 使用 CLS 池化、匹配查询指令及 token 预算；修改向量维度／模型注册；重建索引 |
| intfloat/multilingual-e5-small | 多语言轻量编码器，384 维；512-token 预算 | 平均池化与 query: / passage: 前缀；适配切片／维度；重建索引 |
| Qwen3-Embedding-0.6B-GGUF | 同系列官方 GGUF，支持 llama.cpp 等后端 | 增加本地推理服务适配；核对池化、归一化、指令和量化后的质量；独立版本重建索引 |

轻量模型和量化是备选，不在本轮安装／切换范围。不能仅替换权重路径：本工程 valid_vector、数据库 vector(1024)、索引版本和推理池化都与现有 Qwen 相连；不同模型的向量即使维度相同也不能混用。官方卡片不能证明本机尽调材料的速度和准确率，决定切换前需以相同材料测召回和耗时。

速度观察来自代码而非性能基准：图片 PDF 先经过基础 OCR，再可能进入视觉区域增强，然后才做文字向量化；OCR、Qwen 和视觉模型按批／区域加载，均在 CPU 运行。故不能把总耗时归因于 embedding 或承诺更换模型后的提速倍数。下一步可基于阶段记录安排常驻模型／批量复用性能试验，不由本轮暂停功能隐式扩大范围。

官方来源（2026-10-21 查阅）：

- [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
- [bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5)
- [multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small)
- [Qwen3-Embedding-0.6B-GGUF](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF)


## 历史业务任务只读观察

2026-10-21 汇总既有工商档案记录（不是用户所述 PPT 图片 PDF 的独立基准）：18 条成功视觉区域平均 elapsed_seconds 为 175.3 秒，15 条失败，1 条历史运行中记录。此为历史不同区域／配置的观察，未记录所有失败耗时，也没有 OCR／embedding 的可比阶段总时长，不能计算整体占比或承诺加速倍数。可以确定视觉增强本身存在明显等待，不能把全部耗时归因于 embedding。

升级前未发现本机 8765 监听服务；业务任务仍保存旧 running 状态和 15／649 页进度。升级迁移 6 后申请安全暂停，新服务启动确认任务为 paused，保留基础阅读 20／649 页与整链路 15／649 页。未执行新的全卷推理；用户自行点击继续即可恢复。历史候选未改写成新结果。
