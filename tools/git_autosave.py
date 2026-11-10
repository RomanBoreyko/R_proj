"""Автосейв волта R_vault в GitHub (RomanBoreyko/R_proj).

git add -A → проверка staged-файлов на секреты/ПДн → commit → push.
Если найдено что-то подозрительное — НИЧЕГО не коммитит, пишет причину в лог
и снимает файлы со stage. Запуск: python tools/git_autosave.py [--dry-run]
Лог: tools/git_autosave.log (в .gitignore через *.log).
"""
import os, re, subprocess, sys, datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(REPO, "tools", "git_autosave.log")
DRY = "--dry-run" in sys.argv
MAX_BYTES = 50 * 1024 * 1024  # GitHub режет >100 МБ, предупреждает >50 МБ
TEXT_EXT = {".md", ".txt", ".json", ".py", ".js", ".ts", ".html", ".csv",
            ".yml", ".yaml", ".toml", ".ini", ".cfg", ".canvas", ".pine", ".ps1", ".sh"}

SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    r"|sk-[A-Za-z0-9_\-]{30,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|(?i:(?:api[_-]?key|api[_-]?secret|secret[_-]?key|access[_-]?token|password|passwd|pass)\s*[:=]\s*['\"]?(?!\[)[A-Za-z0-9/+_\-]{12,})"
)
CARD = re.compile(r"(?<!\d)(?:\d[ \-]?){15,18}\d(?!\d)")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b")
BAD_NAMES = re.compile(r"(\.kdbx|\.pem|\.p12|\.pfx|id_rsa|credentials\.json|\.env)$", re.I)


def luhn(d):
    s = 0
    for i, c in enumerate(reversed(d)):
        n = int(c)
        if i % 2:
            n *= 2
            n -= 9 if n > 9 else 0
        s += n
    return s % 10 == 0


def iban_ok(s):
    s = s.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    r = s[4:] + s[:4]
    try:
        return int("".join(str(int(c, 36)) for c in r)) % 97 == 1
    except ValueError:
        return False


def git(*a, check=True):
    return subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=check)


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:  # pythonw: stdout = None; консоль cp1251 — не падаем из-за вывода
        print(line.encode("ascii", "replace").decode() if sys.stdout and sys.stdout.encoding.lower() != "utf-8" else line)
    except Exception:
        pass
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def scan(path):
    """Вернуть причину блокировки или None. Значения секретов не печатаются."""
    full = os.path.join(REPO, path)
    if BAD_NAMES.search(path):
        return "запрещённый тип файла"
    try:
        if os.path.getsize(full) > MAX_BYTES:
            return "файл > 50 МБ"
    except OSError:
        return None  # удалённый файл
    if os.path.splitext(path)[1].lower() not in TEXT_EXT:
        return None
    try:
        t = open(full, encoding="utf-8", errors="ignore").read()
    except OSError:
        return None
    if SECRET.search(t):
        return "похоже на ключ/пароль"
    for m in CARD.finditer(t):
        d = re.sub(r"\D", "", m.group())
        if len(d) == 16 and d[0] in "2345" and luhn(d):
            return "номер карты"
    for m in IBAN.finditer(t):
        if iban_ok(m.group()):
            return "IBAN"
    return None


def main():
    git("add", "-A")
    files = [f for f in git("diff", "--cached", "--name-only", "-z").stdout.split("\0") if f]
    if not files:
        log("нет изменений")
        return 0
    bad = [(f, r) for f in files if (r := scan(f))]
    if bad:
        git("reset", "-q")
        for f, r in bad:
            log(f"БЛОК: {f} — {r}")
        log(f"коммит отменён: {len(bad)} подозрительных файлов из {len(files)}")
        return 2
    if DRY:
        git("reset", "-q")
        log(f"dry-run: ок, {len(files)} файлов были бы закоммичены")
        return 0
    git("commit", "-q", "-m", f"[autosave] {datetime.datetime.now():%Y-%m-%d %H:%M} ({len(files)} файлов)")
    p = git("push", "-q", "origin", "main", check=False)
    if p.returncode:
        log(f"commit ок, push НЕ прошёл: {p.stderr.strip()[:300]}")
        return 3
    log(f"ок: {len(files)} файлов → GitHub")
    return 0


if __name__ == "__main__":
    sys.exit(main())
