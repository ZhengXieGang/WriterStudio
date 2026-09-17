GRBL 写字机上位机软件。文字、表格、LaTeX公式、TikZ、SVG矢量图，笔画编辑，手写模拟 一个软件全搞定：
可多字体混排、可调手写随机扰动、可纸张对齐参考层。

## 功能特性

### 书写内容
* **多字体混排**：单文档内任意字符指定字体，自动回退链（如中文楷体 + 英文单线体）
* **手写随机扰动**：字号/位置/行首偏移/基线起伏/笔画级抖动，三级预设 + 全参数可调，
  确定性随机（同种子可复现，可整体重摇）
* **Markdown**：标题/列表/表格，表格行线手绘化随机，单元格文字跟随行线起伏
* **LaTeX**：公式用内置 mathtext 引擎渲染（免装 TeX）；完整 LaTeX 文档
  未装 TeX 时自动用内置排版引擎渲染常用子集（标题/章节/列表/公式/简单表格），
  检测到 TeX 工具链则自动升级为 PDF→SVG 忠实编译；TikZ 图形需要 TeX
* **矢量绘图与 SVG 导入**：矩形/椭圆/线条等图元，SVG 可带手绘抖动导入
* **文字方向**：横排、竖排都支持
* **逐字符字体覆盖**：选中文本中的字符单独指定字体/缩放

### 编辑与排版
* 笔画编辑工具：单笔画移动/变换/弯折/重扰动/删除
* 纸张预设与**参考层**：可导入图片/SVG 底图作为参考

## 支持的字体格式

| 类型 | 扩展名 | 说明 |
|------|--------|------|
| Hershey 单线 | `.jhf` | 内置 32 款拉丁/符号字体 |
| 单线笔画 JSON | `STRK-*.json` | 内置中文楷体（20976 字形） |
| TrueType/OpenType | `.ttf` `.otf` | 经 fontTools 解析轮廓转单线 |
| gcode 字符库 | 每字符一文件 | 每字符一段 G-code 的字库目录 |
| `.gfont` | `.gfont` | 单线笔画字体包 |

外部字体目录可在设置中添加，扫描建索引瞬时完成、懒加载。

## 快速开始

```bash
pip install -r requirements.txt   # 或 pip install .
python -m writerstudio            # 启动界面
python -m pytest -q               # 运行测试（无需显示器）
```

**零依赖开箱即用**：程序自带全部依赖（Python/Qt/字库/排版引擎），上述功能
不需要在系统里装任何东西。公式与 LaTeX 文档的内置渲染开箱即用；可选安装
TeX 工具链（texlive 的 `xelatex` + poppler 的 `pdftocairo`，或 `dvisvgm`）
后，LaTeX 文档自动升级为忠实编译——不装也不影响任何日常功能。

## 打包发行

PyInstaller 不能交叉编译，三个平台各自原生构建：

```bash
pip install pyinstaller
pyinstaller writerstudio.spec    # Windows 出单文件便携 exe；Linux/macOS 出目录版
python scripts/make_appimage.py  # Linux：把目录版组装成 AppImage（零安装分发）
```

仓库自带 GitHub Actions CI/CD：

* **ci.yml** —— 推送到 main / PR 时触发：pyflakes 静态检查 + 全量测试
  （Linux 为门禁，Windows/macOS 观察项，Qt 测试离屏运行）。
* **build.yml** —— 手动触发只出构建产物；推 `v*` 标签则先跑门禁测试，
  通过后构建三平台产物并**自动创建 GitHub Release**（附产物与
  SHA256SUMS.txt 校验和，发布说明按提交自动生成）。

发布一次新版本的完整操作：

```bash
git tag v0.2.0 && git push origin v0.2.0   # 之后去 Actions 页看进度
```

Linux 产物在 `ubuntu:22.04` 容器里构建（glibc 2.35 基线），兼容更多
旧发行版；Windows 为单文件便携 exe；macOS 为 .app（ad-hoc 签名）。

## 开发

```
├── writerstudio/
│   ├── core/       数据层（纯 Python，无 Qt 依赖）
│   ├── fonts/      字体系统（5 种格式解析 + 排版引擎 + 扰动管道）
│   ├── perturb/    手写扰动引擎
│   ├── content/    Markdown / LaTeX / TikZ / SVG 导入
│   ├── machine/    G-code 生成 / GRBL 协议 / 串口链路 / 路径优化
│   └── ui/         PySide6 界面层
├── tests/          单元测试（`python -m pytest -q`，离屏运行）
├── tools/          模板生成等辅助脚本
└── scripts/        AppImage 组装脚本
```

测试全部离屏可跑（`QT_QPA_PLATFORM=offscreen`），无显示器环境/CI 直接执行。

## 许可

本软件：GPL-3.0-or-later，详见 [LICENSE](LICENSE)。

内置字库的第三方数据：

* `fonts/builtin/hershey/*.jhf` —— Hershey Fonts。原始创建者 Dr. A. V. Hershey
  （U.S. National Bureau of Standards），字体数据格式来自 James Hurt（Cognition,
  Inc.）。可自由使用（含商业），须随数据保留致谢文本，见
  [`fonts/builtin/hershey/ACKNOWLEDGEMENT.txt`](writerstudio/fonts/builtin/hershey/ACKNOWLEDGEMENT.txt)。
* `fonts/builtin/stroke-json/STRK-Kaiti.json` —— 中文单线字库，由
  [chinese-hershey-font](https://github.com/LingDong-/chinese-hershey-font)（MIT，
  Copyright (c) 2018 Lingdong Huang）从 TTF 生成。
