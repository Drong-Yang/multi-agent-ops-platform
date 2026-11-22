# 演示知识快照

`ecommerce-v1.json` 归档原始演示语料，包含 4 份来源、34 个演示 SKU、89 个知识单元。新实例默认使用 `ecommerce-v2.json`，其中 34 个 SKU 增补了基于原始规格的选购说明，55 个原有规则单元保留原文，另新增 15 个[选购场景指南](../电商选购场景指南-演示用.md)单元，共 5 份来源、104 个知识单元。指南覆盖配色、收纳尺寸、重量、形态和用途。外观资料按原始属性核对，颜色缺失或材质未注明的商品按实际资料回答。店铺、价格和型号均为演示数据。

服务根据规范化语料哈希、`commerce-normalize-v1` 及非敏感检索配置生成 `ks_...` 快照 ID，不将旧 `ecommerce-v1` 标签当作完整身份。激活指针由业务存储层事务管理；服务实例的 `activate` 是同步后的本地指针。新版本通过 `register_snapshot` 写入独立、不可覆盖的 manifest；旧版本可继续按 ID 查询。

模式分别标注：`sample` 是本地字面检索；`milvus` 是真实 Embedding 与 Milvus adapter；`cloud` 是原托管知识库 adapter。后两者需要单独配置和实测，不会因失败而静默切换到 sample。

`ensure_ready` 对 sample 明确报告 `local_corpus`；Milvus 必须验证已有集合字段/维度、COSINE 索引及当前语料/Embedding scope 的完整记录数。未验证的 Milvus 快照为 building，不允许 activate。cloud 当前仅保留配置和旧回归能力，配置完整仍报告 `configuration_only / ready=false`，新快照业务入口不宣称已经验证远端索引对齐。

`restore_snapshot(manifest)` 用于从业务数据库恢复历史快照：校验身份与哈希，保留原检索模式和 Embedding 配置，不改变 active 指针。恢复 live 快照后必须重新执行 ensure_ready，再由事务层同步激活。

`persist_snapshot(id)` 将完整 manifest 保存在本目录，以同级暂存文件、flush/fsync 和不可覆盖的原子硬链接发布。已有文件须通过相同身份校验；损坏文件会报错，不能静默覆盖。文件名由服务端计算出的快照 ID 生成。`register_snapshot` 自动复用此保存方法。

在 backend 目录可执行 `python -m app.fusion.knowledge` 检查当前模式；显式执行 `python -m app.fusion.knowledge --sync-milvus` 为新 `commerce_ops_fusion*` 集合同步索引。同步会调用真实 Embedding，产生正常云用量；命令不会改变数据库 active 指针，也不会删除旧集合。

在生成内容中发现的新表达不自动写回这里。FAQ 是独立资产，不获得事实来源资格。
