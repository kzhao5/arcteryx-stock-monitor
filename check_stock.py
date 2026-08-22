#!/usr/bin/env python3
"""Monitor a single Arc'teryx size/colour and shout when it comes back in stock.

Default target: Leutia Pant, colour 17166 (white), size 00S.

The Arc'teryx PDP is a JavaScript app and its internal stock API is not
documented, so this drives a real headless browser and reads the size
selector the same way a shopper would. Run with --discover once to dump the
XHR traffic if you later want to build a lighter API-only poller.

Exit codes:
    0  in stock
    1  out of stock
    2  could not determine (page changed, network error, blocked, ...)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

DEFAULT_URL = "https://arcteryx.com/us/en/shop/womens/leutia-pant-0962?colour=17166"
DEFAULT_SIZE = "00S"

# Text on the primary call-to-action that tells us what state we are in.
IN_STOCK_CTA = re.compile(r"add to (cart|bag)|添加到购物车|加入购物车", re.I)
OOS_CTA = re.compile(r"notify me|out of stock|sold out|waitlist|back in stock|缺货|售罄|到货通知", re.I)

# Attributes / classes that mark a size chip as unbuyable.
OOS_CLASS = re.compile(r"disabl|unavail|sold[-_ ]?out|out[-_ ]?of[-_ ]?stock|\boos\b|strike", re.I)

LENGTH_WORDS = {"S": "Short", "R": "Regular", "T": "Tall"}

CLICKABLE = "button, a, li, label, [role='radio'], [role='option'], [role='button']"


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


# --------------------------------------------------------------------------
# page interaction
# --------------------------------------------------------------------------

def dismiss_overlays(page) -> None:
    """Close cookie banners / region pickers that swallow clicks."""
    labels = [
        "Accept All", "Accept all", "Accept Cookies", "I Accept", "Got it",
        "Continue", "Shop United States", "Close", "接受", "同意",
    ]
    for label in labels:
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I))
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=2000)
                page.wait_for_timeout(400)
        except Exception:
            pass
    # Generic close buttons on modals.
    for sel in ["[aria-label='Close']", "[data-testid*='close']", ".modal button.close"]:
        try:
            el = page.locator(sel)
            if el.count() and el.first.is_visible():
                el.first.click(timeout=1500)
                page.wait_for_timeout(300)
        except Exception:
            pass


def find_option(page, label: str):
    """Return a locator for a clickable chip whose visible text is exactly `label`."""
    pattern = re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
    candidates = page.locator(CLICKABLE).filter(has_text=pattern)
    count = candidates.count()
    for i in range(count):
        el = candidates.nth(i)
        try:
            if not el.is_visible():
                continue
            # filter(has_text=...) matches ancestors too; keep the tightest node.
            if el.locator(CLICKABLE).count() == 0:
                return el
        except Exception:
            continue
    # Fall back to any visible match, tightest-first was not found.
    for i in range(count):
        el = candidates.nth(i)
        try:
            if el.is_visible():
                return el
        except Exception:
            continue
    return None


def option_is_disabled(el) -> bool:
    try:
        if el.is_disabled():
            return True
    except Exception:
        pass
    for attr in ("aria-disabled", "data-disabled", "disabled"):
        try:
            val = el.get_attribute(attr)
        except Exception:
            continue
        if val is not None and val.lower() not in ("false", "0"):
            return True
    for attr in ("class", "data-testid", "data-state", "aria-label", "title"):
        try:
            val = el.get_attribute(attr) or ""
        except Exception:
            continue
        if OOS_CLASS.search(val):
            return True
    return False


def read_cta(page) -> tuple[str | None, str]:
    """Look at the buy button. Returns (state, text) with state in/out/None."""
    texts: list[str] = []
    try:
        buttons = page.locator("button, a[role='button'], input[type='submit']")
        for i in range(min(buttons.count(), 60)):
            el = buttons.nth(i)
            try:
                if not el.is_visible():
                    continue
                txt = (el.inner_text() or el.get_attribute("value") or "").strip()
            except Exception:
                continue
            if txt:
                texts.append(txt)
    except Exception:
        pass
    blob = " | ".join(texts)
    # "Add to cart" wins: a page can carry a dormant "notify me" in the DOM.
    for txt in texts:
        if IN_STOCK_CTA.search(txt):
            return "in", txt
    for txt in texts:
        if OOS_CTA.search(txt):
            return "out", txt
    return None, blob[:400]


def select_size(page, size: str, length: str | None) -> tuple[object | None, str]:
    """Click the length (if any) and the size chip. Returns (size_locator, note)."""
    if length:
        el = find_option(page, length)
        if el is None:
            return None, f"length option {length!r} not found"
        el.click(timeout=8000)
        page.wait_for_timeout(1200)
        el = find_option(page, size)
        if el is None:
            return None, f"size {size!r} not found after choosing length {length!r}"
        return el, f"selected length={length}, size={size}"

    el = find_option(page, size)
    if el is not None:
        return el, f"selected size={size}"

    # "00S" may be rendered as size "00" plus a separate Short/Regular/Tall control.
    m = re.fullmatch(r"([0-9]{1,2}|X{0,3}[SML]|XXL|XL)\s*([SRT])", size, re.I)
    if m:
        base, suffix = m.group(1), m.group(2).upper()
        word = LENGTH_WORDS[suffix]
        len_el = find_option(page, word)
        if len_el is not None:
            len_el.click(timeout=8000)
            page.wait_for_timeout(1200)
        el = find_option(page, base)
        if el is not None:
            note = f"selected {word}+{base}" if len_el is not None else f"selected size={base} (no length control found)"
            return el, note
    return None, f"size {size!r} not found on page"


def detect_colour(page) -> str:
    for sel in ["[data-testid*='colour'], [data-testid*='color']", ".pdp-colour, .product-colour"]:
        try:
            el = page.locator(sel)
            if el.count():
                txt = (el.first.inner_text() or "").strip()
                if txt:
                    return txt.replace("\n", " ")[:80]
        except Exception:
            pass
    return ""


def check(page, url: str, size: str, length: str | None, shot: Path | None) -> tuple[str, str]:
    """Return (state, detail) with state in {'in', 'out', 'unknown'}."""
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    dismiss_overlays(page)
    page.wait_for_timeout(1000)

    colour = detect_colour(page)
    if colour:
        log(f"page colour reads: {colour}")

    el, note = select_size(page, size, length)
    if el is None:
        if shot:
            page.screenshot(path=str(shot), full_page=True)
        return "unknown", note

    if option_is_disabled(el):
        detail = f"{note}; size chip is marked unavailable"
        if shot:
            page.screenshot(path=str(shot), full_page=True)
        return "out", detail

    try:
        el.click(timeout=8000)
    except Exception as exc:  # an unclickable chip is itself a strong OOS signal
        return "out", f"{note}; chip not clickable ({exc.__class__.__name__})"
    page.wait_for_timeout(1500)

    if option_is_disabled(el):
        return "out", f"{note}; chip disabled after selection"

    state, cta = read_cta(page)
    if shot:
        page.screenshot(path=str(shot), full_page=True)
    if state == "in":
        return "in", f"{note}; CTA says {cta!r}"
    if state == "out":
        return "out", f"{note}; CTA says {cta!r}"
    return "unknown", f"{note}; no recognisable CTA. Buttons seen: {cta}"


# --------------------------------------------------------------------------
# discover mode
# --------------------------------------------------------------------------

def discover(page, url: str, size: str, outdir: Path) -> None:
    """Dump JSON XHR traffic so you can find the real stock endpoint."""
    outdir.mkdir(parents=True, exist_ok=True)
    hits: list[tuple[str, str]] = []
    seen = 0

    def on_response(resp):
        nonlocal seen
        ctype = (resp.headers or {}).get("content-type", "")
        if "json" not in ctype.lower():
            return
        try:
            body = resp.text()
        except Exception:
            return
        seen += 1
        name = re.sub(r"[^A-Za-z0-9]+", "_", urllib.parse.urlparse(resp.url).path)[:80]
        path = outdir / f"{seen:03d}{name or '_root'}.json"
        try:
            path.write_text(body, encoding="utf-8")
        except Exception:
            return
        if size.lower() in body.lower():
            hits.append((resp.url, path.name))

    page.on("response", on_response)
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=25000)
    except Exception:
        pass
    dismiss_overlays(page)
    page.wait_for_timeout(2500)

    html = page.content()
    (outdir / "page.html").write_text(html, encoding="utf-8")

    log(f"saved {seen} JSON responses + page.html to {outdir}")
    if hits:
        log(f"responses mentioning {size!r}:")
        for u, f in hits:
            log(f"  {f}  <-  {u}")
    else:
        log(f"no JSON response mentioned {size!r}; check page.html for inline data")


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------

def _post(url: str, data: bytes | None = None, headers: dict | None = None) -> None:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def notify(title: str, body: str, url: str) -> None:
    """Fire every notifier that has credentials in the environment."""
    sent = []

    bark = os.environ.get("BARK_URL")
    if bark:
        try:
            target = (
                f"{bark.rstrip('/')}/{urllib.parse.quote(title)}/{urllib.parse.quote(body)}"
                f"?url={urllib.parse.quote(url, safe='')}&group=arcteryx"
            )
            _post(target)
            sent.append("bark")
        except Exception as exc:
            log(f"bark failed: {exc}")

    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            payload = json.dumps({"chat_id": chat, "text": f"{title}\n{body}\n{url}"}).encode()
            _post(f"https://api.telegram.org/bot{token}/sendMessage", payload,
                  {"Content-Type": "application/json"})
            sent.append("telegram")
        except Exception as exc:
            log(f"telegram failed: {exc}")

    sc = os.environ.get("SERVERCHAN_KEY")
    if sc:
        try:
            payload = urllib.parse.urlencode({"title": title, "desp": f"{body}\n\n{url}"}).encode()
            _post(f"https://sctapi.ftqq.com/{sc}.send", payload,
                  {"Content-Type": "application/x-www-form-urlencoded"})
            sent.append("serverchan")
        except Exception as exc:
            log(f"serverchan failed: {exc}")

    hook = os.environ.get("WEBHOOK_URL")
    if hook:
        try:
            payload = json.dumps({"title": title, "text": body, "url": url}).encode()
            _post(hook, payload, {"Content-Type": "application/json"})
            sent.append("webhook")
        except Exception as exc:
            log(f"webhook failed: {exc}")

    host, to = os.environ.get("SMTP_HOST"), os.environ.get("SMTP_TO")
    if host and to:
        try:
            msg = EmailMessage()
            msg["Subject"] = title
            msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER", to)
            msg["To"] = to
            msg.set_content(f"{body}\n\n{url}\n")
            port = int(os.environ.get("SMTP_PORT", "465"))
            if port == 465:
                srv = smtplib.SMTP_SSL(host, port, timeout=25)
            else:
                srv = smtplib.SMTP(host, port, timeout=25)
                srv.starttls()
            with srv:
                user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
                if user and pw:
                    srv.login(user, pw)
                srv.send_message(msg)
            sent.append("email")
        except Exception as exc:
            log(f"email failed: {exc}")

    log(f"notified via: {', '.join(sent) if sent else 'nothing configured (set BARK_URL / TELEGRAM_* / SERVERCHAN_KEY / WEBHOOK_URL / SMTP_*)'}")


# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Watch one Arc'teryx size/colour for restock.")
    p.add_argument("--url", default=DEFAULT_URL, help="PDP url including ?colour=")
    p.add_argument("--size", default=DEFAULT_SIZE, help="size label, e.g. 00S")
    p.add_argument("--length", default=None, help="length label if it is a separate control (Short/Regular/Tall)")
    p.add_argument("--state-file", default="stock_state.json", help="remembers last state so you are told once")
    p.add_argument("--notify-always", action="store_true", help="notify on every in-stock check, not just the transition")
    p.add_argument("--interval", type=int, default=0, help="seconds between checks; 0 = run once and exit")
    p.add_argument("--jitter", type=int, default=60, help="random-ish spread added to --interval, seconds")
    p.add_argument("--retries", type=int, default=2, help="retries per check on hard failure")
    p.add_argument("--screenshot", default=None, help="write a screenshot of each check here")
    p.add_argument("--headful", action="store_true", help="show the browser (debugging)")
    p.add_argument("--executable", default=os.environ.get("CHROMIUM_PATH"),
                   help="path to a chromium binary, if the bundled one is missing")
    p.add_argument("--discover", metavar="DIR", default=None, help="dump XHR JSON to DIR and exit")
    args = p.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is missing. Run:\n"
              "  pip install -r requirements.txt && playwright install chromium", file=sys.stderr)
        return 2

    shot = Path(args.screenshot) if args.screenshot else None
    if shot:
        shot.parent.mkdir(parents=True, exist_ok=True)
    state_path = Path(args.state_file)

    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

    with sync_playwright() as pw:
        launch_kw = {"headless": not args.headful}
        if args.executable:
            launch_kw["executable_path"] = args.executable
        browser = pw.chromium.launch(**launch_kw)
        ctx = browser.new_context(user_agent=ua, locale="en-US",
                                  viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()

        if args.discover:
            try:
                discover(page, args.url, args.size, Path(args.discover))
                return 0
            finally:
                browser.close()

        last_exit = 2
        while True:
            state, detail = "unknown", "no attempt"
            for attempt in range(args.retries + 1):
                try:
                    state, detail = check(page, args.url, args.size, args.length, shot)
                    if state != "unknown":
                        break
                except Exception as exc:
                    state, detail = "unknown", f"{exc.__class__.__name__}: {exc}"
                if attempt < args.retries:
                    time.sleep(5 * (attempt + 1))

            log(f"{args.size} -> {state.upper()}  ({detail})")
            last_exit = {"in": 0, "out": 1}.get(state, 2)

            previous = None
            if state_path.exists():
                try:
                    previous = json.loads(state_path.read_text()).get("state")
                except Exception:
                    previous = None

            if state == "in" and (args.notify_always or previous != "in"):
                notify(f"Arc'teryx {args.size} 有货了", detail, args.url)

            if state in ("in", "out"):
                try:
                    state_path.write_text(json.dumps(
                        {"state": state, "detail": detail, "size": args.size,
                         "url": args.url, "checked_at": datetime.now(timezone.utc).isoformat()},
                        ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception as exc:
                    log(f"could not write state file: {exc}")

            if args.interval <= 0:
                browser.close()
                return last_exit

            wait = args.interval + (int(time.time() * 1000) % max(args.jitter, 1))
            log(f"sleeping {wait}s")
            time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
