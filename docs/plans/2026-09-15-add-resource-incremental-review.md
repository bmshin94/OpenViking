# add_resources 增量更新技术评审

## 1. 摘要

本次 `add_resources` 改造分为两类：性能优化和接口范式改造。

### 1.1 性能优化

当前首次导入和后续导入都存在远程 IO 浪费。首次导入时，parser 生成的完整解析产物可能先逐文件上传到远程临时目录，再从临时目录全量提交到正式资源树；文件数越多，这段重复远程写入和清理成本越高。后续导入除此之外，还可能重复读取目标树正文，并让大量未变文件重复进入 semantic/embedding。

本次性能优化按导入阶段分别收敛成本：

- 首次导入：local parse output 让解析产物直接落本地，省去完整解析产物的远程临时上传和对应清理；正式资源仍然需要全量写入 AGFS，语义和向量仍然需要全量生成。
- no-op：在 N/F/V 快照完整一致且本次请求没有有效标量变化时，只完成必要的 source/parse/diff 判断，不写正式资源，不进入 semantic/embedding 队列。
- 小比例修改：只上传变化文件，只处理变化文件和必要父目录。
- shared source：复用已上传对象，减少重复 source 暂存。
- manifest 和向量库中的 MD5：优先判断内容是否变化，减少增量 diff 的远程正文读取。

### 1.2 接口范式改造

`add_resources` 不能继续把正式文件树当成唯一事实源。向量库已经保存了部分业务和检索状态，例如 `tags`、`search_tags`、ACL/owner、已有摘要和索引存在性；本次请求也可能要求修改 tags 等标量字段。新的增量链路需要从请求意图、本次解析产物、正式文件树和向量库快照共同生成明确计划，再交给后续队列执行。

接口范式改造对应的核心模型是：

```text
R/N/F/V -> ResourceDiffResult -> ContextUpdatePlan
                              ├── ContentTreeAction
                              ├── SemanticPlan / SemanticAction
                              └── Direct IndexAction
```

其中：

- `R` 是本次请求意图，例如目标 URI、tags、tag_mode、ACL 相关选项和 processing mode。
- `N` 是本次解析产物快照。
- `F` 是当前正式资源文件树。
- `V` 是当前向量库索引快照。

上游根据四类输入先得到最终 diff 事实，再生成 `ContextUpdatePlan`。该计划把正式内容树操作、语义 DAG 操作和直接索引操作分开表达：内容树操作在请求同步阶段执行；纯索引操作直接进入 embedding queue；依赖语义结果的索引操作绑定到 semantic tree entry，由 semantic DAG 完成后释放。这样可以表达“文件未变但 tags 变化”“文件存在但索引缺失”“索引存在但文件缺失”等旧接口难以准确表达的情况。

### 1.3 收益与适用场景

- **性能收益：** 首次导入主要收益来自省去完整解析产物的远程临时上传和清理；后续导入主要收益来自 no-op、小比例修改和 shared/local 模式下的重复读取与下游工作收敛。867 文件真实三轮验收中，local 相对 main 的 initial 中位数减少 31.3%，no-op 减少 89.2%，edit-one 减少 86.2%，edit-10% 减少 78.8%。
- **正确性收益：** 通过向量库快照参与计划，可以处理索引缺失、孤儿向量、文件/目录结构变化和请求级标量更新，降低或暴露旧链路中“文件提交不完整但请求仍返回成功”等问题。
- **接口收益：** 业务判断从 semantic dequeue 前移到显式 plan，后续可以继续接入 tags、ACL 和其他向量库标量，而不需要继续向 semantic worker 增加特殊分支。
- **代价和边界：** 初次导入仍需要全量文件、语义和向量处理；大比例修改时模型调用仍是主要成本；本轮允许文件和向量短期不一致，不引入完整跨系统事务协议；local artifact 只能在异步入队前消费，跨队列的 artifact ref 必须指向 AGFS 等远程持久化文件。

## 2. 背景：当前 add_resources 链路

### 2.1 当前旧方案执行行为

当前旧方案下，初次导入和后续导入的执行行为如下。两者的前置阶段基本相同，主要差异集中在正式目标树处理及其下游执行：

| 阶段 | 初次导入 | 后续导入 |
| --- | --- | --- |
| source、parser、解析产物暂存 | 获取输入、全量解析，并将完整产物写入 AGFS 临时树；以代码仓库 parser 为例，逻辑路径类似 `viking://temp/<临时标识>/repository/...`。 | 重新获取输入、全量解析，并重新写入完整 AGFS 临时树。 |
| 正式目标树处理 | 从临时 AGFS 树将完整产物提交到目标 `viking://` 资源树，建立正式文件结构。 | 读取已有正式资源树，与新的临时 AGFS 产物逐路径比较，再对新增、修改和删除路径执行同步。 |
| 同名文件修改 | 不存在旧文件，直接写入正式资源树。 | 删除或覆盖旧路径，再写入新文件。 |
| 语义处理 | 对完整新树生成文件摘要和目录 overview/abstract。 | 根据文件比较结果触发文件摘要和目录聚合。 |
| 向量处理 | 为完整新树生成并写入 L2/L1/L0。 | 对变化文件和受影响目录执行向量更新或删除。 |
| 清理 | 清理 source 暂存和解析临时 artifact。 | 清理 source 暂存和解析临时 artifact。 |

### 2.2 真实分阶段耗时与问题定位

这次诊断使用的是一个中等规模的 OpenViking Python 源码子集：223 个业务 Python 文件，约 69,947 行、2,633,282 bytes；测试输入还包含最小 Git 元数据，源暂存阶段实际上传约 240 个文件。两次请求导入同一份未修改输入，第二次用于观察 no-op 行为。

后端环境为：正式资源文件系统使用远程 TOS/S3，代码 parser 的旧解析产物写入 `viking://temp/...` 对应的 AGFS 临时树；向量库使用本地向量库。两轮使用相同的服务进程、模型配置和并发配置。这里的阶段时间来自运行时探针，阶段可能嵌套或并行，不能把各行直接相加还原端到端时间。

| 指标 | 首次导入 | 二次导入（no-op） |
| --- | ---: | ---: |
| 全流程耗时 | 453.72s | 452.07s（本轮校验失败） |
| 源文件暂存到远程文件系统 | 106.88s | 108.73s |
| 从远程物化到本地 | 55.68s | 57.57s |
| 解析及再次上传临时目录 | 100.65s | 99.64s |
| 目标树同步、逐文件比较 | 124.04s | 112.07s |
| 语义处理 DAG | 68.02s | 59.37s |
| 清理 | 10.20s | 12.88s |
| 文件摘要生成数 | 223 | 125 |
| 旧摘要复用数 | 0 | 222 |
| LLM/VLM 调用数 | 54 | 26 |
| embedding 调用数 | 283 | 171 |
| 向量 upsert 耗时 | 约 1.62s | 约 0.93s |
| 结果文件数 / L2 向量数 | 223 / 223 | 222 / 222（本轮校验失败） |

**首次导入的耗时特征**

- 首次导入的全量解析、正式树提交、文件/目录语义和向量构建属于正常建库成本；额外的远程临时树写入和正式树落地属于中间传输成本。

**二次导入（no-op）的耗时特征**

- 输入未变，但 source、全量 parser、完整远程临时树写入、目标树读取和逐路径比较仍执行，说明前置链路仍接近全量，存在明显的重复远程 IO。
- 目标树同步还包含目录列举、stat、正文读取和差异判断；本轮还因产物缺失触发了删除判断，因此不能视为健康 no-op 的纯比较成本。
- 语义 DAG 的主要耗时来自递归节点调度、当前内容检查、旧摘要读取、部分摘要/目录生成和 embedding；向量 upsert 不是主要瓶颈。
- 旧逻辑依赖生成式 `.overview.md` 反向解析文件摘要，文件缺失、名称或格式变化都可能影响复用，使相同内容的 no-op 语义工作量不稳定。
- 清理仍处理完整远程临时产物，成本与输入规模相关。

总体上，首次导入的全量处理主要是建库成本；二次 no-op 仍重复执行了大量前置存储、比较和语义判断，属于明显的可优化空间。

### 2.3 问题总结

结合现状链路和阶段耗时，当前问题可以归为两类。

**一是性能问题。**

- no-op 导入仍可能重复 source 处理、解析产物写入、目标文件读取、语义 DAG 和 embedding。
- diff 决策发生太晚，很多高成本存储操作已经完成。
- 小文件修改可能触发过大的语义和向量处理范围。
- AGFS 解析产物如果没有预计算内容指纹，增量 diff 会回退到远程正文比较。

**二是接口范式问题。**

- 请求意图没有成为 diff 的一等输入，例如 `tags` 和 `tag_mode`。
- 向量库已有状态没有稳定地作为资源计划输入。
- semantic queue consumer 中包含了一部分本应在入队前完成的业务判断。
- embedding 消息中的 `context_data` 同时混合模型输入、向量字段和内部写入控制。

正确性风险本质上也是接口范式过时带来的结果：向量库正在逐步成为部分数据的事实源，而不再只是文件内容的派生索引。tags/search_tags、ACL/owner、已有 abstract 和索引存在性等信息，不能只从正式文件树和文件正文恢复。典型表现包括：

| 场景 | 风险 |
| --- | --- |
| 同名文件修改先删除再 move/upload | 可能丢失已有 tags、ACL、owner、created_at 等标量信息。 |
| 文件存在但索引缺失 | 文件可以读取，但搜索召回不到。 |
| 索引存在但文件缺失 | 搜索可能返回孤儿记录。 |
| 文件内容未变但 tags 变化 | 只看文件内容会误判为 no-op，漏掉标量更新。 |
| 文件和目录发生同路径类型变化 | 普通 move 或 upsert 可能留下跨层级旧向量。 |
| 解析产物、目标树扫描或同步不完整 | 缺失路径可能被误判为 deleted。 |
| 目录聚合需要旧子摘要 | 只从文件树重建状态可能得到不完整或过期的目录摘要。 |

这些问题说明旧方案的增量判断、资源同步、语义 DAG 和向量库状态之间缺少统一的计划边界；后文分别围绕性能优化和接口范式改造展开。

## 3. 优化思路与总体设计

本节承接前面的性能问题和接口范式问题，先说明针对性优化方向，再给出统一的数据模型。具体实现协议在后续章节展开。

### 3.1 针对重复数据传输的优化思路

- shared source：已上传的共享对象直接作为 source 输入，避免再次复制到独立的 source 暂存目录。
- local parse output：解析产物在请求同步阶段写入本地目录；在确认资源变化前，不把完整解析产物上传到远程临时树。正式资源仍然写入 AGFS。
- 延迟正式上传：先在解析产物、正式文件树和向量索引之间完成 diff，确认新增或修改后才上传对应文件；no-op 不产生正式文件上传。
- 内容指纹：在解析产物最终 bytes 形成时记录 MD5，优先使用 N/V 的 MD5 判断内容是否变化，避免为正常交集重新读取远程正文。

这些方向主要针对文件数量较多的首次导入、no-op 和小比例修改。首次导入仍需要全量写正式资源；优化的是不必要的远程临时中转。

### 3.2 针对 DAG 抽象不足的优化思路

- 将资源文件同步、索引差异和请求标量变化在进入 semantic queue 前整理成明确的 `ResourceDiffResult` 和 `ContextUpdatePlan`。
- 将正式内容树修改表示成 `ContentTreeAction`，将语义 DAG 需要的变化文件、必要父目录、旧摘要和旧索引标量整理成 `SemanticPlan`。
- 将完全不依赖语义结果的索引操作表示成 direct `IndexAction`，直接进入 embedding queue；依赖语义结果的索引操作放在语义节点的 `IndexSlot` 中。
- unchanged 文件不作为需要重新摘要的任务，但在父目录聚合需要时保留其旧 abstract 作为输入。
- 将 embedding 队列抽象成明确的 embed/upsert、update_fields、delete 操作，让内容变化、标量变化和索引删除不再混在同一个隐式流程里。
- 保留现有 semantic/embedding 队列名称和通用队列职责，先只迁移 add_resources；write、batch-write、reindex 和 memory 后续再评估。

### 3.3 总体模型：R/N/F/V 与状态化行为

增量计划由四类输入共同决定：

| 输入 | 含义 | 主要职责 |
| --- | --- | --- |
| `R` Request intent | 本次 `add_resources` 请求参数 | 表达 tags、tag_mode、ACL 选项、processing mode 和 target 等请求意图。 |
| `N` New artifact snapshot | 本次解析产物 | 描述新文件内容和目录结构。 |
| `F` Formal resource tree | 当前正式资源树 | 描述当前资源文件存在性。 |
| `V` Vector index snapshot | 当前向量库记录 | 描述已有索引、旧摘要、业务元数据和索引存在性。 |

RNFV 先生成事实状态，再映射成三类行为。状态和行为需要分开：状态说明“发生了什么”，行为说明“接下来由哪一层执行什么”。

| 层次 | 状态 / 行为 | 作用对象 | 执行位置 |
| --- | --- | --- | --- |
| diff 事实 | `ContentState`、`IndexState` | N/F/V 的比较结果 | 请求同步阶段，只用于构造计划。 |
| 文件行为 | `ContentTreeAction` | 正式 AGFS 内容树 | 请求同步阶段，必须先于异步队列完成。 |
| 语义行为 | `SemanticAction` | 文件摘要、目录 overview/abstract、最小 DAG | semantic queue。 |
| 向量行为 | `IndexAction`、`IndexSlot` | L0/L1/L2 向量记录和标量字段 | direct `IndexAction` 直接进入 embedding queue；`IndexSlot` 由 semantic DAG 根据语义结果释放。 |

典型映射如下：

| RNFV 最终状态 | 文件行为 | 语义行为 | 向量行为 |
| --- | --- | --- | --- |
| 内容和索引都未变 | 无 | 无；若作为父目录聚合输入则 `REUSE` | 无 |
| 文件新增 | `ContentTreeAction.upsert` | 文件 `GENERATE`，祖先目录 `AGGREGATE` | 摘要可用后 `UPSERT` L2 |
| 文件内容修改 | `ContentTreeAction.upsert` | 文件 `GENERATE`，必要祖先目录 `AGGREGATE` | 根据语义结果 `UPSERT` 或只更新字段 |
| 文件删除 | `ContentTreeAction.delete` | 被删文件不入新语义树，祖先目录 `AGGREGATE` | direct `IndexAction.delete` 删除旧 L2 |
| 文件存在但索引缺失 | 无 | 需要摘要时 `GENERATE` 或 `REUSE` | `OUTPUT_READY` 后补建，或正文策略 direct `UPSERT` |
| 只有孤儿向量 | 无 | 无 | direct `IndexAction.delete` |
| 内容未变但 tags/search_tags 变化 | 无 | 无 | direct `IndexAction.update_fields` |
| file/directory 类型转换 | `ContentTreeAction.replace_kind` | 新类型对应 `GENERATE` 或 `AGGREGATE` | direct delete 旧层级，新层级按语义结果 upsert |

no-op 的定义必须覆盖所有相关维度：

内容和结构未变、必要索引完整存在、本次请求标量意图没有造成有效变化，三者同时成立时才是 no-op。

- 内容未变 + 标量未变：跳过。
- 内容未变 + 标量变化：只更新向量字段。
- 内容变化 + 标量未变：更新文件、语义和向量。
- 内容变化 + 标量变化：更新文件、语义、向量和标量字段。

### 3.4 本阶段边界

- 不引入 AGFS 和向量库之间的完整跨系统事务协议。
- 不保证文件提交成功但向量更新失败时的强一致。
- 不允许 local artifact 跨异步队列边界。
- 不重命名或替换现有 semantic / embedding 队列。
- 本阶段不迁移 write、batch-write、reindex 和 memory 入口到新 plan 协议。

## 4. 总体流程

新的 `add_resources` 流程可以拆成同步规划、正式内容树提交和异步派生执行三个阶段。

```text
请求参数 R + 源输入
  -> 解析 source：本地目录、Git、temp_upload shared 或其他 URI
  -> parser 输出解析产物：写入 local 或 AGFS artifact，并生成 manifest
  -> 构建 N：本次解析产物快照，包含相对路径、类型和内容 MD5
  -> 读取 F：目标 viking:// 正式资源树快照
  -> 读取 V：目标目录下的向量库索引快照
  -> 生成 ResourceDiffResult：按 R/N/F/V 判定内容状态和索引状态
  -> 生成 ContextUpdatePlan：映射为 ContentTreeAction、SemanticPlan 和 direct IndexAction
  -> 执行 ContentTreeAction：只把需要新增或修改的文件提交到正式 AGFS，并删除需要删除的旧资源
  -> 分流异步派生行为：
       direct IndexAction -> embedding queue
       SemanticPlan       -> semantic queue
                            -> semantic DAG 释放依赖语义结果的 IndexSlot
                            -> embedding queue
  -> 清理本次 source / parser 临时产物
```

同步阶段负责完成本次资源差异判断，并在需要时把业务文件提交到正式 AGFS。local/AGFS 解析产物只属于 parser 到资源提交之间的中间输入；解析产物进入正式 AGFS 的唯一时机是执行 `ContentTreeAction` 的资源提交阶段。语义队列开始执行后，worker 只能依赖正式 AGFS 资源、向量库和序列化 plan，不再依赖请求进程内对象或某台机器上的本地临时目录。

不同场景的边界是：

- 首次导入：N 中的全部业务文件都属于新增，需要从 local artifact 或 AGFS artifact 全量提交到正式 AGFS。
- no-op：N/F/V 快照完整一致且无有效标量变化时，不向正式 AGFS 上传任何业务文件；只清理本次解析产物。
- 文件修改或新增：只上传 `ContentTreeAction` 标记为 upsert 的文件。
- 文件删除或 orphan vector：文件删除通过 `ContentTreeAction.delete` 修改正式 AGFS；孤儿向量不触碰文件树，生成 direct `IndexAction.delete`。
- 文件/目录结构变化：删除旧类型对应的正式资源和索引，再提交新类型中实际存在的业务文件。

控制文件和派生 sidecar 不按照普通业务文件逐个上传；它们由解析产物完整性、目录语义和正式资源控制文件规则分别处理。

职责边界：

- parser 只产出解析 artifact 和 manifest，不判断增量行为。
- resource processor 读取 R/N/F/V，生成 `ResourceDiffResult` 和 `ContextUpdatePlan`，并同步执行 `ContentTreeAction`。
- semantic processor 只消费 `SemanticPlan`，按显式 `SemanticAction` 执行最小 DAG，不再通过同步临时树重新发现资源 diff。
- embedding processor 执行明确的 `IndexAction`：embed/upsert、update fields 或 delete。

这样 planning 和 execution 分离。no-op 也变得可观测：如果 `ContextUpdatePlan` 没有 `content_tree_actions`、没有 `semantic_plan`、没有 `direct_index_actions`，就不应该入 semantic 或 embedding 队列。

流程里涉及的核心对象如下，详细定义见第 5 章：

| 名词 | 简要含义 |
| --- | --- |
| `ParseOutputStore` | parser 产物的统一读写接口，屏蔽 local/AGFS 后端差异。 |
| `ParseArtifactRef` | parser 产物的可序列化引用，包含 backend 和 root。 |
| `Artifact Manifest` | parser 产物的文件清单和内容指纹，是 N 快照的基础。 |
| `R/N/F/V Snapshot` | 生成最终 diff 事实的四类输入快照。 |
| `ResourceDiffResult` | 描述每个路径的最终内容状态和索引状态，不包含待执行动作。 |
| `ContextUpdatePlan` | 一次 add_resources 的最终执行计划，包含内容树动作、语义计划和直接索引动作。 |
| `ContentTreeAction` | 同步修改正式内容树的动作。 |
| `SemanticPlan` | 描述 semantic DAG 需要执行的最小树、语义动作和节点级索引槽。 |
| `IndexAction` | 向量队列执行的 `embed_and_upsert`、`update_fields`、`delete`。 |

local parse output 的关键边界可以简化为：

```text
parser 写本地 artifact
  -> resource processor 在同一次请求内立即读取 artifact
  -> ContextUpdatePlan 执行时只把变化文件写入正式 AGFS
  -> semantic message 只携带 SemanticPlan 和正式资源 URI
```

因此，多机部署也可以使用 local parse output，前提是 local artifact 在异步入队前已经被消费；任何跨队列传递的 artifact ref 都必须指向 AGFS 等远程持久化存储。

## 5. 核心数据结构

本章单独说明新链路中的核心结构。每个结构都按“定义、说明、示例”展开，便于评审时判断字段边界、序列化边界和下游消费方式。

### 5.1 ParseOutputStore

**定义**

`ParseOutputStore` 是 parser 写入解析产物的统一接口。它不关心上层是否在做 initial、no-op 还是增量修改，只负责把 parser 产物按 artifact-relative path 写入某个后端，并提供后续读取、列举、manifest 和清理能力。

实现包括：

- AGFS store：把解析产物写到远程临时存储。
- Local store：在请求同步阶段把解析产物写到本地目录。

两种 backend 应提供相同基础能力：创建 artifact、写 bytes、读 bytes、列举 entry、读写 manifest 和 cleanup。

**说明**

这个抽象的目标是让 parser 不再直接判断“应该写本地还是写 AGFS”。parser 只拿到一个 store，然后按相同接口输出文件。后续 ResourceProcessor 根据 `ParseArtifactRef.backend` 读取 artifact，构造 N 快照，并进一步生成 `ResourceDiffResult` 和 `ContextUpdatePlan`。

解析产物和正式资源的边界如下：

| local artifact 中的内容 | 是否进入正式远程 AGFS | 进入时机 |
| --- | --- | --- |
| 新增文件（N 有、F 无） | 是 | `ContextUpdatePlan` 生成 `ContentTreeAction.upsert` 后，在资源提交阶段上传。 |
| 内容修改文件（N/F 同为 file，MD5 不同） | 是 | `ContentTreeAction.upsert` 用 local artifact 中的最终 bytes 覆盖正式文件。 |
| 文件存在但索引缺失 | 文件内容通常不需要重复上传 | `ResourceDiffResult` 表示 index missing 后，直接使用正式文件补索引；只有正式文件本身不一致时才从 artifact 上传。 |
| 内容未变文件（MD5 相同） | 否 | 只使用其 manifest 和向量库旧状态，不上传文件 bytes。 |
| 删除文件 | 不上传 | 删除动作作用于正式远程资源和对应向量，不从 local artifact 上传。 |
| 文件/目录结构冲突 | 新类型对应的内容会上传 | 先处理旧类型冲突，再将新类型中需要的文件写入正式资源。 |
| `.overview.md`、`.abstract.md` 等控制/派生文件 | 按正式资源语义处理，不作为普通业务文件逐个 diff | 目录语义提交阶段生成或更新；不把它们当作源文件参与普通文件删除。 |
| `.artifact_manifest.json` | 不作为业务资源文件 | 只服务于解析产物完整性和 diff，是否留在正式树由控制文件规则决定。 |

因此 local 模式不是“local artifact 全量上传后再走旧流程”，也不是“所有内容都永远留在本地”：local artifact 先在请求内作为完整 N 快照存在，只有 `ContentTreeAction` 需要提交的文件才进入正式 AGFS；`SemanticPlan` 入队后不再依赖 local artifact。

**示例**

```python
store = LocalParseOutputStore(local_root="/tmp/openviking/parse-output")
artifact_ref = await store.create_artifact(root_type="dir")

await store.write_bytes(artifact_ref, "repository/pkg/a.py", b"def add(a, b): ...")
await write_artifact_manifest(
    store,
    artifact_ref,
    {"repository/pkg/a.py": "6f1ed002ab5595859014ebf0951522d9"},
)
```

### 5.2 ParseArtifactRef

**定义**

`ParseArtifactRef` 是一个解析产物的可序列化引用。它描述 artifact 存在哪个 backend、根路径是什么、资源根在 artifact 内的相对位置是什么。

```python
ParseArtifactRef(
    backend="local" | "agfs",
    root="...",
    resource_rel="",
    root_type="dir" | "file",
)
```

**说明**

规则：

- 在请求同步阶段内，local ref 可以被同一个请求处理链路消费。
- 跨异步队列边界时，artifact ref 必须指向远程持久化文件。
- 下游必须根据 `artifact_ref.backend` 选择 store，不能只依赖全局配置推断。

`resource_rel` 用于处理 parser 产物中存在包裹目录的情况。例如代码仓库 parser 可能把真实仓库内容放在 artifact 的 `repository/` 子目录下，而正式资源树 target 下不应该再带这一层。

**示例**

```json
{
  "backend": "local",
  "root": "/tmp/openviking/parse-output/9c5f...",
  "resource_rel": "repository",
  "root_type": "dir"
}
```

如果 backend 是 AGFS，同一结构可以表示为：

```json
{
  "backend": "agfs",
  "root": "viking://temp/9c5f...",
  "resource_rel": "repository",
  "root_type": "dir"
}
```

### 5.3 Artifact Manifest

**定义**

Artifact Manifest 是 parser 产物里的内容指纹清单，当前实际格式是 artifact-relative path 到 MD5 的映射。

```json
{
  "repository/pkg/a.py": "6f1ed002ab5595859014ebf0951522d9",
  "repository/pkg/b.py": "2c1743a391305fbf367df8e4f069f9f9"
}
```

**说明**

这里的 MD5 表示最终会提交到正式资源树的文件 bytes。它不是源文件 MD5、不是摘要 MD5，也不是 embedding 输入 MD5。MD5 由 parser 输出通道在写入最终 bytes 时同步计算，并写入 `.artifact_manifest.json`；目录类型不存放在 manifest 文件里，而是在构造 N 快照时通过 `store.list()` 遍历 artifact 树得到。构造 N 快照时会结合 `resource_rel` 把 `repository/pkg/a.py` 转成正式资源树下的相对路径 `pkg/a.py`。

用途：

- 当向量记录已有匹配 MD5 时，避免读取目标文件正文。
- 将最终内容指纹写入向量标量字段。
- 让资源提交和向量元数据围绕同一份已提交 bytes 对齐。

**示例**

基于上面的 manifest 和 `resource_rel="repository"`，N 快照中的业务文件会变成：

```python
{
    "pkg/a.py": NewEntry(md5="6f1ed002ab5595859014ebf0951522d9", is_dir=False),
    "pkg/b.py": NewEntry(md5="2c1743a391305fbf367df8e4f069f9f9", is_dir=False),
}
```

### 5.4 R/N/F/V Snapshot

**定义**

R/N/F/V Snapshot 是生成最终 diff 事实前的四类输入视图。它们把“本次请求想做什么”“本次解析出了什么”“正式资源树现在有什么”“向量库现在有什么”拆开表达，避免下游只凭文件树或临时目录做推断。

| 输入 | 来源 | 生成时机 | 主要内容 |
| --- | --- | --- | --- |
| R 请求意图 | add_resources 请求参数和 IngestOptions | 请求开始时解析并规范化 | target、tags/search_tags、tag_mode、ACL/owner 相关意图、processing_mode。 |
| N 新解析产物 | ParseOutputStore 和 artifact manifest | parser 完成最终产物写入、图片/路径等后处理完成后 | 相对路径、file/directory 类型、最终 bytes、MD5、artifact-relative 读取位置。 |
| F 正式文件树 | 正式 AGFS target 的完整 tree snapshot | 目标 URI 确定并持有资源锁后 | 正式路径是否存在、file/directory 类型、扫描是否完整。 |
| V 向量库快照 | 向量库目标目录范围的完整 inventory | 与 F 同一资源操作边界内读取 | record id、canonical URI、level、md5；后续按需补 abstract、tags、ACL 等非向量标量。 |

**说明**

四类输入的来源和职责不同。首次导入通常没有旧 F，V 仍可用于发现遗留索引；后续导入必须同时读取 N/F/V，才能区分 no-op、内容变化、索引修复和孤儿索引。

V 快照按用途分阶段读取，不预加载 dense vector：

- `ResourceDiffResult` 生成前，按目标目录范围读取轻量 inventory，主要包含 record id、URI、level 和 MD5，用来判断内容是否变化、索引是否缺失以及是否存在孤儿向量。
- `SemanticPlan` 裁剪后，只对 DAG 需要依赖的 record id 回填 abstract、tags/search_tags、ACL/owner 等标量字段，用于复用旧摘要和继承业务元数据。
- 纯标量更新真正写入向量库时，如果底层 backend 需要原向量或完整记录，再由 embedding/vector 执行阶段按 record id 读取一次。

不在 diff 阶段预加载 dense vector 是有意取舍：大目录下每条向量都可能很大，提前把向量数组放进 `ResourceDiffResult`、`ContextUpdatePlan` 或队列消息，会显著增加内存占用和序列化成本。

**示例**

```python
request_intent = {
    "target_uri": "viking://resources/repo",
    "processing_mode": "semantic_and_vectors",
    "tags": ["service:api"],
    "tag_mode": "merge",
}

new_snapshot = {
    "src/api/router.py": NewEntry(md5="9b74c9897bac770ffc029102a200c5de", is_dir=False),
    "src/api/schema.py": NewEntry(md5="e358efa489f58062f10dd7316b65649e", is_dir=False),
}

formal_tree_snapshot = {
    "src/api/router.py": TargetFile(is_dir=False),
    "src/api/schema.py": TargetFile(is_dir=False),
    "docs/guide.md": TargetFile(is_dir=False),
}

vector_snapshot = {
    "src/api/router.py": TargetVector(md5="old-md5", abstract="Old router implementation."),
    "src/api/schema.py": TargetVector(
        md5="e358efa489f58062f10dd7316b65649e",
        abstract="schema.py defines request and response schemas.",
    ),
    "legacy/old.py": TargetVector(md5="abandoned-md5", abstract="Old removed file."),
}
```

这里 `router.py` 会进入内容修改判断，`schema.py` 可以判定内容未变，`docs/guide.md` 可能进入删除判断，`legacy/old.py` 是向量库快照中暴露的孤儿索引候选。

### 5.5 ResourceDiffResult

**定义**

`ResourceDiffResult` 是 R/N/F/V 比较后的最终事实结果。它只回答“每个路径发生了什么”和“索引当前是什么状态”，不描述如何执行。执行行为由后续 `ContextUpdatePlan` 生成。

```python
class ContentState(str, Enum):
    UNCHANGED = "unchanged"
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RESTORE = "restore"
    REPLACE_KIND = "replace_kind"
    ABSENT = "absent"


class IndexState(str, Enum):
    ABSENT = "absent"
    COMPLETE = "complete"
    MISSING = "missing"
    PARTIAL = "partial"
    STALE = "stale"
    ORPHAN = "orphan"
    LEVEL_CONFLICT = "level_conflict"


@dataclass(frozen=True)
class ResourceDiffEntry:
    relative_path: str
    content_state: ContentState
    index_state: IndexState
    old_kind: Literal["file", "directory"] | None = None
    new_kind: Literal["file", "directory"] | None = None
    md5: str | None = None
```

**说明**

`ResourceDiffResult` 不是执行计划，也不进入队列。它的价值是把 RNFV 判断和行为映射拆开，便于测试“状态判定是否正确”。旧 `DiffPlan.needs_body_compare` 不应成为状态：当 N/V 的 MD5 不足以判断时，diff resolver 在本阶段直接有限并发读取 N/F 正文，现场归并为 `UNCHANGED` 或 `MODIFIED`。

R 会参与 diff：内容未变但 tags/search_tags 变化时，`content_state` 仍是 `UNCHANGED`，但后续会生成 `UPDATE_FIELDS`；文件存在但 L2 缺失时，`content_state` 可能是 `UNCHANGED`，`index_state` 是 `MISSING`。

文件节点的常见决策如下：

| N | F | V | 比较结果 | 最终状态 | 后续行为摘要 |
| --- | --- | --- | --- | --- | --- |
| 文件 | 文件 | 正确 L2 | same | `UNCHANGED + COMPLETE` | 无动作；若作为目录聚合输入则语义 `REUSE`。 |
| 文件 | 文件 | 正确 L2 | diff | `MODIFIED + STALE` | `ContentTreeAction.UPSERT`；语义 `GENERATE`；按策略更新 L2。 |
| 文件 | 文件 | 无 L2 | same | `UNCHANGED + MISSING` | 不写文件；按正文或摘要策略补建 L2。 |
| 文件 | 文件 | 错误 L0/L1 | same | `UNCHANGED + LEVEL_CONFLICT` | 删除错误层级；补建 L2。 |
| 文件 | 无 | 无 | - | `ADDED + MISSING` | 写文件；语义 `GENERATE`；新建 L2。 |
| 文件 | 无 | 正确 L2 | N.md5 = V.md5 | `RESTORE + COMPLETE` | 恢复正式文件；可复用旧 L2 摘要。 |
| 文件 | 无 | 正确 L2 | N.md5 != V.md5 | `RESTORE + STALE` | 恢复正式文件；重新生成语义和 L2。 |
| 无 | 文件 | 正确 L2 | - | `DELETED + COMPLETE` | 删除文件；直接删除 L2；父目录聚合。 |
| 无 | 无 | L2 | - | `ABSENT + ORPHAN` | 不碰文件树；直接删除孤儿向量。 |

目录节点的状态由子树和目录层级索引共同决定。目录自身存在不代表语义无变化；只要直接或间接子节点发生新增、修改、删除，相关祖先目录就需要进入最小语义闭包。

### 5.6 ContextUpdatePlan

**定义**

`ContextUpdatePlan` 是本次请求最终可执行计划。它由 `ResourceDiffResult + RequestIntent` 生成，包含三类互相解耦的行为。

```python
@dataclass(frozen=True)
class ContextUpdatePlan:
    root_uri: str
    context_type: str
    content_tree_actions: tuple[ContentTreeAction, ...]
    semantic_plan: SemanticPlan | None
    direct_index_actions: tuple[IndexAction, ...]
```

**说明**

`ContextUpdatePlan` 是唯一进入执行器的上层计划。`ResourceDiffResult` 只提供事实，不能和它一起成为下游执行依据。执行顺序固定为：

```text
validate ContextUpdatePlan
  -> 同步执行全部 ContentTreeAction
  -> 正式内容树提交成功
  -> direct_index_actions 直接进入 embedding queue
  -> semantic_plan 进入 semantic queue
```

如果没有任何内容树动作、没有语义计划、没有 direct index action，则这是健康 no-op，不应入任何下游队列。

### 5.7 ContentTreeAction

**定义**

`ContentTreeAction` 描述正式内容树中的文件或目录变化。它只作用于正式 AGFS 资源树，不直接操作语义摘要或向量库。

```python
class ContentTreeOperation(str, Enum):
    UPSERT = "upsert"
    DELETE = "delete"
    REPLACE_KIND = "replace_kind"


@dataclass(frozen=True)
class ContentTreeAction:
    operation: ContentTreeOperation
    relative_path: str
    old_kind: Literal["file", "directory"] | None = None
    new_kind: Literal["file", "directory"] | None = None
    artifact_path: str | None = None
    md5: str | None = None
```

**说明**

- `UPSERT`：从 local/AGFS parse artifact 读取最终 bytes，写入正式资源树。
- `DELETE`：删除正式资源树中的文件或目录。
- `REPLACE_KIND`：处理同路径 file/directory 类型转换，先删除旧类型，再创建新类型。

`ContentTreeAction` 必须在当前请求同步阶段、持有资源锁时执行。只有它们全部成功后，才能分发 semantic 和 index 的异步动作。内容提交后不再把 parse artifact 交给队列；artifact cleanup 是 best-effort，失败只记录告警，不能阻断已经确定的异步派生动作。

### 5.8 SemanticPlan、SemanticAction 与 IndexSlot

**定义**

`SemanticPlan` 是进入 semantic queue 的可序列化计划。它只描述语义 DAG 的最小闭包和依赖语义结果的索引行为；纯向量删除、纯标量更新不进入 `SemanticPlan`。

```python
class SemanticAction(str, Enum):
    REUSE = "reuse"
    GENERATE = "generate"
    AGGREGATE = "aggregate"


class SemanticOutputCondition(str, Enum):
    OUTPUT_READY = "output_ready"
    OUTPUT_CHANGED = "output_changed"


class IndexOperation(str, Enum):
    NONE = "none"
    UPSERT = "upsert"
    DELETE = "delete"
    UPDATE_FIELDS = "update_fields"


@dataclass(frozen=True)
class IndexSlot:
    level: int
    record_id: str
    existing_fields: Mapping[str, Any] | None
    operation: IndexOperation = IndexOperation.NONE
    trigger: SemanticOutputCondition | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)
    fallback_update_fields: bool = False


@dataclass(frozen=True)
class SemanticTreeEntry:
    relative_path: str
    kind: Literal["file", "directory"]
    content_state: ContentState
    semantic_action: SemanticAction
    md5: str | None = None
    index_slots: tuple[IndexSlot, ...] = ()


@dataclass(frozen=True)
class SemanticPlan:
    root_uri: str
    context_type: str
    tree: SemanticTreeSnapshot
    vectorize: bool = True
    file_vector_source: FileVectorSource = FileVectorSource.CONTENT
    ingest_options: IngestOptions = field(default_factory=IngestOptions)
    source_metadata: dict[str, str] | None = None
```

**说明**

`IndexSlot` 合并了旧方案里的 `indexed_records` 和节点级 `IndexAction`，避免在队列消息里重复保存 `record_id/level/uri/abstract`。它表达的是“当前节点某个 level 的索引槽位”：当前是否已有记录、仍需要哪些旧字段、本次语义完成后要不要更新这个槽位。

- URI 不存入 `IndexSlot`，由 `SemanticPlan.root_uri + SemanticTreeEntry.relative_path` 推导。
- `level` 只存一处。
- `record_id` 来自向量库快照；新增记录由 plan builder 分配一次稳定 ID，保证重试不换 ID。
- `existing_fields` 只保留执行仍需要的旧标量，如 `abstract`、`search_tags`；不保存 dense vector、sparse vector 或大段 content。
- `fields` 是本次请求要求覆盖的最终字段，不是旧字段副本。

语义执行规则：

| `SemanticAction` | 行为 |
| --- | --- |
| `REUSE` | 不调用模型，从 `IndexSlot.existing_fields["abstract"]` 读取旧摘要作为 DAG 输入。 |
| `GENERATE` | 对文件正文生成新摘要。 |
| `AGGREGATE` | 等待直接子项摘要就绪后生成目录 overview/abstract。 |

索引触发规则：

| trigger | 行为 |
| --- | --- |
| `OUTPUT_READY` | 只要语义结果可用就执行，适用于新增和索引缺失 repair。 |
| `OUTPUT_CHANGED` | 只有新旧摘要变化才执行，适用于已有索引的摘要向量更新优化。 |

当 `OUTPUT_CHANGED` 未命中、但文件 MD5 或请求标量仍需写回时，
`fallback_update_fields=True` 会降级为不调用 embedding 模型的字段更新。目录节点也使用同一规则回写待更新标量。
`IndexSlot` 只允许 `NONE` 和 `UPSERT`；`DELETE` 与不依赖语义结果的 `UPDATE_FIELDS` 必须作为 direct `IndexAction` 分流。

`REUSE` 的不变量是：对应 level 的 `IndexSlot.existing_fields` 中必须有可用 `abstract`。如果旧摘要缺失，plan builder 应把该节点提升为 `GENERATE` 或 `AGGREGATE`，不能把空字符串当成有效摘要。
非空 `SemanticPlan` 还必须包含 `relative_path=""` 的资源根，并包含每个保留节点的父目录；否则反序列化时直接拒绝，避免 worker 收到无法遍历的断开图。

### 5.9 Direct IndexAction 与向量写入语义

**定义**

`IndexAction` 是不依赖语义结果、可以直接进入 embedding queue 的索引操作。它必须自包含目标 URI、level 和 record ID，因为它不挂在某个 `SemanticTreeEntry` 下。

```python
@dataclass(frozen=True)
class IndexAction:
    operation: IndexOperation
    uri: str
    level: int
    record_id: str
    fields: Mapping[str, Any] = field(default_factory=dict)
    md5: str | None = None
```

**说明**

| operation | 是否调用向量模型 | 使用场景 |
| --- | ---: | --- |
| `UPSERT` | 是 | `vectors_only` 或无需语义摘要的正文向量化。 |
| `UPDATE_FIELDS` | 否 | 内容未变，但 tags/search_tags 等请求标量变化。 |
| `DELETE` | 否 | 文件删除、孤儿索引、结构变化产生的旧层级冲突记录。 |

`UPDATE_FIELDS` 不预先携带 dense vector。执行时如果后端需要完整旧记录，由 embedding/vector 写入阶段根据 `record_id` 再读一次向量库，合并白名单字段后写回。这是为了控制 `ContextUpdatePlan` 和队列消息大小。

向量写入正确性原则：

- same-level 内容更新应 upsert 现有向量记录，不先删除。
- 已有记录更新必须使用 V 快照读出的真实 `record_id`。
- 文件和目录互换时，删除旧层级记录，给新层级使用对应 upsert。
- 一个请求内同一 `record_id` 不应同时出现互相冲突的动作；执行器发现冲突应视为 plan builder bug。
- 文件提交成功但向量更新失败时，系统可能保留文件/向量不一致。本方案接受这个取舍，不承诺任意一次后续导入都能自动发现并修复；需要通过显式 reindex/repair 恢复。特别是 N 与 V 的 MD5 相同但 F 正文不同的组合，为避免 no-op 再读取远程正文，本方案仍按 `UNCHANGED` 处理。

### 5.10 最小 DAG 闭包与执行过程

假设完整正式树如下，本次请求有三类变化：`src/api/router.py` 修改，`docs/guide.md` 删除，`tests/unit/test_api.py` 新增；`src/utils`、`examples` 和 `tests/integration` 没有任何变化。

裁剪前的资源树和状态：

```text
repo/
├── src/
│   ├── api/
│   │   ├── router.py       # modified
│   │   └── schema.py       # unchanged
│   ├── utils/              # unchanged subtree
│   │   ├── dates.py
│   │   └── strings.py
│   └── README.md           # unchanged
├── docs/
│   ├── guide.md            # deleted
│   └── design.md           # unchanged
├── tests/
│   ├── unit/
│   │   ├── test_api.py     # added
│   │   └── test_utils.py   # unchanged
│   └── integration/        # unchanged subtree
│       └── test_flow.py
└── examples/               # unchanged subtree
    └── quickstart.py
```

裁剪后的 SemanticPlan tree：

```text
repo/                    AGGREGATE
├── src/                 AGGREGATE
│   ├── api/             AGGREGATE
│   │   ├── router.py    GENERATE
│   │   └── schema.py    REUSE
│   ├── utils/           REUSE，不展开后代
│   └── README.md        REUSE
├── docs/                AGGREGATE
│   └── design.md        REUSE
├── tests/               AGGREGATE
│   ├── unit/            AGGREGATE
│   │   ├── test_api.py  GENERATE
│   │   └── test_utils.py REUSE
│   └── integration/     REUSE，不展开后代
└── examples/            REUSE，不展开后代
```

`docs/guide.md` 已被删除，不进入新的语义树；它的 L2 删除是 direct `IndexAction.delete`。父目录 `docs/` 会基于剩余直接子项重新聚合。未变化兄弟目录 entry 自身保留旧 L0 摘要，但不会展开其后代。

最小闭包的构造规则：

1. 从 `ResourceDiffResult` 中收集所有会影响语义的变化路径，例如新增、修改、删除、类型转换、索引缺失且需要摘要修复的路径。
2. 对每个变化路径补齐必要祖先目录，直到 `root_uri`。这些祖先目录通常会生成 `SemanticAction.AGGREGATE`。
3. 对每个需要聚合的目录，保留它当前正式树中的全部直接子项。直接子项可以是文件或目录；如果子项自身无变化，则使用 `SemanticAction.REUSE`。
4. 未变化兄弟目录只作为摘要叶子保留，不展开后代。例如 `src/utils` 只需要旧 L0 abstract，不需要把 `dates.py` 和 `strings.py` 放进队列消息。
5. 已删除节点不进入新的语义树。它只影响父目录成员集合，并通过 direct `IndexAction.delete` 清理旧索引。
6. 如果某个 `REUSE` 节点缺少可用旧 abstract，plan builder 不能把空摘要当成依赖输入；应把该节点提升为 `GENERATE` 或 `AGGREGATE`，或者显式失败并要求 reindex。

DAG 执行流程：

1. Semantic worker 反序列化 `SemanticPlan` 后，只根据 `tree.entries` 构造内存邻接表，不重新 `ls/tree` 扫描完整资源树。
2. `REUSE` 节点初始化为已完成，摘要来自对应 `IndexSlot.existing_fields["abstract"]`。
3. `GENERATE` 文件节点读取正式 AGFS 中当前文件正文，生成新文件摘要，并与旧 L2 abstract 比较。
4. `AGGREGATE` 目录节点等待全部直接输入就绪：变化文件的新摘要、变化子目录的新摘要、未变化文件或兄弟目录的旧摘要。等待完成后，按现有采样策略生成目录 overview/abstract。
5. 每个节点完成后检查自身 `IndexSlot`：`OUTPUT_READY` 在语义输出可用时释放；`OUTPUT_CHANGED` 只有新旧摘要变化时释放。释放后的操作进入 embedding queue。
6. 语义节点失败时，依赖该节点新语义结果的 `IndexSlot` 不入队。父目录若能使用旧摘要降级，则继续；没有可用摘要时，该输入应被明确忽略或使当前聚合失败，不能静默使用空摘要。
7. 父目录传播仍使用现有 freshness 机制。当前资源内部由最小闭包执行；是否继续刷新资源外部父目录，由目录摘要是否变化和 freshness 策略共同决定。

这套执行方式保留 DAG 的自底向上依赖关系，但把“访问哪些节点”和“每个节点做什么”前移到 `SemanticPlan`。Semantic worker 不再根据 `added/modified/deleted` 自行推断业务行为，也不再递归访问无关子树。

### 5.11 ContextUpdatePlan 示例

下面示例省略非关键字段，展示三类行为如何分开：

```json
{
  "root_uri": "viking://resources/repo",
  "context_type": "resource",
  "content_tree_actions": [
    {
      "operation": "upsert",
      "relative_path": "src/api/router.py",
      "new_kind": "file",
      "artifact_path": "repository/src/api/router.py",
      "md5": "9b74c9897bac770ffc029102a200c5de"
    },
    {
      "operation": "delete",
      "relative_path": "docs/guide.md",
      "old_kind": "file"
    }
  ],
  "direct_index_actions": [
    {
      "operation": "delete",
      "uri": "viking://resources/repo/docs/guide.md",
      "level": 2,
      "record_id": "l2-guide"
    },
    {
      "operation": "delete",
      "uri": "viking://resources/repo/legacy/old.py",
      "level": 2,
      "record_id": "l2-legacy-old"
    }
  ],
  "semantic_plan": {
    "root_uri": "viking://resources/repo",
    "context_type": "resource",
    "vectorize": true,
    "tree": {
      "entries": [
        {
          "relative_path": "",
          "kind": "directory",
          "content_state": "unchanged",
          "semantic_action": "aggregate",
          "index_slots": [
            {
              "level": 0,
              "record_id": "l0-root",
              "existing_fields": {"abstract": "Old repository summary."},
              "operation": "upsert",
              "trigger": "output_changed"
            }
          ]
        },
        {
          "relative_path": "src",
          "kind": "directory",
          "content_state": "unchanged",
          "semantic_action": "aggregate",
          "index_slots": [
            {
              "level": 0,
              "record_id": "l0-src",
              "existing_fields": {"abstract": "Old source summary."},
              "operation": "upsert",
              "trigger": "output_changed"
            }
          ]
        },
        {
          "relative_path": "src/api",
          "kind": "directory",
          "content_state": "unchanged",
          "semantic_action": "aggregate",
          "index_slots": [
            {
              "level": 0,
              "record_id": "l0-api",
              "existing_fields": {"abstract": "Old API summary."},
              "operation": "upsert",
              "trigger": "output_changed"
            }
          ]
        },
        {
          "relative_path": "src/api/router.py",
          "kind": "file",
          "content_state": "modified",
          "semantic_action": "generate",
          "md5": "9b74c9897bac770ffc029102a200c5de",
          "index_slots": [
            {
              "level": 2,
              "record_id": "l2-router",
              "existing_fields": {
                "abstract": "Old router implementation."
              },
              "operation": "upsert",
              "trigger": "output_changed",
              "fields": {
                "search_tags": ["service:api"]
              }
            }
          ]
        },
        {
          "relative_path": "src/api/schema.py",
          "kind": "file",
          "content_state": "unchanged",
          "semantic_action": "reuse",
          "index_slots": [
            {
              "level": 2,
              "record_id": "l2-schema",
              "existing_fields": {
                "abstract": "schema.py defines request and response schemas."
              },
              "operation": "none"
            }
          ]
        }
      ]
    },
    "file_vector_source": "summary_when_available"
  }
}
```

## 6. 配置与部署

示例配置：

```json
{
  "server": {
    "temp_upload": {
      "default_mode": "shared"
    }
  },
  "storage": {
    "parse_output": {
      "mode": "local"
    }
  }
}
```

含义：

- `temp_upload.default_mode=shared` 避免把已上传的 source 内容再复制到第二份 source 暂存。
- `storage.parse_output.mode=local` 在请求同步阶段把 parser artifact 写到本地。
- 正式资源仍提交到 AGFS。
- semantic 和 embedding worker 读取正式资源和序列化 plan，不读取本地 parser artifact。

部署结论：

> 多机部署可以使用 local parse output，前提是 local artifact 在异步入队前已被消费。任何跨队列的 artifact ref 都必须指向远程持久化存储。

## 7. 可观测性

每次 `add_resources` 应输出足够信息，判断增量是否生效。

初次导入日志：

- root URI。
- committed file count。
- committed directory count。

增量 diff 日志：

- `ResourceDiffResult` 中各 `ContentState` 数量。
- `ResourceDiffResult` 中各 `IndexState` 数量。
- scalar-only 变化数量。
- 触发正文回退比较的文件数量及有限并发比较结果。

计划分发日志：

- `ContentTreeAction` 数量，按 `upsert/delete/replace_kind` 分组。
- `direct_index_actions` 数量，按 `upsert/update_fields/delete` 分组。
- `SemanticPlan` 是否生成。
- `SemanticTreeEntry` 数量，按 `reuse/generate/aggregate` 分组。
- `IndexSlot` 数量，按 `none/upsert` 和 `output_ready/output_changed` 分组。

队列和 benchmark 指标：

- semantic message count。
- embedding message count。
- file summary count。
- directory overview count。
- vector upsert/update/delete count。
- stage-level wall time。
- process RSS peak。
- local disk peak。

关键运营信号不只是 wall time。更确定的信号是工作量：健康 no-op 应产生 0 个 semantic 和 embedding 工作项。

## 8. 正确性验证

必须覆盖的场景：

| 场景 | 期望结果 |
| --- | --- |
| initial | 正式文件、L2 记录和目录记录完整生成。 |
| no-op | 不写资源、不执行 semantic、不执行 embedding。 |
| edit-one | 只处理变化文件和必要父目录。 |
| edit-10% | 工作量随变化文件和受影响目录增长。 |
| tags-only | 内容未变，但向量标量字段更新。 |
| delete | 正式文件和向量记录删除。 |
| orphan vector | 没有文件的向量记录被删除。 |
| repair index | 有文件但无向量记录时补索引。 |
| structural change | 文件/目录冲突删除旧层级脏记录。 |
| legacy missing fields | 缺少新增元数据时回退保守比较或修复。 |

每个场景应验证：

- 正式文件 URI 集合。
- 正式文件 bytes。
- L2 URI 集合。
- L2 内容指纹。
- L0/L1/L2 数量。
- tags 和 ACL 相关标量字段。
- semantic 和 embedding 队列工作量。
- `ContextUpdatePlan` 是否符合预期：no-op 必须没有 content tree action、没有 semantic plan、没有 direct index action。
- `IndexSlot` 是否只携带必要旧字段，不重复携带 URI/level/record 身份。

## 9. 性能评测

评测维度：

| 维度 | 取值 |
| --- | --- |
| 实现模式 | baseline、optimized AGFS、optimized local |
| 场景 | initial、no-op、edit-one、edit-10% |
| 数据集 | 中等规模仓库、真实较大代码仓库 |
| 后端 | 远程 AGFS/S3 正式存储、固定向量库后端 |
| 隔离 | 独立 account、user、target URI、vector collection 或 namespace |

指标：

- 端到端耗时。
- 阶段耗时：source、parse、artifact persist、diff/apply、semantic DAG、embedding、cleanup。
- 工作量：文件数、目录数、semantic task、embedding task、vector upsert/update/delete。
- 资源成本：RSS 峰值和本地磁盘峰值。
- 正确性 diff：missing/unexpected files、content mismatch、missing vectors、MD5 mismatch。

评测结果摘要如下，时间均为端到端耗时：

| 数据集 | 对比口径 | initial | no-op | edit-one | edit-10% | 正确性结论 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 642 文件 OpenViking Python 子集 | baseline shared-only vs SemanticPlan + local | 387.68s -> 133.89s | 433.15s -> 10.76s | 437.99s -> 9.79s | 619.85s -> 130.44s | local 场景通过；baseline edit-10% 失败。 |
| 867 文件 third_rank_service | main vs final local，中位数 | 1797.871s -> 1234.737s | 1653.903s -> 178.218s | 1696.052s -> 233.918s | 1862.750s -> 395.633s | final local 12/12 通过；main 多个增量场景出现缺文件或缺 L2。 |

### 9.1 642 文件早期对照

**实验环境与请求口径**

642 文件评测使用 OpenViking Python 源码集：642 个文件、7,061,967 bytes、188,387 行。每组都通过 HTTP 调用 `add_resources`，不使用 Rust CLI；输入先通过 `POST /api/v1/resources/temp_upload` 以 shared 模式上传，再调用 `POST /api/v1/resources` 导入固定 target，使用 `wait=true` 和 `processing_mode=semantic_and_vectors`。每个场景使用独立 account、user、target URI 和向量库 namespace。

三组共同后端为：正式资源文件系统使用远程 TOS/S3，向量库使用本地向量库。VLM 使用 `doubao-seed-1-6-flash-250828`，并发 32；embedding 使用 `doubao-embedding-vision-250615`，并发 10。差异只有代码版本和解析产物模式：

| 组别 | 代码版本 | 解析产物模式 |
| --- | --- | --- |
| 优化前 baseline | shared-only 基线 `6e04f20ed` | AGFS temp |
| 优化版 AGFS | SemanticPlan 版本 `17e62a015` | AGFS temp |
| 优化版 local | 同一 SemanticPlan 版本 | local parse output |

场景覆盖 `initial`、`no-op`、`edit-one`、`edit-10%`；每个场景为单样本，结果用于阶段归因和工作量对比，不宣称统计显著性。

| 场景 | Baseline shared-only | SemanticPlan + AGFS | SemanticPlan + local |
| --- | ---: | ---: | ---: |
| initial | 387.68s | 391.83s | 133.89s |
| no-op | 433.15s | 343.20s | 10.76s |
| edit-one | 437.99s | 336.41s | 9.79s |
| edit-10% | 619.85s，正确性失败 | 448.19s | 130.44s |

解释：

- SemanticPlan 消除了 no-op 的 semantic 和 embedding 工作。
- local parse output 消除了主要的 AGFS temp artifact 写入成本。
- 较大比例修改时，语义和模型调用重新成为主要成本。
- baseline edit-10% 暴露正确性失败，应作为诊断证据，不作为有效性能样本。

### 9.2 867 文件最终三轮验收

**实验环境与请求口径**

最终验收使用真实 `third_rank_service` Git 仓库，在站内向量库环境做三轮对照。两组都通过 HTTP 调用 `add_resources`，不使用 Rust CLI；每轮创建新的 account、user、target URI 和独立服务环境，按 `initial -> no-op -> edit-one -> edit-10%` 顺序执行。输入通过 shared temp upload 进入服务，再由 `add_resources` 消费。

正式资源文件系统使用远程 TOS/S3。优化版使用 local parse output，解析产物在请求同步阶段写本地，正式文件仍写 TOS/S3；main 基线使用旧 AGFS temp 解析产物路径。向量库使用站内远程向量库，不是本地向量库。两组使用相同的 embedding/VLM 模型配置、模型版本和并发配置；当前留存的实验环境摘要未记录具体模型名称。

版本对照为：优化版 `ad465f97101e27f13197bf88b29472532b43a4f1`，main 基线 `192b813e7e3106680a5534e2d4c9bcf6d2390abd`。每个场景都校验正式文件 URI、逐文件正文、L2 URI、向量 identity 和 MD5。

输入规模：

- 867 个文件。
- 10,635,180 bytes。
- 199,807 行。
- manifest SHA-256 为 `bf9d835a1c635e08c4cd1dbb824875e549834ff5ddbde0baa8190653ab339d16`。
- `edit-one` 修改 `BASE_BUILD.py`。
- `edit-10%` 累计修改 87 个文件。

端到端耗时中位数：

| 场景 | final local 三轮 | final local 中位数 | main 三轮 | main 中位数 | local 相对 main |
| --- | ---: | ---: | ---: | ---: | ---: |
| initial | 1281.956 / 1159.608 / 1234.737s | 1234.737s | 1772.045 / 1797.871 / 1915.498s | 1797.871s | 1.46x，减少 31.3% |
| no-op | 178.218 / 355.679 / 177.027s | 178.218s | 1572.042 / 1653.903 / 1742.917s | 1653.903s | 9.28x，减少 89.2% |
| edit-one | 219.084 / 233.918 / 239.519s | 233.918s | 1575.083 / 1696.052 / 1738.261s | 1696.052s | 7.25x，减少 86.2% |
| edit-10% | 373.421 / 395.633 / 407.972s | 395.633s | 1713.327 / 1862.750 / 2129.230s | 1862.750s | 4.71x，减少 78.8% |

服务端主要阶段中位数：

| 模式 / 场景 | 解析 | 正式树提交或快照/同步 | SemanticPlan | Semantic DAG | file summary | overview | embedding/upsert |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| final local / initial | 42.776s | local artifact -> 正式 AGFS 729.684s | 0.030s | 355.074s | 867 | 174 | 1215 / 1215 |
| final local / no-op | 43.699s | snapshot 34.860s | 0.030s | 0s | 0 | 0 | 0 / 0 |
| final local / edit-one | 41.466s | snapshot 30.071s | 0.047s | 54.776s | 1 | 2 | 3 / 3 |
| final local / edit-10% | 41.606s | snapshot 28.740s | 11.625s | 185.901s | 87 | 12 | 107 / 107 |
| main / initial | 1121.273s | persist 100.641s | - | 323.256s | 867 | 174 | 1215 / 1215 |
| main / no-op | 1103.945s | sync tree 177.401s | - | 291.014s | 866 | 93 | 742 / 742 |
| main / edit-one | 1142.379s | sync tree 157.336s | - | 309.905s | 867 | 90 | 734 / 734 |
| main / edit-10% | 1120.365s | sync tree 341.007s | - | 307.640s | 866 | 105 | 797 / 797 |

CPU、RSS 与本地磁盘中位数：

| 模式 / 场景 | CPU 时间 | CPU 利用率 | RSS 峰值 / 增量 | 本地磁盘峰值 / 增量 |
| --- | ---: | ---: | ---: | ---: |
| final local / initial | 1253.70s | 101.54% | 788.54 / 278.04 MiB | 33.38 / 21.04 MiB |
| final local / no-op | 177.81s | 99.45% | 858.93 / 19.04 MiB | 42.87 / 20.53 MiB |
| final local / edit-one | 232.28s | 99.64% | 868.86 / 6.50 MiB | 43.94 / 20.53 MiB |
| final local / edit-10% | 393.92s | 99.78% | 912.70 / 36.51 MiB | 46.71 / 21.14 MiB |
| main / initial | 1832.17s | 101.96% | 807.11 / 299.03 MiB | 24.41 / 12.06 MiB |
| main / no-op | 1685.86s | 101.93% | 948.25 / 92.59 MiB | 37.84 / 12.06 MiB |
| main / edit-one | 1728.75s | 101.86% | 1040.92 / 18.76 MiB | 51.61 / 12.06 MiB |
| main / edit-10% | 1898.73s | 101.93% | 1081.37 / 15.20 MiB | 64.35 / 12.07 MiB |

正确性结论：

| 模式 / 场景 | 有效轮次 | 正式文件 | L2 | 正文错误 | 向量缺失 | identity 错误 | MD5 missing | 非空错误 MD5 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| final local / initial | 3/3 | 每轮 867/867 | 每轮 867/867 | 0 | 0 | 0 | 0 | 0 |
| final local / no-op | 3/3 | 每轮 867/867 | 每轮 867/867 | 0 | 0 | 0 | 0 | 0 |
| final local / edit-one | 3/3 | 每轮 867/867 | 每轮 867/867 | 0 | 0 | 0 | 0 | 0 |
| final local / edit-10% | 3/3 | 每轮 867/867 | 每轮 867/867 | 0 | 0 | 0 | 0 | 0 |
| main / initial | 3/3 | 每轮 867/867 | 每轮 867/867 | 0 | 0 | 0 | 2601 | 0 |
| main / no-op | 1/3 | 合计缺 3 个 | 合计缺 3 个 | 0 | 3 | 0 | 2598 | 0 |
| main / edit-one | 2/3 | 合计缺 2 个 | 合计缺 2 个 | 0 | 2 | 0 | 2599 | 0 |
| main / edit-10% | 1/3 | 合计缺 2 个 | 合计缺 2 个 | 0 | 2 | 0 | 2599 | 0 |

final local 共 12/12 场景通过：每个场景都是 867/867 正式文件、867/867 正文和 867/867 L2，正文、identity、MD5 均无错误。三轮 edit-10% 都准确产生 87 次 file summary，最终 MD5 mismatch 为 0，说明资源内最小祖先闭包修复消除了早期深层文件旧 MD5 问题。

main 的失败表现为偶发缺少 1 到 2 个正式文件及其 L2，不是“查到非空错误 MD5”。main 不写入 MD5 字段，因此可见记录中的 MD5 被归类为 `NOT_FILLED` 或 missing；这和早期 optimized 缺祖先闭包导致“存在但非空且错误的旧 MD5”是两类不同问题。

第二轮 final local no-op 耗时 355.679s，高于另外两轮。原因是该轮 initial 后严格逐 ID 校验可见 867/867 条 L2，但紧接着 no-op 的 URI/level 范围扫描只得到 `V_vectors=862`。diff 将 862 个文件判为 unchanged，将另外 5 个正式文件判为 `vector_missing` 并进入保守 repair，触发 5 次 file summary、18 个目录节点、16 次 overview LLM，以及 31 次 embedding/upsert。该轮结束后的强校验仍为 867/867，MD5 missing 和 mismatch 都为 0；后续 edit-one 的同类范围扫描恢复为 867。这个样本说明 no-op 尾延迟仍受向量范围索引短暂可见性影响；后续可在 repair 前按稳定 ID 二次 `get`，或给范围索引增加可见性屏障/有界重试。

最终结论：

- final local 在三轮健康路径上同时满足性能和正确性要求：四个场景中位数分别比 main 快 1.46x、9.28x、7.25x 和 4.71x，12/12 场景没有非空错误 MD5。
- 完整的资源内最小祖先闭包是 edit-10% 正确性的必要条件。active ancestor 目录携带 L0+L1，未变化 sibling 目录只携带当前父目录聚合所需的 L0，文件携带 L2，整个资源从一个连通 execution root 自底向上执行。
- 第二轮 no-op 是范围索引短暂少返回 5 条导致的保守 repair，不是内容或 MD5 错误。
- 本节验证的是串行、成功完成的健康路径和实验中实际出现的索引可见性波动；并发覆盖、异步写晚到、部分写失败或异常取消窗口仍需要单独测试。

## 10. 风险与待确认问题

需要评审关注：

- Parser 覆盖：所有 parser 路径要么正确支持 `ParseOutputStore`，要么返回 backend 匹配的远程 artifact。
- 队列契约：local artifact 不得跨异步队列边界。
- 标量语义：tags、ACL 和后续 metadata 需要明确 merge/replace 行为。
- 向量库快照性能：prefix 或 DSL scan 必须分页完整。
- 清理：local artifact 清理失败不能导致磁盘无限增长。
- Embedding 消息协议：当前仍依赖 `context_data` 承载部分内部协议，后续应拆成显式字段。
- 大修改行为：影响目录很多时，最小 DAG 合理退化到接近全量成本。

## 11. 发布与回滚

发布：

- 默认保持 AGFS parse output。
- 先启用 `shared` source。
- 在受控环境启用 local parse output。
- 验证 no-op、edit-one、tags-only、delete、repair 和 structural-change 场景。
- 监控 diff 状态计数、队列工作量、向量错误率、RSS 和本地磁盘使用。

回滚：

- 将 `storage.parse_output.mode` 切回 AGFS/default。
- 对检测到的文件/向量不一致，用 reindex 或 repair 恢复。

## 12. 附录

以下内容不放在评审主路径：

- 完整 benchmark 原始输出。
- 长日志。
- 详细代码路径索引。
- 测试命令输出。
- 历史 bug 调查。
- 后续演进：显式 `EmbeddingMsg` record 协议、更广泛 parser local 支持，以及 write/batch-write/reindex plan 化。

## 13. 评审检查清单

- `R/N/F/V` 是否足以表达内容、索引和请求标量变化？
- no-op 是否能通过工作量证明，而不只看 wall time？
- local artifact 是否在异步入队前被消费？
- 跨队列 artifact ref 是否都指向远程持久化文件？
- scalar-only 变化是否避免不必要 embedding？
- same-level 文件更新是否 upsert，而不是 delete/reinsert？
- 文件/目录结构变化是否删除旧层级脏记录？
- `ContentTreeAction` 是否总是在 semantic/direct index action 入队前完成？
- direct `IndexAction` 是否只包含不依赖语义结果的操作？
- 语义节点中的 `IndexSlot` 是否避免与旧记录快照重复字段？
- tags、ACL、abstract 和索引存在性是否在需要时被当作向量库状态处理？
- 失败语义和回滚语义是否足够明确？
