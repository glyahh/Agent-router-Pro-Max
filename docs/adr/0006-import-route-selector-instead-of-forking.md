# Prism 通过 import 复用 route_selector.py，不搬它的代码

`script\route_selector.py` 是路由逻辑的唯一实现——615 行、38 个顶层函数、11 个模块级常量。Prism 需要里面的 `snapshot` / `apply_selection` / `connect_client` / `recheck_blocked`。最初的计划是"按资源重写"或者"迁 5 个核心函数到 `app\core\`"，两句话本身就互相矛盾；查过一遍，以这 4 个入口为根做传递闭包，38 个函数里能摸到 37 个（唯一没被摸到的 `upstream_models` 本来就是死代码）。按 5 个搬，import 阶段就 NameError。

更硬的一条是路径。文件第 8 行是 `ROOT = Path(__file__).resolve().parent.parent`——它靠自己的位置往上两级找到 `D:\My_Agent_Proxy`。往 `app\core\` 搬一层，`ROOT` 就变成 `app\`，`config.yaml`、`routing-plan.json`、`codex-model-catalog*.json`、`auth\` 四个文件全都找不到。搬运不是移动代码，是移动一堆隐式的地基。

决定：`app\core\bridge.py` 里加 `sys.path` 再 `import route_selector as rs`，函数直接转调，不重写。这个 import 是干净的——启动代码在 `if __name__ == '__main__'` 保护里，实测 import 后没有残留线程、没有监听端口、`ROOT` 解析到 `D:\My_Agent_Proxy`。逻辑只有一份，就不会分叉：今天修好选择页的 bug，Prism 同时就好了。

**代价要写清楚，而且是三条。**

第一，Prism 从此依赖那个文件**存在且未被改动**。删掉它、挪走它、改一行，Prism 都起不来。兜底是文件哈希校验：`bridge.ensure_route_selector_intact()` 在启动时比对 sha256，写定的期望值是 `427d197ba44c4ab1c78260a2ae3e03e9f1e0cd413138fb725ff024744ef9796b`（2026-09-26 的值）。不匹配就明确报错，**不静默降级**——最坏的情况是 Prism 用着一份它以为是 A 实际是 B 的逻辑，把用户的路由改错。

这个哈希不是防攻击的，是防"我以为改的是那边"。以后真要动 `route_selector.py`，改完必须同步更新这个常量，否则 Prism 会一直拒绝启动。反过来说，任何"顺手给 route_selector.py 升级一下"的自动更新都是陷阱，别做。

第二，import 给你的是函数，**不是并发安全**。第 11 行的 `LOCK = threading.Lock()` 是模块级的，Prism 导入后拿到的是它自己进程里的一份，跨进程什么也挡不住。而 `write_json`（第 18-21 行）的临时文件名固定是 `path + '.tmp'`，不含 pid。两个进程同时写 `routing-plan.json`，实测各写 600 次，失败 500 多次。所以 Prism 自己另加了 pid 后缀的临时名和跨进程命名互斥——这部分是新增的，不是 import 白送的。

第三，两边不能同时跑。老的 `route_selector.py` 和 Prism 都监听 8318，靠 `SO_EXCLUSIVEADDRUSE` 互斥：谁后启动谁报错，不会有一边静默应答另一边的页面。回退到命令行模式时必须先退出 Prism，步骤写在 `app\README.md`。

出处见 `C:\Users\user\.claude\plans\agent-codex-gpt-deepseek-glm-url-key-ds-shimmering-snowflake.md` 的修正 B1 / B2。
