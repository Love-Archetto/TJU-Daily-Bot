#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一次性回填: 清掉 state.json 里公众号旧链接记录, 用新链路重新收录.

背景: /web/mp/articles 的 originalId 变成加密长串后, 旧代码拼出的 /s/<长串> 是死链;
现改为逐篇调 /web/mp/content 从正文页 og:url 取规范链接(全格式, 带 chksm)。链接格式变了,
state.json 里存的旧短码记录再也匹配不上新链接 —— 不清理的话每篇都会被判成"新文章"永久重复收录。
按用户要求: 删掉旧记录, 重新收录。

做什么:
  1. 备份 state.json 到系统临时目录(可回滚)
  2. 保留网页链接, 删除 processed_links 里全部 mp.weixin.qq.com 记录
  3. 走 weread_subscribe(CDP → 书架 → articles → content)重新抓全部在列文章
  4. 明细写到 tools/_backfill_out.jsonl(含正文片段, 供人工核验; 不进 output/, 不污染报告)
  5. 把新链接(去重)写回 state["processed_links"], 用 src.main.save_state 原子落盘

环境变量:
  LIMIT_ACCOUNTS=N  只处理前 N 个订阅号(试跑用)
  LIMIT_ITEMS=N     每号只处理前 N 篇(试跑用)
  DRY_RUN=1         只抓不写 state.json(仍写 jsonl 明细)
  FAST_TEST=1       请求间隔跳过(调试提速)
  WEREAD_INTERVAL   请求间隔均值秒数(默认 3; 回填量大时可调小)
  BACKFILL_ALL=1    关掉回溯窗口, 全部在列文章都重收(慢 5 倍以上)
  MIN_ARTICLES=N    产量下限(默认 20): 抓到少于 N 篇就不写 state.json, 防止
                    大面积抓取失败时把未抓到号的历史记录一并清掉

默认按"日常回溯窗口"(WECHAT_LOOKBACK_DAYS, 默认 3 天)重收: 窗口**之外**的旧文章爬虫
永远不会再产出链接(日常抓取同样受该窗口限制), 所以那些旧记录删掉也没有副作用 ——
需要重复收录的只有窗口内的文章。要连历史一起换成全格式链接时才用 BACKFILL_ALL=1。

用法:
  python tools/backfill_wechat.py            # 全量回填
  LIMIT_ACCOUNTS=2 LIMIT_ITEMS=3 DRY_RUN=1 python tools/backfill_wechat.py   # 小样试跑
"""

import json
import logging
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

OUT_JSONL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_backfill_out.jsonl")

# 写盘前的产量下限: 抓到少于这么多篇就判定本轮大面积失败, 不写 state.json(见下方护栏)
_RAW_MIN = os.environ.get("MIN_ARTICLES", "").strip()
MIN_ARTICLES = int(_RAW_MIN) if _RAW_MIN.isdigit() else 20


def main() -> int:
    from src import main as M
    from src.crawler import weread_subscribe as ws

    # 复用主流程的日志配置: 控制台 + logs/run-*.log(UTF-8)。
    # 原先只 basicConfig 到 stderr, 而本脚本常要跑几十分钟, 中断/失败后无从查证。
    log_path = M._setup_logging()
    logging.getLogger(__name__).info("回填开始, 日志: %s", log_path or "(未落盘)")

    limit_accounts = int(os.environ.get("LIMIT_ACCOUNTS", "0") or 0)
    limit_items = int(os.environ.get("LIMIT_ITEMS", "0") or 0)
    dry = os.environ.get("DRY_RUN", "") == "1"
    # 默认沿用日常回溯窗口(3 天); BACKFILL_ALL=1 才关掉窗口做全量重收
    if os.environ.get("BACKFILL_ALL", "") == "1":
        os.environ["WECHAT_LOOKBACK_DAYS"] = "0"
    # WEREAD_MAX_* 在 weread_subscribe 导入时读取, 必须先设再导入
    if limit_accounts:
        os.environ["WEREAD_MAX_ACCOUNTS"] = str(limit_accounts)
    if limit_items:
        os.environ["WEREAD_MAX_ITEMS_PER_SOURCE"] = str(limit_items)

    state = M.load_state()
    old = list(state.get("processed_links", []))
    mp_old = [l for l in old if "mp.weixin.qq.com" in l]
    keep = [l for l in old if "mp.weixin.qq.com" not in l]
    print(f"state.json: processed_links={len(old)}  (mp.weixin {len(mp_old)}, 其它 {len(keep)})")

    # 备份(可回滚)
    backup = os.path.join(tempfile.gettempdir(),
                          f"tju_state_backup_{time.strftime('%Y%m%d_%H%M%S')}.json")
    shutil.copy2(M.STATE_PATH, backup)
    print(f"已备份 state.json → {backup}")

    print(f"开始抓取(限号={limit_accounts or '全部'}, 每号限篇={limit_items or '全部'}) ...")

    # 预检: 书架拿不到订阅号 = weread 登录态失效(errCode -2010)或 Edge 未就绪。
    # 此时抓取会返回 0 篇 —— 而"0 篇"绝不能被当成"重新收录完成": 那会把旧记录
    # 清空后写入空集, 等于把去重历史抹掉(2026-10-13 就这么干过一次)。宁可不写。
    # -2010 偶尔是瞬时抖动, 所以重试一次再判死。
    subs = ws.list_subscribed("")
    if not subs:
        print("书架返回空, 5s 后重试一次 ...")
        time.sleep(5)
        subs = ws.list_subscribed("")
    if not subs:
        print("错误: 书架接口没返回任何订阅号 —— 大概率是 weread 登录态失效(errCode -2010)。")
        print("       state.json 未被改动。请先恢复登录:")
        print("       1) python tools/start_weread_edge.py")
        print("       2) 在弹出的 Edge 里重新扫码登录微信读书, 并点进任一订阅号的阅读器页")
        print("       3) 确认页面标题出现「XXX - 公众号」后重跑本脚本")
        return 1

    t0 = time.time()
    articles, inactive = ws.fetch_subscribed_articles(seen_links=set())
    dt = time.time() - t0
    print(f"抓取完成: {len(articles)} 篇, 耗时 {dt:.0f}s, 失效号 {len(inactive)}")
    # 产量下限: 92 个号 × 回溯窗口, 正常应产出上百篇。数量过少说明本轮抓取大面积失败
    # (部分号 errCode / 网络抖动), 此时写盘会把没抓到的那些号的历史记录一起清掉。
    if len(articles) < MIN_ARTICLES:
        print(f"错误: 只抓到 {len(articles)} 篇(< MIN_ARTICLES={MIN_ARTICLES}), "
              f"疑似大面积失败, state.json 未被改动。")
        print(f"       确认确实该这么少时, 用 MIN_ARTICLES=1 重跑。请先看日志: {log_path or 'logs/run-*.log'}")
        return 1

    # 明细落盘(核验用)
    with open(OUT_JSONL, "w", encoding="utf-8") as f:
        for a in articles:
            f.write(json.dumps({
                "source": a.get("source", ""),
                "title": a.get("title", ""),
                "link": a.get("link", ""),
                "publish_time": a.get("publish_time", ""),
                "content_len": len(a.get("content") or ""),
                "image": bool(a.get("image")),
                "content_head": (a.get("content") or "")[:200],
            }, ensure_ascii=False) + "\n")
    print(f"明细 → {OUT_JSONL}")

    new_links = [a["link"] for a in articles if a.get("link")]
    merged, seen = [], set()
    for l in keep + new_links:
        if l and l not in seen:
            seen.add(l)
            merged.append(l)
    if len(merged) > M.MAX_PROCESSED_LINKS:
        merged = merged[-M.MAX_PROCESSED_LINKS:]
        print(f"警告: 超过 MAX_PROCESSED_LINKS={M.MAX_PROCESSED_LINKS}, 已截断保留最新")

    with_content = sum(1 for a in articles if a.get("content"))
    print(f"新链接 {len(new_links)} 条(去重后 {len(merged)} 条写入); "
          f"其中带正文 {with_content}/{len(articles)}")

    if dry:
        print("DRY_RUN=1, 不写 state.json")
        return 0
    state["processed_links"] = merged
    M.save_state(state)
    print(f"已写回 state.json: processed_links={len(merged)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
