# assets/ — 程序图标与 logo

本目录的图片**全部由仓库内的脚本生成**，不包含任何来自游戏或第三方的素材：

```powershell
python tools/make_icon.py            # 重新生成（覆盖本目录）
python tools/make_icon.py --check    # 校验本目录与生成器一致（不写入）
python tools/make_icon.py --preview build\icon-preview.png   # 生成各尺寸预览图
```

| 文件 | 尺寸 | 用途 |
| --- | --- | --- |
| `app.ico` | 16 / 20 / 24 / 32 / 40 / 48 / 64 / 128 / 256 | exe 图标（Explorer、任务栏、标题栏）；构建时由 `Nioh3EquipmentAffixEditor.spec` 嵌入 PE 资源 |
| `logo.png` | 256 | 通用 logo（透明背景，GUI/文档用） |
| `logo-64.png` | 64 | GUI 标题区 logo（原生分辨率，不做缩放） |
| `logo-32.png` | 32 | 备用小尺寸 / 窗口图标回退 |

## 图形含义

主体是**勾玉**（まがたま）：金色勾玉 + 深墨色圆角底 + 一枚朱红印记，边框描金。
勾玉是日本传统的「宝珠/护符」形象，正好对应本工具处理的对象（饰品 = 护符/勾玉类装备），
且与「仁王」的题材一致。几何构造（`tools/make_icon.py` 的 `_parameters()`）完全由圆
计算而来：头部大圆减去一个**内切**的咬合圆，内切保证尾部收成一点——这是勾玉区别于普通
月牙的关键；大尺寸额外挖出穿绳孔，小尺寸则加粗玉体、去掉描边与孔洞，保证 16 px 下依然
可辨认。

## 授权

这些图片由本项目脚本生成，属于本项目的原创几何图形，采用与代码相同的授权（见仓库根目录
`README.md` 的 Credits & attribution 与 Disclaimer）。**它们不是《仁王3》的游戏素材**，
不含任何 Koei Tecmo 的商标或美术资源，仅为形似日本传统纹样的自绘图形。
