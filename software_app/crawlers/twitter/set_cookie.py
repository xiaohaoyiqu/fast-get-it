import argparse
import json
import shutil
from datetime import datetime
from getpass import getpass
from pathlib import Path


COOKIE_PATH = Path("X_cookie.json")
X_DOMAIN = ".x.com"
REQUIRED_COOKIES = {"auth_token", "ct0"}
COOKIE_GUIDE = """
获取 cookie 的方式：

方式一：复制整段 Cookie 请求头，最推荐
1. 用你平时使用的 Chrome 或 Edge 打开 https://x.com/，确认账号已经正常登录。
2. 按 F12 打开开发者工具，切到 Network / 网络。
3. 刷新 x.com 页面。
4. 在 Network 列表中点任意一个 x.com 请求，通常选 HomeTimeline、UserTweets、UserMedia、graphql 或 x.com 主文档都可以。
5. 在 Headers / 标头 中找到 Request Headers / 请求标头。
6. 找到 Cookie: 这一行，复制冒号后面的整段内容。
7. 运行 python .\\set_cookie.py，然后把整段 Cookie 粘贴进去。

方式二：手动复制 auth_token 和 ct0
1. 用普通浏览器登录 https://x.com/。
2. 按 F12 打开开发者工具，切到 Application / 应用。
3. 左侧 Storage / 存储 -> Cookies -> https://x.com。
4. 分别找到 auth_token 和 ct0，复制 Value / 值。
5. 运行 python .\\set_cookie.py，不粘贴整段 Cookie 时，按提示分别输入 auth_token 和 ct0。

方式三：用浏览器插件导出
1. 在普通浏览器确认 x.com 已登录。
2. 用 cookie 导出插件导出 x.com 的 cookie，格式可以是 JSON 或 cookies.txt / Netscape。
3. 运行 python .\\set_cookie.py D:\\path\\cookies.json
   或 python .\\set_cookie.py D:\\path\\cookies.txt

注意：
- cookie 等同登录凭证，只能用于你自己的账号，不要发给别人。
- 不要在 X 已提示临时限制登录时继续反复登录，等限制解除后再从普通浏览器复制。
""".strip()


def backup_old_cookie(output_path):
    if not output_path.exists():
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = output_path.with_name(f"{output_path.stem}_{timestamp}.bak")
    shutil.copy2(output_path, backup_path)
    print(f"已备份旧 cookie: {backup_path}")


def normalize_same_site(value):
    if not value:
        return None

    value = str(value).strip().lower()
    mapping = {
        "lax": "Lax",
        "strict": "Strict",
        "none": "None",
        "no_restriction": "None",
        "unspecified": None,
    }
    return mapping.get(value)


def normalize_expiry(cookie):
    for key in ("expiry", "expirationDate", "expires"):
        value = cookie.get(key)
        if value in (None, "", 0, -1):
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return None


def normalize_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default

    value = str(value).strip().lower()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no"}:
        return False
    return default


def cookie_defaults(name, value):
    cookie = {
        "name": name,
        "value": value,
        "domain": X_DOMAIN,
        "path": "/",
        "secure": True,
    }
    if name == "auth_token":
        cookie["httpOnly"] = True
        cookie["sameSite"] = "None"
    elif name == "ct0":
        cookie["httpOnly"] = False
        cookie["sameSite"] = "Lax"
    return cookie


def normalize_cookie(raw_cookie):
    name = raw_cookie.get("name")
    value = raw_cookie.get("value")
    if not name or value is None:
        return None

    domain = raw_cookie.get("domain") or X_DOMAIN
    if domain.startswith("https://"):
        domain = domain.replace("https://", "", 1)
    if domain.startswith("http://"):
        domain = domain.replace("http://", "", 1)
    domain = domain.split("/", 1)[0]

    # 主程序访问的是 x.com，twitter.com 域 cookie 不能直接 add_cookie 到 x.com。
    if domain.endswith("twitter.com"):
        domain = X_DOMAIN
    if domain == "x.com":
        domain = X_DOMAIN

    cookie = {
        "name": str(name),
        "value": str(value),
        "domain": domain,
        "path": raw_cookie.get("path") or "/",
        "secure": normalize_bool(raw_cookie.get("secure"), True),
        "httpOnly": normalize_bool(raw_cookie.get("httpOnly", raw_cookie.get("http_only")), False),
    }

    expiry = normalize_expiry(raw_cookie)
    if expiry:
        cookie["expiry"] = expiry

    same_site = normalize_same_site(raw_cookie.get("sameSite"))
    if same_site:
        cookie["sameSite"] = same_site

    return cookie


def parse_cookie_header(header_text):
    header_text = header_text.strip()
    if header_text.lower().startswith("cookie:"):
        header_text = header_text.split(":", 1)[1].strip()

    cookies = []
    for item in header_text.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue

        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append(cookie_defaults(name, value))

    return cookies


def load_json_cookies(path):
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and "cookies" in data:
        data = data["cookies"]
    if not isinstance(data, list):
        raise ValueError("JSON 格式不正确：需要 cookie 列表，或包含 cookies 字段的对象")

    return data


def load_netscape_cookies(path):
    cookies = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            http_only = False
            if line.startswith("#HttpOnly_"):
                http_only = True
                line = line.removeprefix("#HttpOnly_")
            elif line.startswith("#"):
                continue

            parts = line.split("\t")
            if len(parts) != 7:
                continue

            domain, _, cookie_path, secure, expiry, name, value = parts
            cookies.append(
                {
                    "domain": domain,
                    "path": cookie_path or "/",
                    "secure": secure.upper() == "TRUE",
                    "httpOnly": http_only,
                    "expiry": expiry,
                    "name": name,
                    "value": value,
                }
            )
    return cookies


def load_cookie_file(path):
    suffix = path.suffix.lower()
    if suffix == ".json":
        return load_json_cookies(path)

    text = path.read_text(encoding="utf-8").strip()
    if "auth_token=" in text and "ct0=" in text and "\t" not in text:
        return parse_cookie_header(text)

    return load_netscape_cookies(path)


def print_cookie_guide():
    print(COOKIE_GUIDE)


def manual_cookies():
    print_cookie_guide()
    print()
    cookie_header = input("如果已复制整段 Cookie 请求头，请粘贴后回车；没有则直接回车，改为分别输入 auth_token/ct0：").strip()
    if cookie_header:
        return parse_cookie_header(cookie_header)

    auth_token = getpass("auth_token: ").strip()
    ct0 = getpass("ct0: ").strip()

    return [
        cookie_defaults("auth_token", auth_token),
        cookie_defaults("ct0", ct0),
    ]


def filter_x_cookies(cookies):
    normalized = []
    seen = set()

    for raw_cookie in cookies:
        cookie = normalize_cookie(raw_cookie)
        if not cookie:
            continue

        domain = cookie.get("domain", "")
        name = cookie["name"]
        is_x_domain = domain == X_DOMAIN or domain.endswith(".x.com")
        is_required = name in REQUIRED_COOKIES
        if not is_x_domain and not is_required:
            continue

        dedupe_key = (name, domain, cookie.get("path", "/"))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(cookie)

    return normalized


def validate_cookies(cookies):
    cookie_names = {cookie["name"] for cookie in cookies if cookie.get("value")}
    missing = REQUIRED_COOKIES - cookie_names
    if missing:
        raise ValueError(f"缺少必要 cookie: {', '.join(sorted(missing))}")


def save_cookies(cookies, output_path):
    backup_old_cookie(output_path)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    print(f"已保存 {len(cookies)} 个 cookie 到: {output_path}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="从普通浏览器导出的 cookie 生成 X_cookie.json；不再使用 Selenium 登录。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=COOKIE_GUIDE,
    )
    parser.add_argument(
        "source",
        nargs="?",
        help="浏览器插件导出的 JSON 文件、cookies.txt 文件，或包含 Cookie 请求头的文本文件。",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=str(COOKIE_PATH),
        help="输出文件路径，默认 X_cookie.json。",
    )
    parser.add_argument(
        "--guide",
        action="store_true",
        help="只显示 cookie 获取指南，不生成文件。",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.guide:
        print_cookie_guide()
        return

    output_path = Path(args.output)

    if args.source:
        source_path = Path(args.source)
        if not source_path.exists():
            raise FileNotFoundError(f"找不到 cookie 文件: {source_path}")
        raw_cookies = load_cookie_file(source_path)
    else:
        raw_cookies = manual_cookies()

    cookies = filter_x_cookies(raw_cookies)
    validate_cookies(cookies)
    save_cookies(cookies, output_path)


if __name__ == "__main__":
    main()
