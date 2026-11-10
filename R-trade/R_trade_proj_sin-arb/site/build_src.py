"""Собирает исходники сайта-конспекта (GitHub Pages, MkDocs) из TRADING-SYSTEM.

Копирует нужные заметки в site/docs/ и приводит Obsidian-разметку к MkDocs:
  [[wikilink|alias]]   -> относительная md-ссылка (неразрешённые -> просто текст)
  ![[embed]]           -> текст-заглушка
  > [!note]- Заголовок -> ??? note "Заголовок"   (сворачиваемый блок, pymdownx.details)
  <details><summary>X</summary> ... </details> -> ??? note "X"
Оригиналы в TRADING-SYSTEM не меняются. Только stdlib.
Запуск: python site/build_src.py   (из R-trade/R_trade_proj_sin-arb)
"""
import os, re, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "TRADING-SYSTEM")
DST = os.environ.get("SITE_DOCS") or os.path.join(HERE, "docs")  # локально лучше вне волта (Obsidian/синхронизация)
DIRS = ["13-LEARNING", "12-CONCEPTS", "03-STRATEGIES", "10-DASHBOARD", "11-ASSETS", "06-RISK"]
FILES = ["scripts/portfolio/DASHBOARD.md"]

WL = re.compile(r"(!?)\[\[([^\]\|#]+?)(#[^\]\|]+)?((?:\\?\|)[^\]]+)?\]\]")
CODE = re.compile(r"(```.*?```|`[^`\n]+`)", re.S)
CALLOUT = re.compile(r"^> ?\[!(\w+)\]([-+]?) ?(.*)$")
DETAILS = re.compile(r"<details>\s*<summary>(.*?)</summary>\s*\n(.*?)</details>", re.S)


def copy():
    if os.path.isdir(DST):
        shutil.rmtree(DST)
    out = []
    for d in DIRS:
        for root, _, files in os.walk(os.path.join(SRC, d)):
            for f in files:
                if f.endswith(".md"):
                    out.append(os.path.relpath(os.path.join(root, f), SRC))
    out += [f.replace("/", os.sep) for f in FILES if os.path.isfile(os.path.join(SRC, f))]
    for rel in out:
        dst = os.path.join(DST, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(os.path.join(SRC, rel), dst)
    shutil.copyfile(os.path.join(HERE, "index.md"), os.path.join(DST, "index.md"))
    return out


def fix_links(text, here_dir, idx):
    def rep(m):
        emb, name, head, alias = m.group(1), m.group(2).strip(), m.group(3), m.group(4)
        label = alias.lstrip("\\").lstrip("|") if alias else name.rsplit("/", 1)[-1]
        if emb:
            return "*(%s — см. в Obsidian)*" % label
        path = idx.get(name.rsplit("/", 1)[-1])
        if not path:
            return label
        rel = os.path.relpath(path, here_dir).replace("\\", "/").replace(" ", "%20")
        if head:
            rel += "#" + re.sub(r"[^\w\- ]", "", head[1:].strip().lower()).replace(" ", "-")
        return "[%s](%s)" % (label, rel)
    parts = CODE.split(text)
    for i in range(0, len(parts), 2):
        # «\$» внутри формул arithmatex принимает за границу формулы -> MathJax \unicode{36}
        # (сначала внутри \text{} — там макросы MathJax не работают -> «USD»)
        parts[i] = re.sub(r"\\text\{([^{}]*)\}",
                          lambda t: r"\text{" + t.group(1).replace(r"\$", "USD") + "}", WL.sub(rep, parts[i]))
        parts[i] = "\n".join(fix_dollars(l) for l in parts[i].split("\n"))
        parts[i] = parts[i].replace(r"\$", r"\unicode{36}")
        # однострочный $$…$$ сразу после текста (Obsidian так можно, MkDocs — нет) -> inline \displaystyle
        parts[i] = re.sub(r"(?m)(?<=\S\n)^(\s*)\$\$(.+?)\$\$[ \t]*$", r"\1$\\displaystyle \2$", parts[i])
    return "".join(parts)


CYR = re.compile(r"[А-Яа-яЁё]")


def _is_math(seg):
    """Кусок между $…$ похож на формулу: без кириллицы вне \\text{}/индексов и без **жирного**."""
    t = re.sub(r"\\(?:text|mathrm|operatorname)\{[^{}]*\}", "", seg)
    t = re.sub(r"_\{[^{}]*\}|_\S", "", t)
    return not CYR.search(t) and "**" not in t


def fix_dollars(line):
    """Валютный «$» в прозе (≈ $377, Γ$ на 1%) -> &#36;, чтобы arithmatex не принял его за формулу.
    Правила как у Pandoc: после открывающего $ нет пробела, перед закрывающим нет пробела,
    за закрывающим не цифра; плюс содержимое должно быть похоже на формулу."""
    s, out, i, n = line, [], 0, len(line)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            out.append(s[i:i + 2]); i += 2; continue
        if c != "$":
            out.append(c); i += 1; continue
        if s.startswith("$$", i):
            j = s.find("$$", i + 2)
            if j < 0:
                out.append(s[i:]); break
            out.append(s[i:j + 2]); i = j + 2; continue
        if i + 1 < n and not s[i + 1].isspace():
            j = i + 1
            while j < n:
                if s[j] == "\\":
                    j += 2; continue
                if s[j] == "$" and not s[j - 1].isspace() and not (j + 1 < n and s[j + 1].isdigit()):
                    break
                j += 1
            if j < n and _is_math(s[i + 1:j]):
                out.append(s[i:j + 1]); i = j + 1; continue
        out.append("&#36;"); i += 1
    return "".join(out)


def fix_callouts(text):
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        m = CALLOUT.match(lines[i])
        if not m:
            out.append(lines[i]); i += 1; continue
        kind, fold, title = m.group(1).lower(), m.group(2), m.group(3).strip()
        mark = "???" if fold == "-" else ("???+" if fold == "+" else "!!!")
        out.append('%s %s "%s"' % (mark, kind, title.replace('"', "'")) if title else "%s %s" % (mark, kind))
        i += 1
        while i < len(lines) and lines[i].startswith(">"):
            out.append("    " + re.sub(r"^> ?", "", lines[i])); i += 1
        out.append("")
    return "\n".join(out)


def fix_details(text):
    def rep(m):
        body = "\n".join("    " + l if l.strip() else "" for l in m.group(2).strip("\n").split("\n"))
        return '??? note "%s"\n\n%s\n' % (m.group(1).strip().replace('"', "'"), body)
    return DETAILS.sub(rep, text)


COURSE = "13-LEARNING/Арбитражный портфель"
COURSE_ORDER = ["АП-00", "АП-01", "АП-02", "АП-03", "АП-04", "АП-05", "АП-06", "АП-07",
                "АП-08", "АП-09", "АП-13", "АП-10", "АП-11", "АП-12"]
SECTIONS = [("Понятия", "12-CONCEPTS"), ("Стратегии", "03-STRATEGIES"),
            ("Активы", "11-ASSETS"), ("Дашборд", "10-DASHBOARD")]


def q(s):
    return '"%s"' % s.replace('"', "'")


def write_nav(files):
    rels = sorted(f.replace(os.sep, "/") for f in files)
    L = ["# сгенерировано build_src.py — не править", "INHERIT: mkdocs.yml", "nav:",
         "  - Главная: index.md", "  - Курс «Арбитражный портфель»:"]
    course = [r for r in rels if r.startswith(COURSE + "/")]
    key = lambda r: next((i for i, p in enumerate(COURSE_ORDER) if os.path.basename(r).startswith(p)), 99)
    for r in sorted(course, key=key):
        title = os.path.basename(r)[:-3]
        title = title.replace("АП-00 ", "").replace("АП-", "", 1) if title.startswith("АП-") else title
        title = re.sub(r"^(\d\d) ", lambda m: "%d. " % int(m.group(1)) if int(m.group(1)) <= 9 or m.group(1) == "13" else "", title)
        L.append("      - %s: %s" % (q(title), q(r)))
    for name, d in SECTIONS:
        items = [r for r in rels if r.startswith(d + "/")]
        if items:
            L.append("  - %s:" % name)
            L += ["      - %s: %s" % (q(os.path.basename(r)[:-3]), q(r)) for r in items]
    L.append("  - Правила риска: 06-RISK/risk-rules.md")
    L.append("  - DASHBOARD.md (тех. доки): scripts/portfolio/DASHBOARD.md")
    open(os.environ.get("SITE_GEN") or os.path.join(HERE, "mkdocs.gen.yml"), "w", encoding="utf-8", newline="\n").write("\n".join(L) + "\n")


def main():
    files = copy()
    os.makedirs(os.path.join(DST, "js"), exist_ok=True)
    shutil.copyfile(os.path.join(HERE, "mathjax.js"), os.path.join(DST, "js", "mathjax-config-2.js"))
    write_nav(files)
    idx = {}
    for rel in files:
        idx.setdefault(os.path.splitext(os.path.basename(rel))[0], os.path.join(DST, rel))
    for rel in files + ["index.md"]:
        p = os.path.join(DST, rel)
        s = open(p, encoding="utf-8").read()
        s = fix_links(s, os.path.dirname(p), idx)
        s = fix_details(s)
        s = fix_callouts(s)
        open(p, "w", encoding="utf-8", newline="\n").write(s)
    print("site/docs: %d заметок" % (len(files) + 1))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
