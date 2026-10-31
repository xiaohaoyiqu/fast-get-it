import json
import re
from pathlib import Path
from urllib.parse import unquote


def headings(path):
    anchors = set()
    duplicates = {}
    inside = False
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
        if inside:
            continue
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            base = re.sub(r"[^\w\- ]", "", match[1].lower()).replace(" ", "-")
            count = duplicates.get(base, 0)
            duplicates[base] = count + 1
            anchors.add(base if not count else f"{base}-{count}")
    return anchors


ROOT = Path(__file__).resolve().parents[1]
paths = [ROOT / "README.md", ROOT / "tests/README.md"]
paths.extend(
    ROOT / "文档" / name for name in ("README.md", "用户手册.md", "开发与架构说明.md", "插件开发.md", "鸣谢.md")
)
paths.extend(sorted((ROOT / "文档/平台").glob("*.md")))
paths.extend(sorted((ROOT / "software_app/crawlers/twitter").glob("*.md")))
errors = []
link_count = 0
json_count = 0
for value in filter(None, paths):
    path = Path(value)
    content = path.read_text(encoding="utf-8-sig")
    for match in re.finditer(r"(?<!!)\[[^\]]*\]\(([^)\n]+)\)", content):
        target = match[1].strip().strip("<>")
        if re.match(r"^[a-zA-Z]+://", target):
            continue
        relative, _, fragment = unquote(target).partition("#")
        destination = (path.parent / relative).resolve() if relative else path.resolve()
        link_count += 1
        if not destination.exists():
            errors.append(f"{path}: missing local target {target}")
        elif fragment and destination.suffix.lower() == ".md" and fragment not in headings(destination):
            errors.append(f"{path}: missing heading {target}")
    for match in re.finditer(r"^```json\s*\n(.*?)^```", content, re.MULTILINE | re.DOTALL):
        json_count += 1
        try:
            json.loads(match[1])
        except json.JSONDecodeError as exc:
            errors.append(f"{path}: JSON example near line {content[: match.start()].count(chr(10)) + 1}: {exc.msg}")
print(
    f"Checked {len(list(filter(None, paths)))} Markdown files, {link_count} local links and {json_count} JSON examples"
)
print("\n".join(errors) if errors else "Documentation checks passed")
raise SystemExit(bool(errors))
