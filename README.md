# dsh-multicad (community fork)

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 插件：把 **AutoCAD / 中望 ZWCAD / 浩辰 GstarCAD / BricsCAD** 接进来，让 agent 能直接读图、画图、改几何、存盘。

本仓库是 [a494359163-byte/dsh-multicad](https://github.com/a494359163-byte/dsh-multicad) 的 fork，上游内置 [AnCode666/multiCAD-mcp](https://github.com/AnCode666/multiCAD-mcp)。本项目保留 Apache-2.0 许可与原作者署名，见 [NOTICE](NOTICE)。

## 这个 fork 改了什么

上游能画能改，但几何编辑只有"整块移动/旋转/缩放"，圆角只能自己拼弧、多段线没法合并，而且两个截图/导出的 bug 会让人拿到旧图。本 fork：

**修 bug**

| 问题 | 现象 | 修复 |
|---|---|---|
| 窗口识别只认英文标题 + AutoCAD 窗口类 | 中文版 ZWCAD 2026 上 `screenshot` 报 `Could not find window for zwcad` | 优先读 COM 的 `Application.HWND`，回退枚举窗口时匹配多语种标题（`中望CAD` / `浩辰CAD` / `ZWCAD` …），并排除 VBA、控制台类窗口 |
| `export_view` 不覆盖已有文件 | 图纸变了但导出还是旧图（PNGOUT 的覆盖提示被 SendCommand 吞掉） | 导出前先删目标文件，导出后校验 mtime，拿不到新图就明确报错 |
| 圆弧角度只收 `[0,360)` | `-90°`（正下方）画不了，右下圆角要绕成 `270→0` | 角度归一化，`-90`、`540` 都能用 |
| `rect` 多传一个闭合点 | 矩形是 5 个顶点（含零长边），圆角只倒 3 个角、explode 多出一个空实体 | 不再重复首点，闭合交给 `Closed` 标志；`draw_polyline` 也去重 |

**新增 `manage_geometry`（8 个动作）**

| 动作 | 写法 | 说明 |
|---|---|---|
| join | `join\|239,23A,23B` | 把首尾相接的 LINE/ARC 串成一条多段线，弧段转成精确 bulge，**不折线拟合** |
| fillet | `fillet\|1F\|1.5` / `fillet\|1F\|1.5\|0,2` | 真正的相切圆角，可只倒指定角，半径过大自动收敛并回报 `clamped` |
| chamfer | `chamfer\|1F\|2` | 直边倒角 |
| offset | `offset\|1F\|3` | 偏移，正负决定方向（一侧不通会自动换向） |
| array_rect | `array_rect\|1F\|3\|4\|25\|25` | 矩形阵列 |
| array_polar | `array_polar\|1F\|6\|360\|0,0` | 环形阵列 |
| explode | `explode\|1F` | 炸开多段线/块参照 |
| mirror | `mirror\|1F\|0,0\|0,1\|true` | 镜像，可选删除原件 |

几何全部用 Python 算完再通过 `AddLightWeightPolyline` / `SetBulge` 写回，不模拟键盘、不依赖 CAD 命令别名。

## 实测校验

20×10 试件上，实测值与解析值对得上：

| 操作 | 面积 | 周长 |
|---|---|---|
| 4 角 R3 圆角 | 192.274（理论 192.2743） | 54.850（54.8496） |
| 4 角 2mm 倒角 | 192.000（192.0000） | 55.314（55.3137） |

60×60 板（四角 R1.5、中心 Ø3）8 个实体合并成 1 条闭合多段线后：

- 面积 **3598.069** ＝ 3600 − 4×(1.5² − π·1.5²/4) = 3598.0686
- 周长 **237.425** ＝ 4×57 + 4×(π·1.5/2) = 237.4248

再对该多段线做 `offset 3`：面积 4338.617 ＝ A + P·d + πd² = 3598.069 + 712.274 + 28.274，周长 256.274 ＝ P + 2πd，同样精确。

## 安装

需要 Windows、Python 3.10+（PATH 上能跑 `py -3` 或 `python`），以及本机已安装并在运行的 AutoCAD 2018+ / ZWCAD 2020+ / GstarCAD 2020+ / BricsCAD 21+。CAD 必须与 DSH 用同一个 Windows 用户、同一权限级别（别用管理员启动 CAD）。

```powershell
# 从本地目录安装（推荐，改动立刻可见）
dsh plugin add C:\path\to\dsh-multicad

# 或者从仓库安装
dsh plugin add github:OWNER/dsh-multicad
```

装完**完全退出 DSH 再打开**，工具会以 `mcp__multicad__*` 出现。首次启动会在 `%LOCALAPPDATA%\dsh-multicad\venv` 建私有 venv 装依赖（需联网），之后秒开；venv 坏了删掉即可自愈。

## 工具一览

`mcp__multicad__*` 共 8 个：

| 工具 | 用途 |
|---|---|
| `manage_session` | 连接/断开、状态、缩放、截图、导出视图、撤销重做 |
| `draw_entities` | 线/圆/弧/矩形/多段线/样条/文字/表格/引线/标注 |
| `manage_geometry` | **本 fork 新增**：join / fillet / chamfer / offset / array / explode / mirror |
| `manage_entities` | 选择、移动、旋转、缩放、改色改层、复制粘贴、删除 |
| `manage_layers` | 图层创建、改色、开关 |
| `manage_blocks` | 块创建、插入、属性读写 |
| `manage_files` | 新建、保存（DWG/DXF/PDF）、关闭、切换、列表 |
| `export_data` | 导出实体清单 JSON / Excel |

## 已知限制

- **未保存的图纸会被丢**：如果图纸还是 `Drawing1`（从没存过盘），MCP 服务进程被重启/杀掉时，多数情况下整张图的内容会没掉。已在文件里的图纸不受影响。**动手前先 `manage_files save|路径.dwg`。**
- `manage_session export_view` 用 CAD 自带 `PNGOUT` 平移到图片，窗口被完全遮挡时偶尔会拿到旧帧（本 fork 已加 mtime 校验，会报错而不是静默给旧图）。
- 服务会常驻一个本地看板 `http://127.0.0.1:8888`（无鉴权，仅本机可达）。占用 8888 端口，可通过 `python/src/config.json` 的 `dashboard.port` 修改。
- `config.json` 里 `allow_arbitrary_paths: true`，保存时可以写任意绝对路径。
- 只在 ZWCAD 2026 专业版上做过端到端实测；AutoCAD / GstarCAD / BricsCAD 走同一套 ActiveX，理论上兼容但未逐个验证。

## 开发

```powershell
$py = "$env:LOCALAPPDATA\dsh-multicad\venv\Scripts\python.exe"
& $py -m pip install -r python\requirements.txt pytest
& $py -m pytest tests -q          # 纯几何数学，不需要 CAD
```

修改 `python/src` 后同步到运行目录（插件加载时会从包内复制到该目录）：

```powershell
Copy-Item python\src\* "$env:LOCALAPPDATA\dsh-multicad\python\src\" -Recurse -Force
# 然后重启 DSH 或结束 %LOCALAPPDATA%\dsh-multicad 下的 server.py 进程
```

## 许可

Apache-2.0。上游 dsh-multicad 与 multiCAD-mcp 的版权归其作者所有，详见 [NOTICE](NOTICE)。
