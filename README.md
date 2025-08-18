# mod_sync_tool

当前版本：3.1.0（2025-08-16）

面向 Arma Reforger 的开源模组同步/分发工具，支持多节点（Cloudflare R2/S3 兼容与 HTTP 直链）、分片并发下载、断点续传与速度限制，自动比较本地与云端版本并一键同步。提供现代化 Tkinter 界面与日志面板，适合俱乐部、服务器与个人使用。

## 特性

- 自动检测模组更新：语义化版本比较，差异直观
- 多节点下载：S3 优先，404 时回退 HTTP；可配置公共直链域名
- 分片并发与断点续传：瞬时速度计算，MiB/s 与 Mbps 同显
- 速度限制：0–1000 Mbps，运行中可随时调整
- UI 线程安全：所有 UI 更新通过 `root.after()` 调度
- 自动识别本地路径：解析 Arma Reforger 日志定位 addons 目录
- 现代 UI：`ttkbootstrap` 主题、可拖拽分割、斑马纹表格与右键菜单
- 应用内更新：读取远端 `version.json` 提示（支持强制更新）

## 设计约定

- 云端对象命名：`Name_ModID_Version.7z`（允许空格），全部置于存储桶根目录
- 本地目录命名：解压至 `Name_ModID`
- 直链支持：`mod_info*.json` 的 `path` 字段可写 `@https://...` 完整直链

## 快速开始

```bash
git clone https://github.com/yourname/mod_sync_tool.git
cd mod_sync_tool
pip install -r requirements.txt
python main.py
```

打包为单文件可执行：

```bash
pyinstaller -F -w --version-file file_version_info.txt -i favicon.ico --name "mod_sync_tool" main.py
```

## 配置

项目根目录示例文件（可直接复制修改）：

1) `mod_lists.json`（选择要加载的列表文件）
```json
{
  "default": { "name": "默认模组列表", "file": "mod_info.json", "description": "示例" },
  "lightweight": { "name": "轻量化列表", "file": "mod_info_light.json", "description": "示例" }
}
```

2) `mod_info.json`（模组清单，关键三元一致：name/mod_id/version）
```json
{
  "62D15D0025AE021B": {
    "name": "ZSU-23-4 Shilka",
    "version": "1.1.5",
    "filename": "ZSU-23-4 Shilka_62D15D0025AE021B_1.1.5.7z"
  }
}
```

3) `minio_config.json`（R2/S3 节点与全局参数）
```json
{
  "settings": { "bucket": "arma-reforger-mods", "timeout": 30, "retry_count": 3, "retry_delay": 1, "max_workers": 4, "force_r2_only": false },
  "R2_S3": { "endpoint": "https://your.r2.domain", "port": 443, "access_key": "AK", "secret_key": "SK", "secure": true, "region": "auto", "public_url_base": "https://cdn.example.com" }
}
```

4) `version.json`（更新检查）
```json
{ "latest": "3.1.0", "min": "3.1.0", "mandatory": true, "url": "https://your-domain/downloads/mod_sync_tool_v3.1.0.exe" }
```

## 使用流程

1. 启动程序（首次会自动检查更新）
2. 选择模组列表（或在 `mod_lists.json` 指向自定义清单）
3. 设置本地模组路径（支持自动识别）
4. 点“检查模组”获取差异
5. 点“同步模组”下载、校验、解压并清理临时文件

## 常见问题

- 速度与任务管理器不一致：UI 显示“瞬时速率”，并同时显示 MiB/s 与 Mbps 便于对齐
- S3 404：程序会自动尝试 HTTP 直链（同时支持 `base/bucket/key` 与 `base/key` 两种 URL 形式）
- 文件名含空格：已做 URL 编码处理；确保云端文件名严格符合命名规范

## 参与贡献

- 提交 Issue/PR 前请先搜索是否已有讨论
- 代码风格：保持清晰命名与类型注解，避免深层嵌套；遵循现有格式
- 提交信息建议：`feat: ...` `fix: ...` `perf: ...` `docs: ...` `refactor: ...`

## 许可证

本项目建议采用 MIT License（你也可以在派生时改为 Apache-2.0 等）。如发布正式版，请在仓库根目录添加 `LICENSE` 文件。

## 致谢

- Cloudflare R2、boto3、py7zr、ttkbootstrap 等优秀开源项目
- 社区与俱乐部用户的持续反馈

## 更新日志（摘）

### [3.1.0] - 2025-08-16

- 统一根目录对象命名与解析：`Name_ModID_Version.7z`，支持空格与 URL 编码
- “检查模组”提速：只为需更新/未安装条目预取云端大小；并行 HEAD 探测
- S3→HTTP 回退：S3 404 时自动尝试直链；速度显示更贴近系统监控
- UI 与稳定性：全面主线程更新调度；现代主题与右键菜单；暂停/取消更即时