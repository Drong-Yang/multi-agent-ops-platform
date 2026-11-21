# 客服检索与回答模块迁移记录

源：用户复制基线 `项目融合/项目1Ai语音对话智能客服项目/rtc_basic-main-8decc9076f4683cfd741b4275e7c1e7ce029a4cd/rag_llm_server/services/`。

保留原项目 BSD-3-Clause 许可及火山 RTC 上游声明；整体来源见工程的 `THIRD_PARTY_NOTICES`。此次没有修改复制基线或原始项目。

- `commerce.py`、`utils.py`：原模块副本。
- `rag_service.py`：保留 `RagService.search` 的型号归一化、别名匹配、未知型号拦截、精确匹配与政策召回算法。改为相对导入、实例注入语料与配置，避免全局语料破坏快照隔离。
- `milvus_service.py`：保留 REST v2 协议、COSINE/FLAT、向量验证、入库 upsert、内容/来源/语料/Embedding 配置过滤。改为实例注入配置与 Embedding adapter；不覆盖或删除旧集合。
- `llm_service.py`：保留流式模型协议、原客服业务边界和政策限定语。只调整导入及配置/语料实例注入，支持新应用调用。
- `config.py`：新环境适配器，只读取当前进程的环境；不自动加载基线 `.env`。调用应用负责先加载新工程配置。默认知识模式明确为 `sample`，默认模拟生成明确标注；真实模式失败不切回模拟。
- `token_build.py`：原 RTC Token 编码与签名实现，保留官方固定测试向量。
- `main.py`：原客服 FastAPI 的兼容回归副本，供旧测试导入；不挂载到新系统的公众路由。新系统使用带身份/持久会话绑定的业务 API 和 `RtcController`。
- `evaluate_commerce.py`：原关键词评测脚本，只有导入、默认用例路径和报告输出路径适配到新工程。原 147 题资料完整保存在 `evals/legacy_customer`。

新 Milvus 集合通过 `FUSION_MILVUS_COLLECTION` 设置，默认 `commerce_ops_fusion_v1`，不会直接采用旧 `voice_commerce_v1`。

新工程基础语料为 `knowledge/snapshots/ecommerce-v1.json`，完整复制公开演示语料；原始字节 SHA256：`130baa1357152e3ee37c5329a75451e7bf148fd239a1cfaa2c24ce9c8c66ae3c`。仍是虚构商品和政策演示资料，没有接入真实交易或库存系统。

`KnowledgeService` 在上层将语料、归一化版本与非敏感检索配置形成快照 ID；`fact_bundle` 通过快照原文重建证据，不能相信客户端带回的正文或哈希。本文所列自动测试不等于已经重新验证真实云模型、Milvus 或麦克风通话。

原 `test_commerce/test_milvus/test_reproduction/test_evaluate_commerce` 迁移到 `backend/tests/legacy_customer`，共 28 项；仅适配导入和实例注入后的 Mock patch 路径。110 条 `assert*` 的 AST 均与源文件一致，比较证据在 `evals/runs/legacy-customer-assertion-manifest.json`。147 题 sample/mock 回归重跑为 136/147，保留原失败项，不将其解释为真实模型准确率。
