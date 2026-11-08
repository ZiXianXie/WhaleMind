"""把 pytest 的 junit.xml 变成一张能直接打开看的 HTML 报告。

用法：
    python -m pytest --junitxml=reports/junit.xml
    python tools/make_report.py
"""

import html
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
XML = ROOT / "reports" / "junit.xml"
OUT = ROOT / "reports" / "tests.html"

GROUP_META = {
    "test_self_study": ("采集与消化（self_study）", "判断什么该记、什么该丢，长文怎么切，日志时间怎么规范化"),
    "test_recall_history": ("历史检索（recall_history）", "切词、片段截取、配置钳制、坏入参不崩"),
    "test_context_budget": ("上下文预算（context_budget）", "滞回裁剪、剥老图、保留最近消息"),
    "test_memory_guards": ("记忆准入（memory_vault / emotion_memory）", "哪些内容不该进记忆库"),
}


def parse():
    tree = ET.parse(XML)
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else list(root)

    total = failures = errors = skipped = 0
    seconds = 0.0
    groups = defaultdict(list)

    for suite in suites:
        seconds += float(suite.get("time", 0) or 0)
        for case in suite:
            if case.tag != "testcase":
                continue
            name = case.get("name", "")
            cls = (case.get("classname") or "").split(".")[-1]
            group = cls or "misc"
            outcome = "passed"
            message = ""
            for child in case:
                if child.tag in ("failure", "error"):
                    outcome = child.tag
                    message = (child.get("message") or "").strip()
                elif child.tag == "skipped":
                    outcome = "skipped"

            total += 1
            failures += outcome == "failure"
            errors += outcome == "error"
            skipped += outcome == "skipped"
            groups[group].append(
                {"name": name.split("[")[0], "param": name[len(name.split("[")[0]):],
                 "outcome": outcome, "message": message,
                 "time": float(case.get("time", 0) or 0)}
            )
    return groups, {"total": total, "failures": failures, "errors": errors,
                    "skipped": skipped, "seconds": seconds}


def render(groups, stats):
    ok = stats["total"] - stats["failures"] - stats["errors"] - stats["skipped"]
    rows = []
    for group, cases in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        title, desc = GROUP_META.get(group, (group, ""))
        items = "".join(
            f'<li class="{"ok" if c["outcome"] == "passed" else "bad"}">'
            f'<span class="dot"></span>{html.escape(c["name"])}'
            f'<em>{html.escape(c["param"])}</em></li>'
            for c in cases
        )
        rows.append(
            f'<section><header><h2>{html.escape(title)}</h2>'
            f'<span class="count">{len(cases)} 条</span></header>'
            f'<p class="desc">{html.escape(desc)}</p><ul>{items}</ul></section>'
        )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Whale Mind 测试报告</title>
<style>
:root {{ color-scheme: light dark; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 48px 24px 72px; background: #0e1116; color: #e6edf3;
       font: 15px/1.65 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }}
.wrap {{ max-width: 880px; margin: 0 auto; }}
h1 {{ font-size: 26px; margin: 0 0 6px; letter-spacing: .3px; }}
.sub {{ color: #8b949e; margin: 0 0 28px; font-size: 13px; }}
.stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 34px; }}
.stat {{ background: #161b22; border: 1px solid #21262d; border-radius: 10px; padding: 14px 16px; }}
.stat b {{ display: block; font-size: 24px; font-weight: 600; line-height: 1.2; }}
.stat span {{ color: #8b949e; font-size: 12px; }}
.pass b {{ color: #3fb950; }}
section {{ background: #161b22; border: 1px solid #21262d; border-radius: 12px;
           padding: 18px 20px; margin-bottom: 14px; }}
section header {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }}
h2 {{ font-size: 15px; margin: 0; font-weight: 600; }}
.count {{ color: #8b949e; font-size: 12px; white-space: nowrap; }}
.desc {{ color: #8b949e; font-size: 12.5px; margin: 4px 0 12px; }}
ul {{ list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px 10px; }}
li {{ font-size: 12.5px; color: #adbac7; display: flex; align-items: center; gap: 6px;
      background: #1c2128; border-radius: 6px; padding: 4px 9px; }}
li em {{ color: #6e7681; font-style: normal; font-size: 11px; }}
.dot {{ width: 6px; height: 6px; border-radius: 50%; background: #3fb950; display: inline-block; }}
li.bad .dot {{ background: #f85149; }}
footer {{ color: #6e7681; font-size: 12px; margin-top: 28px; text-align: center; }}
</style></head>
<body><div class="wrap">
<h1>Whale Mind 测试报告</h1>
<p class="sub">生成于 {datetime.now():%Y-%m-%d %H:%M}　·　纯函数逻辑测试，不依赖 AstrBot 运行时</p>
<div class="stats">
  <div class="stat pass"><b>{ok}</b><span>通过</span></div>
  <div class="stat"><b>{stats['total']}</b><span>用例总数</span></div>
  <div class="stat"><b>{stats['failures'] + stats['errors']}</b><span>失败</span></div>
  <div class="stat"><b>{stats['seconds']:.2f}s</b><span>总耗时</span></div>
</div>
{''.join(rows)}
<footer>python -m pytest　·　CI 矩阵：ubuntu / windows × Python 3.10 / 3.12</footer>
</div></body></html>
"""


def main():
    if not XML.is_file():
        raise SystemExit("先跑：python -m pytest --junitxml=reports/junit.xml")
    groups, stats = parse()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(groups, stats), encoding="utf-8")
    print(f"已生成 {OUT}（{OUT.stat().st_size} 字节，{stats['total']} 条用例）")


if __name__ == "__main__":
    main()
