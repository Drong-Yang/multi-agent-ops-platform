# 代码与资料来源

本工程使用用户放在“项目融合”中的开发副本作为来源，未对原项目目录写入。

- 创作主干来源：`../项目2自媒体运营Ai智能体/graph_xiaohongshu-master-9b8f479bd21512201eb7d74ed034cd05e4af28c2`。复用登录、工作流节点、质量检查、版本记录、PostgreSQL 检查点、图像服务及前端版本差异。其 README 声明基于 graph_xiaohongshu 教学项目改造；保留源码内的原有说明，不额外声称上游未提供的许可。
- 客服检索与语音来源：`../项目1Ai语音对话智能客服项目/rtc_basic-main-8decc9076f4683cfd741b4275e7c1e7ce029a4cd`。复用商品资料、检索、Milvus、模型流式调用、RTC SDK 页面、Token 和签名代码；保留北京火山引擎上游 BSD-3-Clause 标识及原 LICENSE/THIRD_PARTY_NOTICES。
- 新融合部分位于 `backend/app/fusion`、新增工作台与测试，来源与改动可对照 `docs/source-manifest.json` 和 TDD 日志。
- 独立 Milvus Compose 参考 [Milvus 官方部署配置](https://raw.githubusercontent.com/milvus-io/milvus/v2.4.4/deployments/docker/standalone/docker-compose.yml)，使用独立容器、卷和本机端口。运行镜像明确为 v2.4.4。

本机云凭据和私人运行数据不是交付源码。此文件不对第三方模型服务、云账户或上游素材授予额外许可。
