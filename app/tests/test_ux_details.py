# -*- coding: utf-8 -*-
"""UX 细节契约测试（先红后绿）。

前端没有 JS 测试框架，这里用**静态契约 + 真实渲染测量**把待修的行为钉住：每条断言在
旧实现上必须红，改完必须绿。静态部分只读 app/static 的文件内容；布局类断言（树线对齐、
表格溢出）交给 ux_render_probe 用 headless Chromium 量真实坐标——静态推算的 4.5px 与
实测的 5.5px 差了 1px，说明这类事只能实测。

三类断言的强度不同，docstring 里都标了：
- 「实测」：headless 渲染后读 getBoundingClientRect / computed color，最准，依赖本机 Edge；
- 「真算」：对比度用 WCAG 2.1 相对亮度公式，不是抄期望值；
- 「结构」「标记」：断言控件是不是可聚焦元素、有没有守卫分支、约定的实现标记是否出现。

已知取舍（按 verified-fix「断言行为不断言实现」自查）：
- 「标记」类是唯一耦合实现的断言（换实现就要同步改断言），已放宽到多候选命名
  （refreshPending|refreshQueued|…、loadSeq|loadToken|…）以减少假红。真行为化需要把页面
  模块跑进 headless 并 mock 接口，成本高于本次收益，留待需要时升级；
- 窄窗表格溢出、配额卡副文本两条当前已绿，属防回归断言，不计入 FAIL_TO_PASS。

每条 docstring 都写了三件事：现在错在哪、用户实际感受到什么、改完哪条断言转绿。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ux_render_probe import browser_path, probe  # noqa: E402

STATIC = Path(__file__).resolve().parents[1] / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


CSS = read("app.css")
APP_JS = read("app.js")
HOME_JS = read("pages/home.js")
FORM_JS = read("pages/source-form.js")
MANAGE_JS = read("pages/manage.js")
LOGS_JS = read("pages/logs.js")
MONITOR_JS = read("pages/monitor.js")
SETTINGS_JS = read("pages/settings.js")
USAGE_JS = read("pages/usage.js")


# --------------------------------------------------------------- CSS 小工具

def rule(css: str, selector: str) -> str:
    """取 CSS 里 `selector{...}` 的声明体（首次出现，不处理嵌套，够本项目用）。"""
    i = css.find(selector)
    if i < 0:
        return ""
    j = css.find("{", i)
    k = css.find("}", j)
    if j < 0 or k < 0:
        return ""
    return css[j + 1:k]


def decl(block: str, prop: str) -> str:
    m = re.search(r"(?:^|[;{]\s*)" + re.escape(prop) + r"\s*:\s*([^;}]+)", block)
    return m.group(1).strip() if m else ""


def px(text: str) -> float:
    return float(re.search(r"(-?[\d.]+)px", text).group(1))


def _channel(v: float) -> float:
    v /= 255.0
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def light_theme_vars(css: str) -> dict[str, str]:
    """:root 里的颜色变量（截到深色主题之前，避免被 dark 覆盖）。"""
    cut = css.find('html[data-theme="dark"]')
    head = css if cut < 0 else css[:cut]
    return dict(re.findall(r"--([\w-]+)\s*:\s*(#[0-9A-Fa-f]{6})", head))


def func_body(src: str, signature: str) -> str:
    """从 `signature` 起，按大括号配平截出函数体（粗略，仅用于体量小、无嵌套模板串的函数）。"""
    i = src.find(signature)
    if i < 0:
        return ""
    j = src.find("{", i)
    if j < 0:
        return ""
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
    return src[j:]


def near(src: str, needle: str, span: int = 260) -> str:
    """`needle` 前后各 span 个字符的窗口（在源码里找构造点用）。"""
    i = src.find(needle)
    return "" if i < 0 else src[max(0, i - span): i + span]


# =============================================================== 视觉与对比度

class TestLightThemeContrast(unittest.TestCase):
    """浅色主题状态色文字过不了 WCAG AA（小字 4.5:1）。

    现在错在哪：--warn 是 #F59E0B，配 --warnbg #FFFBEB 只有约 2.1:1。
    用户体验：监控页最关键的两条告警「网关配置来源不一致」「网关身份未核实」
    正是琥珀字配琥珀底，几乎读不清；全站 warn 文案同样发虚。
    改完转绿：--warn 压深到至少 4.5:1（如 #B45309）。断言是真算的。
    """

    MIN = 4.5

    def test_warn_text_on_warnbg(self):
        v = light_theme_vars(CSS)
        ratio = contrast_ratio(v["warn"], v["warnbg"])
        self.assertGreaterEqual(
            round(ratio, 2), self.MIN,
            "浅色 warn 文字对比度 %.2f:1 < %.1f:1（--warn=%s / --warnbg=%s）"
            % (ratio, self.MIN, v["warn"], v["warnbg"]))


class TestRenderedLayout(unittest.TestCase):
    """真实渲染测量：树线对齐 + 窄窗表格 + warn 通告对比度（headless Chromium，真实 app.css）。

    树线现在错在哪：实测 chevron 三角中心比它引出的子树竖线右偏 5.5px
    （Codex 16.5 vs 11.0，GPT 中转站 42.5 vs 37.0），静态推算还少算 1px。
    用户体验：展开箭头与树线没串成一条轴，层级看起来是散的（截图红框处）。
    改完转绿：每个节点的 |三角中心 − 竖线 x| ≤ 0.5px。

    warn 通告对比度实测 2.07:1（rgb(245,158,11) on rgb(255,251,235)），与静态计算一致，
    但这条读的是真实层叠后的 computed color，能抓住"改了变量却被别的规则覆盖"的假绿。

    表格溢出是防回归断言（当前已绿）：一度怀疑最小窗口下 9 列宽表会横滚，
    实测 900px / 780px 容器下表格都没有撑出容器，故不作为"待修项"，只钉住不退化。
    """

    TOL = 0.5

    @classmethod
    def setUpClass(cls):
        if browser_path() is None:
            raise unittest.SkipTest("找不到 Edge/Chromium，跳过真实渲染测量")
        cls.data = probe()

    def test_chevron_center_matches_guide_x(self):
        bad = [r for r in self.data["chevrons"]
               if r["delta"] is None or abs(r["delta"]) > self.TOL]
        self.assertFalse(
            bad, "箭头中心与树线错位：%s" % [(r["label"], r["delta"]) for r in bad])

    def test_wide_table_fits_container(self):
        o = self.data["overflow"]
        self.assertFalse(
            o["overflows_container"],
            "宽表撑出容器：表格 %dpx > 容器 %dpx" % (o["table"], o["container"]))

    def test_overview_card_layout(self):
        """用量页总览指标卡：高度、大数字字号、图表区占宽（防回归）。"""
        c = self.data.get("metric_card")
        self.assertTrue(c, "夹具里没渲染出总览指标卡")
        self.assertGreaterEqual(c["height"], 236, "卡片高度不足：%spx" % c["height"])
        self.assertGreaterEqual(c["value_font"], 48, "大数字字号过小：%spx" % c["value_font"])
        self.assertLessEqual(
            abs(c["chart_ratio"] - 62), 2,
            "图表区占宽偏离 62%%：%s%%" % c["chart_ratio"])
        self.assertFalse(c["overflows"], "卡片内容撑出边界")

    def test_warn_notice_contrast_rendered(self):
        """warn 通告的对比度按**渲染结果**量（读 computed color/background），不是读变量。

        静态那条只能证明变量值不够；这条证明真实层叠后 `.notice.warn` 拿到的那两个颜色
        确实过不了 4.5:1，避免"改了变量但被别的规则覆盖"的假绿。
        """
        w = self.data.get("warn_contrast")
        self.assertTrue(w, "夹具里没渲染出 .notice.warn")
        self.assertGreaterEqual(
            w["ratio"], 4.5,
            "渲染后 warn 通告对比度 %.2f:1（%s on %s）" % (w["ratio"], w["color"], w["background"]))


class TestCursorConsistency(unittest.TestCase):
    """同一窗口里可点元素的鼠标指针一会儿箭头一会儿手形。

    现在错在哪：.btn / .tabs a / .wbtn 是 cursor:default，而 .u-seg span /
    .ghead / .log-nav-btn 是 cursor:pointer。
    用户体验：点按钮是箭头、点旁边同级的「按天/按周」变手形，像两套界面拼起来的。
    改完转绿：两者口径一致（都 default 或都 pointer）。
    """

    def test_clickables_share_same_cursor(self):
        btn = decl(rule(CSS, ".btn{"), "cursor")
        # 分段项宿主可能是 span（旧）或 button（换标签后），两者取其一
        seg = decl(rule(CSS, ".u-seg span{"), "cursor") or decl(rule(CSS, ".u-seg button{"), "cursor")
        self.assertTrue(btn and seg, "没解析到 .btn / .u-seg 项的 cursor（选择器变了就改断言）")
        self.assertEqual(btn, seg, "按钮 cursor=%s，分段器 cursor=%s，同级控件不一致" % (btn, seg))


# =============================================================== 文案与格式

class TestLogsTimeFormat(unittest.TestCase):
    """错误日志表的时间只给「月-日 时:分」，与全站格式不同。

    现在错在哪：mtime() 返回 `MM-DD HH:MM`，全站其它时间是 `YYYY-MM-DD HH:MM:SS`。
    用户体验：跨年时分不清 12-31 是哪一年，和日志正文里的完整时间戳也对不上。
    改完转绿：mtime 输出带年份。
    """

    def test_mtime_includes_year(self):
        body = func_body(LOGS_JS, "function mtime(v)")
        self.assertTrue(body, "没找到 mtime")
        self.assertTrue("getFullYear" in body, "mtime 仍不带年份（MM-DD HH:MM）")


class TestByteFormatUnified(unittest.TestCase):
    """同一份体积数字，两处格式规则不同。

    现在错在哪：logs.js 的 size() 用 KB 1 位、MB 2 位且没有 GB；app.js 的 bytes()
    统一 1 位且含 GB，但全仓库没人调用。
    用户体验：错误日志表出现 `3.19 MB`，别的组件会是 `3.2 MB`，同一界面两种精度。
    改完转绿：两处归一到同一规则（size 复用 bytes 或补齐同级规则）。
    """

    def test_size_matches_shared_rule(self):
        body = func_body(LOGS_JS, "function size(n)")
        self.assertTrue(body, "没找到 size")
        unified = ("bytes(" in body) or ("GB" in body and "toFixed(1)" in body)
        self.assertTrue(unified, "size() 仍与 bytes() 规则分叉（缺 GB / 精度不一致）")


class TestDegradeNotesLayout(unittest.TestCase):
    """设置页把多条降级说明用全角空格拼成一整段。

    现在错在哪：`notes.map(...).join('　')` 塞进单个 .ps-banner .m。
    用户体验：每条说明本身 60+ 字，多条连起来是一堵字墙，看不出有几个问题、分别对应哪项。
    改完转绿：一条说明一行（不再用全角空格拼接）。
    """

    def test_notes_are_not_one_paragraph(self):
        self.assertTrue("join('　')" not in SETTINGS_JS,
                        "降级说明仍用全角空格拼成一整段")


class TestSilentSuccess(unittest.TestCase):
    """项目铁律 9：操作成功必须静默，不弹成功提示。

    现在错在哪：app.js 的 F5 刷新弹 `toast('数据已刷新','ok')`，logs.js 清空弹
    `toast('日志已清空','ok')`。
    用户体验：每按一次 F5、每次清空日志都跳一个邀功气泡，界面显得吵。
    改完转绿：这两条成功 toast 不再存在（失败才提示）。
    """

    def test_no_success_toast_on_refresh(self):
        self.assertTrue("数据已刷新" not in APP_JS, "F5 刷新仍在弹成功提示")

    def test_no_success_toast_on_clear(self):
        self.assertTrue("日志已清空" not in LOGS_JS, "清空日志仍在弹成功提示")


class TestQuotaCardHasNoFiller(unittest.TestCase):
    """配额卡不得有「配额正常」这类正常态解释副文本（AGENTS.md 铁律 7）。

    这条在提交 a2b5476 已修掉（quotaCardHTML 正常态不再输出副文本），当前是**防回归**
    断言，不计入 FAIL_TO_PASS；留着是因为这类"零信息量自我说明"极易在后续迭代里被加回来。
    历史：旧实现未达上限时渲染 `<span>配额正常</span>`，卡片底部多一截噪音。
    """

    def test_no_normal_state_filler(self):
        self.assertTrue("配额正常" not in USAGE_JS, "配额卡仍在渲染「配额正常」副文本")


class TestMonitorTerms(unittest.TestCase):
    """同一实体必须一个词。CONTEXT.md 明确把「上游」列为禁用词。

    现在错在哪：指标卡写「活跃通道」，同页表格却写「上游来源」「来源」。
    用户体验：得自己推断「活跃通道」和下面的「来源」是不是一回事。
    改完转绿：monitor.js 不再出现「活跃通道」「上游」。
    """

    def test_no_banned_or_inconsistent_terms(self):
        for bad in ("活跃通道", "上游来源", "上游"):
            self.assertTrue(bad not in MONITOR_JS, "monitor.js 里出现 %r" % bad)


class TestBannedTermsElsewhere(unittest.TestCase):
    """禁用词「上游」也在首页与设置页的 UI 文案里。

    现在错在哪：home.js 的「上游地址：」「上游没返回模型」「上游返回了 0 个模型」，
    settings.js 的「上游代理」。
    用户体验：监控页刚统一成「来源」，首页和设置页还叫「上游」，同一个东西两个名字，
    读的人要自己在脑子里对上。
    改完转绿：两处改用「来源」/「服务商」措辞。
    """

    def test_home_has_no_upstream_word(self):
        self.assertTrue("上游" not in HOME_JS, "home.js 仍用禁用词「上游」")

    def test_settings_has_no_upstream_word(self):
        self.assertTrue("上游" not in SETTINGS_JS, "settings.js 仍用禁用词「上游」")


# =============================================================== 键盘可达

class TestSwitchKeyboard(unittest.TestCase):
    """.sw2 开关是 <span>+click，键盘与读屏完全不可用。

    现在错在哪：logs.js / monitor.js 都用 h('span', {class:'sw2'})，无 tabindex、
    无 role、无 keydown。
    用户体验：日志页三个开关（自动刷新 / 隐藏管理流量 / 滚屏锁定）纯键盘用户点不到，
    读屏也读不出开/关。
    改完转绿：宿主元素是 button，或带 role="switch" + tabindex。
    """

    def test_sw2_is_focusable(self):
        for src, name in ((LOGS_JS, "logs.js"), (MONITOR_JS, "monitor.js")):
            seg = near(src, "class: 'sw2'")
            self.assertTrue(seg, name + " 没找到 .sw2 构造")
            focusable = re.search(r"h\('button'|tabindex|role:\s*'switch'", seg)
            self.assertTrue(focusable, name + " 的 .sw2 仍不可聚焦（span + click）")


class TestSegmentedKeyboard(unittest.TestCase):
    """用量页「按天/按周」「7d/30d/90d」是 span，键盘用不了。

    现在错在哪：usage.js 拼的是 `<span class="u-seg">`，只有 click 委托。
    用户体验：键盘用户无法切换统计粒度与时间范围，「长期历史」整块不可达。
    改完转绿：分段项是可聚焦元素（button 或带 tabindex）。
    """

    def test_segments_are_focusable(self):
        seg = near(USAGE_JS, 'class="u-seg"')
        self.assertTrue(seg, "没找到 u-seg 构造")
        self.assertTrue(re.search(r"<button|tabindex", seg),
                        "u-seg 分段项仍不可聚焦")


class TestHomeChipsKeyboard(unittest.TestCase):
    """推理等级芯片与改渠道头必须能用键盘点到。

    等级芯片在共用表单 source-form.js。渠道头编辑从首页挪到管理页，入口是按钮。
    首页上的 .ghead 只显示，不再承担编辑。
    """

    def test_level_chip_focusable(self):
        self.assertTrue(re.search(r"h\('button'|tabindex", near(FORM_JS, "class: 'lv'")),
                        "推理等级芯片仍不可聚焦")

    def test_head_edit_is_a_button(self):
        self.assertIn("function () { editHead(p); }", MANAGE_JS)
        seg = near(MANAGE_JS, "editHead(p)")
        self.assertTrue(re.search(r"h\('button'", seg), "改渠道头的入口不是按钮")


# =============================================================== 交互与状态

class TestHomeRefreshGuard(unittest.TestCase):
    """首页「刷新」会静默丢弃未保存的路由改动。

    现在错在哪：脏状态条与底部动作条的刷新都是 `function () { reload(true); }`，
    而 reload→applyState 会把 S.sel/S.draft 按服务端值重置。
    用户体验：改完勾选误点「刷新」，改动无声消失、界面回到干净态，用户察觉不到丢过东西
    （F5 有脏检查守卫，页内按钮绕过了它）。
    改完转绿：刷新入口带脏检查/确认，裸调用点消失。
    """

    def test_refresh_is_not_bare_reload(self):
        self.assertTrue(
            "function () { reload(true); }" not in HOME_JS,
            "刷新按钮仍是裸 reload(true)，无脏数据守卫")


class TestLevelChipKeepsFocus(unittest.TestCase):
    """点推理等级芯片会重建整张来源表单，正在输入的框连光标一起被销毁。

    现在错在哪：芯片 onclick 末尾调 renderForm()，而 renderForm 先 clear($form) 再整表重建。
    用户体验：在「模型 ID / 显示名称 / 上下文窗口」里打字时顺手点一下 low/high，
    输入框和焦点一起没了，后续输入落空。
    改完转绿：芯片只就地更新自己的 class 与默认等级选项，onclick 段内不再出现 renderForm()。
    """

    def test_chip_toggle_does_not_rebuild_form(self):
        i = FORM_JS.find("class: 'lv'")
        self.assertTrue(i > 0, "没找到等级芯片构造")
        seg = FORM_JS[i:i + 900]
        end = seg.find("}, lv));")
        if end > 0:
            seg = seg[:end]
        self.assertTrue("renderForm()" not in seg, "等级芯片点击仍会重建整张表单")


class TestMonitorRenderThrottle(unittest.TestCase):
    """监控页每 5 秒把整棵 DOM 重建一次。

    现在错在哪：onBusData 收到壳轮询的数据后无条件 render()，而 render 第一件事是
    clear(root)。
    用户体验：驻留监控页时每 5 秒闪一次，来源表的滚动位置被拉回、悬停与文字选中丢失。
    改完转绿：数据与上次一致时跳过重建（加指纹/比较）。
    """

    def test_identical_payload_skips_rebuild(self):
        body = func_body(MONITOR_JS, "function onBusData(m)")
        self.assertTrue(body, "没找到 onBusData")
        self.assertTrue(re.search(r"stringify|fingerprint|lastKey", body),
                        "onBusData 仍无条件 render()，没有数据比对")


class TestUsageRangeSwitch(unittest.TestCase):
    """用量页刷新飞行中切区间会被吞掉，数据与标签对不上。

    现在错在哪：refresh() 开头 `if (unmounted || refreshing) return`，而切区间只是
    `state.days = n; refresh()`；飞行中的那一次直接返回，新请求不发、也不重渲染。
    用户体验：点了「30d」没反应；等旧请求回来，区间高亮已是 30d、表格还是 7d 的数据。
    改完转绿：飞行中的切换被记下并在当前请求结束后补发（refreshPending）。
    """

    def test_range_switch_is_requeued(self):
        self.assertTrue(
            re.search(r"refreshPending|refreshQueued|pendingDays|requeue", USAGE_JS),
            "飞行中的区间切换没有补发机制")


class TestUsageMountRefresh(unittest.TestCase):
    """用量页重新进入不立即刷新，最长 30 秒显示旧数据。

    现在错在哪：mount 里 `if (state.data) render(); else { render(); refresh(); }`，
    有缓存就不请求；定时器要 30s 才首次触发。
    用户体验：切走再切回，看到的是离开时的旧数据和旧时间戳。
    改完转绿：进入即刷新（缓存先渲染可以，但必须发请求）。
    """

    def test_mount_always_refreshes(self):
        self.assertTrue("if (state.data) render(); else" not in USAGE_JS,
                        "mount 仍因缓存跳过刷新")


class TestSettingsLoadRace(unittest.TestCase):
    """设置页「重新读取」没有并发保护，旧响应会覆盖新响应。

    现在错在哪：load() 只判断 alive，没有请求序号；按钮与横幅都可重复点击。
    用户体验：快速连点两次，先发的后返回，用户刚改的值被旧值顶回去，看起来像"自己跳回去"。
    改完转绿：load 带自增令牌，只有最新一次响应的结果被采用。
    """

    def test_load_has_sequence_guard(self):
        self.assertTrue(re.search(r"loadSeq|loadToken|requestSeq", SETTINGS_JS),
                        "load() 仍无请求序号保护")


class TestLogsClearGuard(unittest.TestCase):
    """日志页「清空日志」没有在途守卫，可重复提交。

    现在错在哪：doClear() 不读 S.busy，确认按钮也不禁用。
    用户体验：双击或快速重复确认会连发多次 DELETE，重复 toast 与重复重绘。
    改完转绿：doClear 复用在途标志。
    """

    def test_do_clear_guards_busy(self):
        body = func_body(LOGS_JS, "function doClear()")
        self.assertTrue(body, "没找到 doClear")
        self.assertTrue("S.busy" in body, "doClear 仍无在途守卫")


class TestPageRefreshCapability(unittest.TestCase):
    """日志页与监控页没有导出 refresh，按 F5 只能整页重挂载。

    现在错在哪：app.js 的静默刷新优先调 current.instance.refresh()，没有就兜底
    route(true) 全量重挂载；logs/monitor 的 mount 没返回实例、页面对象也没有 refresh。
    用户体验：在这两页按 F5，日志缓冲、未读计数、滚动位置全部归零，提示却说"数据已刷新"。
    改完转绿：两页都导出 refresh()，F5 走就地刷新。
    """

    def test_logs_exports_refresh(self):
        self.assertTrue("refresh: function" in LOGS_JS, "logs 页未导出 refresh()")

    def test_monitor_exports_refresh(self):
        self.assertTrue("refresh: function" in MONITOR_JS, "monitor 页未导出 refresh()")


class TestPaletteFocus(unittest.TestCase):
    """命令面板没有焦点陷阱，关闭后也不还原焦点。

    现在错在哪：面板的 onKey 只处理 Esc/↑/↓/Enter；close() 只删节点与监听。
    用户体验：Ctrl+K 后按 Tab，焦点跑到遮罩后面的页面上（看不见焦点在哪）；
    关掉面板焦点落到 body，得重新 Tab 一遍才能回到原处。同文件的 dialog 有完整实现。
    改完转绿：面板处理 Tab 循环，并在关闭时把手柄还给打开前的元素。
    """

    def test_palette_traps_and_restores_focus(self):
        i = APP_JS.find("function onKey", APP_JS.find("function onKey") + 1)
        self.assertTrue(i > 0, "没找到命令面板的 onKey")
        palette = APP_JS[i:i + 2000]
        self.assertTrue("'Tab'" in palette or '"Tab"' in palette,
                        "命令面板未处理 Tab，焦点会漏到遮罩后")
        self.assertTrue(re.search(r"prevFocus|activeElement", palette),
                        "命令面板关闭后不还原焦点")


class TestStandaloneFetchTimeout(unittest.TestCase):
    """无壳直开的兜底 fetch 没有超时，后端半死会永久挂住。

    现在错在哪：usage.js 与 monitor.js 的兜底 fetch 都没 AbortController（logs.js 有）。
    用户体验：后端端口通但不回包时，用量页「读取中…」永不恢复、后续刷新全被守卫挡掉；
    监控页刷新按钮永久禁用。
    改完转绿：两处兜底请求都带超时中断。
    """

    def test_usage_fetch_has_timeout(self):
        self.assertTrue("AbortController" in USAGE_JS, "usage 兜底 fetch 无超时中断")

    def test_monitor_fetch_has_timeout(self):
        self.assertTrue("AbortController" in MONITOR_JS, "monitor 兜底 fetch 无超时中断")


# =============================================================== 标记与错误呈现

class TestMissingPanelMarkup(unittest.TestCase):
    """模块加载失败面板把 <b>/<br> 当字面量吐出来。

    现在错在哪：detail 走 ui.empty 的 textContent，而调用方塞了 HTML 标签。
    用户体验：某个 pages/*.js 加载失败时，面板上直接显示"未能加载 <b>pages/home.js</b>
    模块。<br>错误信息"，看起来像界面本身坏了。
    改完转绿：detail 是纯文本，不含标签。
    """

    def test_detail_has_no_html_tags(self):
        body = func_body(APP_JS, "function missingPanel(info, err)")
        self.assertTrue(body, "没找到 missingPanel")
        self.assertTrue("<b>" not in body, "detail 里还有 <b>，会原样显示成标签")
        self.assertTrue("<br>" not in body, "detail 里还有 <br>，会原样显示成标签")


class TestLogsSurfaceError(unittest.TestCase):
    """日志拉取失败只换标题、不显示原因。

    现在错在哪：pull 失败把原因写进 S.err，但 notes() 不消费它，logView 只拿它当布尔
    切换标题。
    用户体验：接口 500/超时/连不上时只看到「未获取到日志」，无法判断是网络、权限还是后端。
    改完转绿：失败原因渲染出来（notice 或 statebox detail）。
    """

    def test_error_reason_is_rendered(self):
        notes = func_body(LOGS_JS, "function notes()")
        logview = func_body(LOGS_JS, "function logView()")
        surfaced = bool(
            re.search(r"notice\([^)]*S\.err", notes)
            or re.search(r"statebox\([^)]*?,\s*S\.err\s*[,)]", logview)
            or re.search(r"notice\([^)]*S\.err", logview)
        )
        self.assertTrue(surfaced, "S.err 只被当作布尔用，失败原因没有渲染")


class TestUsageNumericColumns(unittest.TestCase):
    """用量数字刷新时不能左右跳。

    来源明细和长期历史的成功、失败写在同一行 .nums 里。
    用户体验：30 秒自动刷新时数字位数一变，行尾数字左右微跳。
    改完转绿：.nums 使用 tabular-nums。
    """

    def test_count_lines_use_tabular_nums(self):
        self.assertIn('class="nums"', USAGE_JS)
        i = CSS.find(".usage-page .u-line .nums{")
        self.assertGreater(i, 0, "用量数字行没有 .nums 规则")
        self.assertIn("tabular-nums", CSS[i:i + 220])


class TestCollapsedTreeActions(unittest.TestCase):
    """首页默认收起后，展开、接入预览、来源跳转仍要能落到已挂上的节点。"""

    def test_expand_redraws_instead_of_toggling_missing_nodes(self):
        body = func_body(HOME_JS, "function expandAllNodes()")
        self.assertIn("renderTree()", body)
        self.assertNotIn("querySelectorAll('.gnode')", body)
        self.assertNotIn("S.open['a:' + ag.id] = true", body)

    def test_collapsed_row_does_not_clone_away_preview(self):
        body = func_body(HOME_JS, "function agentNode(a, expanded)")
        self.assertNotIn("cloneNode", body)
        self.assertIn("接入预览", body)

    def test_highlight_opens_agent_and_group(self):
        body = func_body(HOME_JS, "function doHighlight(p)")
        self.assertIn("getActiveAgentId()", body)
        self.assertIn("'a:' + aid", body)
        self.assertIn("'g:' + aid + '/' + gid", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
