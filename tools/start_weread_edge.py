#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键拉起调试 Edge(有头)用于本地公众号抓取(CDP 复用会话).

用法:  python tools/start_weread_edge.py
作用:  用固定 --user-data-dir + --remote-debugging-port 拉起一个有头 Edge 并导航到微信读书登录页。
      仅在"你是第一次"时才需要扫码: 之后 Edge 里已登录+已开阅读器页时, 直接跑主任务即可。

依赖: 系统装有 Microsoft Edge。
"""
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.crawler import weread_cdp  # noqa: E402


def main():
    port = weread_cdp.edge_port()
    profile = weread_cdp.edge_profile()
    dbg = f"http://127.0.0.1:{port}"

    if weread_cdp._debug_port_alive(port):
        print(f"[已就绪] 调试 Edge 已在端口 {port} 运行, 直接复用即可。")
        print(f"  地址: {dbg}")
        print("  无需重新启动。接下来可在已打开的窗口中扫码/登录/点开阅读器页。")
        return 0

    exe = weread_cdp.find_edge_executable()
    if not exe:
        print("错误: 找不到 Edge(msedge.exe)。请安装 Microsoft Edge。")
        return 2

    print("正在拉起调试 Edge ...")
    try:
        weread_cdp.find_or_launch_edge(port, profile)
    except Exception as e:
        print(f"拉起失败: {e}")
        return 1

    print()
    print("=" * 60)
    print("请在刚弹出的 Edge 窗口中完成以下(仅首次/登录失效时需要):")
    print("  1) 用手机微信【扫一扫】登录微信读书 (weread.qq.com)")
    print("  2) 点进任意一个你订阅的公众号")
    print("  3) 打开该公众号的【阅读器页】并保持这个标签页打开")
    print("     阅读器页 URL 形如:")
    print("       https://weread.qq.com/web/mp/reader/<hash>")
    print()
    print("完成后, 直接运行主任务抓取即可(无需再复制任何 cookie):")
    print("   python src/main.py")
    print("   (调试提速可加环境变量 FAST_TEST=1)")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
