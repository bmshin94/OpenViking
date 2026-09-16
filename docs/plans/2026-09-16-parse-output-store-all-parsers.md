# ParseOutputStore 全 Parser 接入实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让所有会生成解析 artifact 的 parser 在 local 与 AGFS 模式下使用同一套输出接口，并保证嵌套解析、manifest 和清理行为一致。

**Architecture:** parser 只操作 artifact-relative path；`ParseArtifactWriter` 负责创建 artifact、写入最终字节和维护 MD5 manifest；通用 merge helper 负责在父子 artifact 间复制目录树并合并 manifest。`ParseResult.artifact_ref` 是后端事实源，`temp_dir_path` 仅保留兼容。

**Tech Stack:** Python、asyncio、pytest、VikingFS、ParseOutputStore。

---

### Task 1: 公共 artifact runtime

**Files:**
- Modify: `openviking/parse/output.py`
- Modify: `openviking/parse/base.py`
- Test: `tests/parse/test_parse_output.py`

1. 为 writer 的写入、manifest、cleanup 和跨 artifact merge 编写失败测试。
2. 实现请求级 `ParseArtifactWriter` 与 backend-neutral merge。
3. 收紧 `ParseResult.ensure_artifact_ref()`：仅允许从 `viking://temp/...` 推导 legacy AGFS ref。
4. 运行 parse output 单测。

### Task 2: Markdown parser 家族

**Files:**
- Modify: `openviking/parse/parsers/markdown.py`
- Modify: `openviking/parse/parsers/text.py`
- Modify: `openviking/parse/parsers/pdf.py`
- Modify: `openviking/parse/parsers/html.py`
- Modify: `openviking/parse/parsers/anydoc.py`
- Test: `tests/parse/test_markdown_apply_layout.py`
- Test: `tests/parse/test_document_parser_threading.py`

1. 增加 Markdown local artifact 测试和委托 parser 的 store 透传测试。
2. 将 Markdown layout 改为 artifact-relative path，通过 writer 写入正文、图片和 sidecar。
3. 保证 Text/PDF/HTML/AnyDoc 将请求级 store 传递给 Markdown。
4. 同时验证 AGFS 与 local 输出树、字节和 manifest 一致。

### Task 3: 媒体与 Understanding parser

**Files:**
- Modify: `openviking/parse/parsers/media/image.py`
- Modify: `openviking/parse/parsers/media/audio.py`
- Modify: `openviking/parse/parsers/media/video.py`
- Modify: `openviking/parse/understanding_api.py`
- Test: `tests/parse/test_media_resource_name.py`
- Test: `tests/parse/test_understanding_api_artifact_images.py`

1. 增加 local 输出和失败清理测试。
2. 将媒体文件、图片 tiles、Understanding ZIP 内容全部改为 writer 相对路径写入。
3. 返回明确的 `artifact_ref`，并保持旧 `temp_dir_path` 兼容。

### Task 4: Directory 与 ZIP 组合

**Files:**
- Modify: `openviking/parse/parsers/directory.py`
- Modify: `openviking/parse/parsers/zip_parser.py`
- Test: `tests/parse/test_add_directory.py`
- Test: `tests/parse/test_directory_understanding_routing.py`
- Test: `tests/parse/test_parse_output.py`

1. 增加 local 父 artifact、local 子 artifact、Understanding 子 artifact 和 no-split flatten 测试。
2. Directory 创建父 writer，并把同一个 store 传给所有子 parser。
3. 用通用 merge helper 替换 VikingFS 专用 move/recursive merge。
4. 直接上传文件也通过父 writer 写入。
5. ZIP 只负责安全解压和委托，不自行拥有解析 artifact。

### Task 5: 下游生命周期收口

**Files:**
- Modify: `openviking/utils/resource_processor.py`
- Modify: `openviking/parse/tree_builder.py`
- Modify: `openviking/parse/parsers/base_parser.py`
- Test: `tests/utils/test_local_artifact_persist.py`
- Test: `tests/utils/test_resource_processor_processing_mode.py`

1. 所有 cleanup 通过 artifact ref 对应的 store 执行。
2. 新 parser 路径不再依赖字符串猜测 backend。
3. 删除未使用且存在并发歧义的 parser 实例级 output-store 状态。
4. 保证进入异步 semantic 队列前，local artifact 已提交正式 AGFS 并完成清理。

### Task 6: 回归验证

1. 运行 parser、artifact、resource diff、SemanticPlan 相关测试。
2. 分别验证 local 与 AGFS 的初次导入、no-op 和嵌套目录导入。
3. 运行变更文件 Ruff、`git diff --check` 和冲突标记检查。
4. 核对工作区，只提交实现与测试，不提交 benchmark 运行产物。
