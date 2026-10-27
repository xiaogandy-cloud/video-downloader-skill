---
name: video-downloader-skill
description: 独立下载用户提供的单条 YouTube、抖音、快手、小红书或视频号视频链接和分享文字，保存本地并返回真实文件路径。不依赖 AIOS；不用于搜索、批量主页、直播、图集、转写、总结、剪辑或发布。
license: GPL-3.0-or-later; original MIT notices retained
---

# 单条视频下载

使用本 Skill 自带的 `scripts/download.py`。路径均相对于本 SKILL.md 所在目录，不需要 AIOS、原作者目录或本地 Web 服务。

## 执行

1. 缺少用户视频链接时询问；保留分享链接的完整参数。只下载用户有权保存的单条视频。
2. 使用 Skill 目录下的 `.venv/Scripts/python.exe`（Windows）运行 `scripts/download.py check`。缺依赖按 [README.md](README.md) 安装，不能把依赖检查成功当成下载成功。
3. 运行 `scripts/download.py download --output <保存目录>`，通过标准输入发送完整链接或分享文字。输入结束后关闭 stdin。不要把带登录/签名参数的 URL 写入命令、对话或调试日志。
4. 该命令持续执行到下载、保存和完整解码校验完成；stderr 为进度，stdout 最后一行是结果 JSON。进程仍在运行时继续等待同一进程，不重复提交。
5. 只有退出码 0、`ok=true`、`status=success`、`validation.full_decode=true` 且 `path` 对应文件存在，才能报告成功。返回完整保存路径、实际尺寸、时长及水印状态。`check` 不能验证真实平台可用性。

## 登录和异常

- 默认匿名下载，不读取其他浏览器、AIOS 配置或账号数据。
- 平台明确要求登录时，可在交互终端运行 `scripts/download.py login <platform>`，让用户在专用 Chrome 窗口自行登录；用户回终端按回车保存后，下载追加 `--use-login`。需要安装 Chrome。不得索取密码、验证码或 Cookie。
- 新登录会话只保存在本 Skill 的 `.local/`，不输出其内容、不加入 Git、不随 Skill 分享。不要复制已有 AIOS 的登录资料。
- 视频号依赖腾讯官方元宝解析；首次说明链接会交给腾讯处理，用户同意后追加 `--allow-wechat-resolver`，并通过 `login wechat` 手动登录。只解析链接，不调用聊天/转写模型。
- 收到有水印来源确认时，仅在用户明确接受后使用 `--accept-watermark`。保留原视频中的水印，不擦除。
- 登录失效、验证码、权限拒绝、DRM 或链接失效时停止并说明真实原因，不绕过访问限制，不自动反复重试或切换第三方解析。
- Ctrl+C 只取消此 Skill 自己创建的进程。不重启 AIOS，不注册服务或自动启动，不提权。

## 范围

仅下载、音画完整性检查和本地保存。原解析器内部包含复用的通用函数，但唯一下载入口严格拒绝主页、播放列表展开、直播及图集。完整解码只是检查文件，不进行剪辑、字幕、转写、总结或发布。
