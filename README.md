# video-downloader-skill

一个独立的本地视频下载 Skill：**视频链接 → 下载 → 校验 → 本地文件路径**。

直接提取 AIOS 已使用的 `local-video-downloader` 下载引擎及必要的平台适配器；新增代码仅负责命令行入口、安装说明和进程锁。无需运行 AIOS，不连接 AIOS 数据库，不带原机器的配置、账号、下载历史或视频。不做转文字、总结、剪辑、发布。

## 支持范围

- 单条 YouTube、抖音、快手、小红书视频和微信视频号分享链接。
- 保留现有解析、音画合并、时长检查及 FFmpeg 全片解码检查。
- 不展开频道、主页、播放列表，不下载直播或图集，不绕过 DRM、验证码和账号权限。
- 默认匿名；部分平台或视频需用户自行登录。平台会变化，不能保证所有链接均可下载。首版本在 Windows 11 / Python 3.12 验证；其他系统未做真实验收。

## 安装

准备 Python 3.12、Git，以及包含 `ffmpeg` / `ffprobe` 的 FFmpeg，确保二者可在终端执行。需要登录时另需安装 Google Chrome。所有登录均在本地专用窗口完成。

仓库已公开为 **Public**，无需申请仓库访问权限即可下载、克隆和安装：

```powershell
git clone https://github.com/xiaogandy-cloud/video-downloader-skill.git
cd video-downloader-skill
.\setup.bat
```

也可手动安装：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -c requirements-lock.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe scripts\download.py check
```

`check` 只检查依赖，不下载视频、不验证账号，也不运行模型。没有 API Key 或付费模型配置。网络需要代理时，可显式传 `--proxy http://127.0.0.1:端口`；默认不继承原 AIOS 的代理设置。

`requirements-lock.txt` 记录本次真实验收的依赖版本。安装 Playwright 浏览器若直连不可用，可在当前终端临时设置 `HTTPS_PROXY` 为你自己的代理地址后重试安装；不要将代理密码写入源码或提交 Git。

### 安装为 Codex Skill

将整个仓库克隆到 `%USERPROFILE%\.codex\skills\video-downloader-skill`，在这个目录完成上面的安装步骤，重新加载 Codex 的 Skill 列表或新建会话。其他支持 SKILL.md 的工具按其技能目录约定安装。

例如在 Codex 中说：

> 使用 video-downloader-skill，把这条视频下载到我的 Downloads 文件夹：〈完整视频链接〉。下载后告诉我文件路径。

不需要 AIOS 在后台运行，也不需要原作者的登录资料。

## 使用

最简单的交互方式（提示后粘贴链接或 App 分享文字）：

```powershell
.\.venv\Scripts\python.exe scripts\download.py download --output "$HOME\Downloads"
```

通过标准输入，适合程序/Agent 调用，避免敏感 URL 参数进入进程命令行：

```powershell
$videoLink = Read-Host '粘贴完整视频链接或分享文字'
$videoLink | .\.venv\Scripts\python.exe scripts\download.py download --output "$HOME\Downloads"
```

无敏感参数的公开链接也支持 `download --url "https://www.youtube.com/watch?v=VIDEO_ID"`。默认保存到当前工作目录的 `downloads/`。正常文件名为时间、平台、视频标题；同名不覆盖。

stderr 输出进度；stdout 输出最终 JSON。只有 `ok: true`、`status: success` 和 `validation.full_decode: true` 表示音画完整性校验及保存完成；`path` 是真实文件路径。任务失败退出码为 1，需要水印确认为 2，用户取消为 130。

### 可选登录

只有平台要求时才运行，例如抖音：

```powershell
.\.venv\Scripts\python.exe scripts\download.py login douyin
# 在专用 Chrome 窗口手动登录，再回终端按回车保存
.\.venv\Scripts\python.exe scripts\download.py download --use-login --output "$HOME\Downloads"
```

`login` 平台可选 `youtube / douyin / kuaishou / xiaohongshu / wechat`。不读取日常浏览器，不输入/保存明文密码。只在用户选择保存登录后将会话保存在当前 Skill 的 `.local/`；平台是否接受会话以实际下载为准。

**视频号**仅支持微信复制的 `https://weixin.qq.com/sph/…` 单视频链接。链接会交给腾讯官方元宝解析，不是第三方解析站；先 `login wechat`，下载时显式使用 `--use-login --allow-wechat-resolver`。不会调用元宝聊天、AI 总结或转写。需要验证码时交给用户，不自动绕过。

若来源带平台水印，程序会停止并提示；接受水印后再次执行时可追加 `--accept-watermark`。这不移除原视频的任何水印。Ctrl+C 仅取消当前 Skill 自己创建的任务。

## 隐私与分享

源码使用白名单提取，未复制原项目 `.local`、`.env`、浏览器资料、账号、密钥、下载记录、视频、虚拟环境或 Git 历史。`.gitignore` 排除这些运行文件。

第一次安装后没有登录数据。不要将运行过的整个文件夹打包发给别人；用 `git archive --format=zip --output=video-downloader-skill.zip HEAD` 导出已提交的干净源码。Git 忽略规则不能代替人工检查，禁止 `git add -f` 加入凭证或个人文件。

## 代码与许可证

- `scripts/download.py`：独立 CLI，不启动 Web 服务。
- `single/`：现有单条下载引擎、任务执行、文件校验和可选登录。
- `app/`：原引擎依赖的平台解析代码；CLI 不暴露批量功能。
- `UPSTREAM.json`：提取时每个源文件的 SHA-256，用于核实复用来源。
- `TEST_REPORT.md`：本版本实际测试结果及限制。

离线回归命令：`.\.venv\Scripts\python.exe -m unittest discover -s tests -v`。这些测试不调用平台下载，不能代替真实下载验收。

组合分发遵循 **GPL-3.0-or-later**；保留原 MIT 声明及视频号组件 MIT 许可证，详见 `LICENSE`、`LICENSE.MIT`、`NOTICE.md` 和 `single/vendor/LICENSE.wxsph`。第三方依赖按其自身许可证安装。后续公开和分发也须保留这些声明。
