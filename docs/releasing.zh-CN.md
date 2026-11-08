# 源码归档

[English](releasing.md) | 简体中文 · [中文首页](../README.zh-CN.md)

使用以下命令创建源码归档：

```bash
python scripts/build_source_zip.py
```

v0.1.0 的输出为 `dist/codex-mcp-0.1.0-source.zip`，并附带 SHA-256 校验文件。ZIP 中的 `codex-mcp/` 目录包含源码、文档、示例、测试及 CI 定义，不包含私有状态、凭据、Git 元数据、已安装依赖或缓存。

按所有者要求，ZIP **不包含 `LICENSE`**，以保留仓库中已有的 MIT 许可证文件。Python 包元数据仍声明 MIT 并引用仓库中的该文件；构建可安装的 Python 分发包时，应使用包含许可证文件的检出目录。直接从源码运行桥接服务不需要先构建 Python 包。

内嵌的 `SOURCE-MANIFEST.json` 记录归档文件的大小和哈希。归档工具会检查生成文件的完整性，不会运行单元测试或调用模型。
