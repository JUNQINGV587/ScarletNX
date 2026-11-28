#!/usr/bin/env python3
"""冰汽时代(Switch) HUD 16:10 布局重排 —— 可复现的容器重打包工具。

背景（经 exp1..exp13 上机实测，详见 EdgeEver「冰汽时代 HUD 16:10 资产侧改造 · 实验报告」）：
  * UI 元素树在 romfs/templates.dat 里，是可读 RTTI 序列化：对象以
        \\x01<TypeName>\\x00\\x01<ObjectName>\\x00
    开头，随后是结构化字段；对 UIElementRecipe/UIExternRecipe/UITextRecipe/UIPictureRecipe：
        +0  float pos.x      +4  float pos.y
        +8  u32 0            +12 float anchor/pivot 枚举(1..15)
        +16..+31 全 0
        +32 float size.w     +36 float size.h
        +48/+52/+56 float scale=(1,1,1)   +68 u32 color
  * 只有 **UIExternRecipe** 的 rect 生效：引擎把子模板等比(fit)渲染进这个矩形，
    改 pos 平移、改 size 等比缩放其内容（exp7/9/10）。
  * 普通元素(UIElementRecipe/UITextRecipe/UIPictureRecipe)的 pos/size/color 写入无效
    —— 每帧被 UILayoutRecipe/anchor 重算（exp1/2/3/4/10）。
  * 根 FRAME 的 size/scale 也无效（exp8/11）：画布尺寸不来自资产。
  * 设计空间 1280x720，屏幕映射是等比 1.5x（实测 extern 每 1 设计单位 = 1.518 px），
    所以圆形元素在 1920x1200 上仍是圆（"帮助"圆钮 47x46 px）=> 画面本身不变形，
    16:10 的差事只有"把 extern 贴到边"。

用法：
    python3 relayout-hud.py --batch b1 --out /data/repack/uirelayout/final
    python3 relayout-hud.py --list
    python3 relayout-hud.py --batch b1 --out DIR --verify

产物：DIR/templates.dat + templates.idx（文件名沿用输入 basename，DatSet.write 的约定）。
上了掌机要改名成 templates.dat/.idx 放回 romfs/。
"""
import argparse, importlib.util, os, struct, sys

ASSETS = '/data/scarletnx-snxsync/scripts/frostpunk-assets.py'
BASE = '/data/repack/uirelayout'
STR = os.path.join(BASE, 'templates.str')
DAT = os.path.join(BASE, 'switch-templates.dat')
IDX = os.path.join(BASE, 'switch-templates.idx')

# UI 模板 id -> 名称（来自 templates.str）
TEMPLATES = {
    1098054804: 'ui/mainscreen',
    2362796985: 'ui/panels/city/resourcespanel',
    3389854642: 'ui/panels/city/timepanel',
    261140055:  'ui/panels/city/temperaturepanel',
}

OBJ = __import__('re').compile(rb'\x01(UI[A-Za-z]+Recipe|UI[A-Za-z]+Preset)\x00\x01([^\x00]*)\x00')


def objects(p):
    for m in OBJ.finditer(p):
        yield m.group(1).decode(), m.group(2).decode(), m.end()


def get_rect(p, off):
    x, y = struct.unpack_from('<ff', p, off)
    w, h = struct.unpack_from('<ff', p, off + 32)
    return [x, y, w, h]


def set_rect(p, off, r):
    struct.pack_into('<ff', p, off, r[0], r[1])
    struct.pack_into('<ff', p, off + 32, r[2], r[3])


def find(p, name, types=('UIExternRecipe',)):
    return [(t, o, get_rect(p, o)) for t, n, o in objects(p) if n == name and t in types]


def move(p, name, dx=0.0, dy=0.0, types=('UIExternRecipe',)):
    hits = find(p, name, types)
    for _, off, r in hits:
        set_rect(p, off, [r[0] + dx, r[1] + dy, r[2], r[3]])
    return len(hits)


def resize(p, name, k, anchor='top', types=('UIExternRecipe',)):
    """等比缩放到 k 倍；anchor 决定哪条边不动。"""
    hits = find(p, name, types)
    for _, off, r in hits:
        nh = round(r[3] * k, 3)
        d = r[3] - nh
        ny = r[1] + (d if anchor == 'bottom' else d / 2 if anchor == 'center' else 0.0)
        set_rect(p, off, [r[0], ny, r[2], nh])
    return len(hits)


# 批次计划：每批只动一个变量，带对照截图后再进下一批。
# (template_id, 操作, 参数)
BATCHES = {
    # B1 顶栏贴 y=0：resourcespanel 的 extern 上移 25 设计单位（37px / 1.518 ≈ 24.4）
    # 实测 exp12：顶栏上沿 37px -> 0px，内容比例、横向位置均不变。
    'b1': [(1098054804, move, dict(name='EXT_RESOURCES_PANEL', dy=-25.0))],
    # B3 底栏贴底：底栏不是 extern（mainscreen 普通元素 BOTTOM/Bottom_Panel），
    # 普通元素几何写入无效 -> 计划中标注为"无杠杆"，保留供后续换思路时复用。
    'b3': [],
    # B2 温度表盘：暂无位移需求（居中，未贴边）；留空表示刻意不动。
    'b2': [],
    # v2 顶底都贴边（exp14 实测通过）：根 FRAME pos 有效（size/scale 无效），
    # 整层 UI 下移 40 设计单位=60px（底栏贴底），再把顶栏 extern 单独拉回 65 单位。
    'v2': [(1098054804, move, dict(name='FRAME', dy=40.0, types=('UIElementRecipe',))),
           (1098054804, move, dict(name='EXT_RESOURCES_PANEL', dy=-65.0))],
}


def load_mod():
    spec = importlib.util.spec_from_file_location('fp', ASSETS)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--batch', default='b1')
    ap.add_argument('--out', default=os.path.join(BASE, 'final'))
    ap.add_argument('--dat', default=DAT)
    ap.add_argument('--idx', default=IDX)
    ap.add_argument('--str', default=STR)
    ap.add_argument('--level', type=int, default=18)
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--verify', action='store_true')
    a = ap.parse_args()

    if a.list:
        for k, v in BATCHES.items():
            print(k, '->', [(TEMPLATES.get(t, t), f.__name__, p) for t, f, p in v] or '(no-op)')
        return

    plan = BATCHES[a.batch]
    fp = load_mod()
    ds = fp.DatSet(a.dat, a.idx, a.str)
    payloads = {t: bytearray(ds.read_payload(rec)) for t, rec in
                ((t, next(r for r in ds.records if r.rid == t)) for t in TEMPLATES)}
    for tid, fn, kw in plan:
        n = fn(payloads[tid], **kw)
        print(f'[{TEMPLATES[tid]}] {fn.__name__} {kw} -> {n} hit(s)')

    os.makedirs(a.out, exist_ok=True)
    ds.write(a.out, replacements={t: bytes(p) for t, p in payloads.items()}, compress_level=a.level)
    for f in sorted(os.listdir(a.out)):
        p = os.path.join(a.out, f)
        print(f, os.path.getsize(p))

    if a.verify:
        ds2 = fp.DatSet(os.path.join(a.out, 'switch-templates.dat'),
                        os.path.join(a.out, 'switch-templates.idx'), a.str)
        for t, name in TEMPLATES.items():
            rec = next(r for r in ds2.records if r.rid == t)
            p = ds2.read_payload(rec)
            for key in ('EXT_RESOURCES_PANEL', 'EXT_TIME_PANEL', 'EXT_TEMPERATURE_PANEL', 'FRAME'):
                for ty, off, r in find(p, key, ('UIExternRecipe', 'UIElementRecipe')):
                    print(f'  {name} {key:26s} {ty:16s} pos({r[0]:8.1f},{r[1]:9.1f}) size({r[2]:7.1f}x{r[3]:7.1f})')


if __name__ == '__main__':
    main()
