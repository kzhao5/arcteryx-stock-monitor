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
OOS_CLASS = re.compile(
    r"no-{1,2}stock|disabl|unavail|sold[-_ ]?out|out[-_ ]?of[-_ ]?stock|\boos\b|strike", re.I)

LENGTH_WORDS = {"S": "Short", "R": "Regular", "T": "Tall"}

CLICKABLE = "button, a, li, label, [role='radio'], [role='option'], [role='button']"


def redact(addr: str) -> str:
    """k***@gmail.com -- enough to tell addresses apart, not enough to harvest."""
    local, _, domain = addr.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


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


def describe(el) -> str:
    """Compact description of a matched node, for the log."""
    try:
        info = el.evaluate("""e => ({
            tag: e.tagName.toLowerCase(),
            cls: (e.getAttribute('class') || '').slice(0, 120),
            dis: e.hasAttribute('disabled') || e.getAttribute('aria-disabled'),
            parent: e.parentElement ? e.parentElement.tagName.toLowerCase() : null,
            html: e.outerHTML.slice(0, 240),
        })""")
        return (f"<{info['tag']} parent={info['parent']} disabled={info['dis']} "
                f"class={info['cls']!r}> {info['html']!r}")
    except Exception as exc:
        return f"(could not describe: {exc.__class__.__name__})"


def dump_size_candidates(page, size: str) -> None:
    """Log every node whose text looks like a size, with its state."""
    try:
        rows = page.evaluate("""() => {
            const out = [];
            document.querySelectorAll("button, a, li, label, option, [role='radio'], [role='option']")
              .forEach(e => {
                const t = (e.textContent || '').replace(/\s+/g, '');
                if (!t || t.length > 6) return;
                if (!/^[0-9]{1,2}[SRT]?$|^X{0,3}[SML][SRT]?$/i.test(t)) return;
                const cs = getComputedStyle(e);
                const inp = e.querySelector('input') ||
                            (e.tagName === 'INPUT' ? e : null);
                out.push({t, tag: e.tagName.toLowerCase(),
                          dis: e.hasAttribute('disabled') || e.getAttribute('aria-disabled') || false,
                          inp: inp ? inp.disabled : null,
                          pe: cs.pointerEvents,
                          deco: cs.textDecorationLine,
                          cls: (e.getAttribute('class') || '').slice(0, 60)});
              });
            return out.slice(0, 40);
        }""")
        if rows:
            log(f"size-like nodes on page ({len(rows)}):")
            for r in rows:
                mark = " <== target" if r["t"].lower() == size.lower() else ""
                log(f"    {r['t']:>5}  <{r['tag']}> disabled={r['dis']} input={r['inp']} "
                    f"pointer={r['pe']} deco={r['deco']} class={r['cls']!r}{mark}")
        else:
            log("no size-like nodes found at all -- the selector may render late")
    except Exception as exc:
        log(f"could not enumerate sizes: {exc.__class__.__name__}: {exc}")


def option_is_disabled(el) -> bool:
    try:
        if el.is_disabled():
            return True
    except Exception:
        pass
    try:
        blocked = el.evaluate("""e => {
            const cs = getComputedStyle(e);
            if (cs.pointerEvents === 'none') return true;
            if ((cs.textDecorationLine || '').includes('line-through')) return true;
            const inp = e.querySelector('input') || (e.tagName === 'INPUT' ? e : null);
            if (inp && inp.disabled) return true;
            const lbl = e.closest('label');
            if (lbl) {
                const own = lbl.control || lbl.querySelector('input');
                if (own && own.disabled) return true;
            }
            return false;
        }""")
        if blocked:
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


def click_option(page, el, label: str) -> bool:
    """Select a size chip. Returns False if every strategy failed."""
    # A native <option> can be located by text but never clicked -- it has to
    # go through its owning <select>.
    try:
        tag = el.evaluate("e => e.tagName.toLowerCase()")
    except Exception:
        tag = ""
    if tag == "option":
        try:
            sel = el.locator("xpath=ancestor::select[1]")
            sel.select_option(label=label, timeout=8000)
            log("selected via native <select>")
            return True
        except Exception as exc:
            log(f"native select failed: {exc.__class__.__name__}")
            return False

    for strategy in ("plain", "scrolled", "forced"):
        try:
            if strategy == "scrolled":
                el.scroll_into_view_if_needed(timeout=4000)
                page.wait_for_timeout(300)
            el.click(timeout=6000, force=(strategy == "forced"))
            if strategy != "plain":
                log(f"selected via {strategy} click")
            return True
        except Exception as exc:
            log(f"{strategy} click failed: {exc.__class__.__name__}")
    return False


def is_selected(el) -> bool:
    try:
        return bool(el.evaluate("""e => {
            if (e.getAttribute('aria-checked') === 'true') return true;
            if (e.getAttribute('aria-pressed') === 'true') return true;
            if (e.getAttribute('aria-selected') === 'true') return true;
            if (/select|active|checked|current/i.test(e.getAttribute('class') || '')) return true;
            const inp = e.querySelector('input') || (e.tagName === 'INPUT' ? e : null);
            if (inp && inp.checked) return true;
            const lbl = e.closest('label');
            if (lbl) {
                const own = lbl.control || lbl.querySelector('input');
                if (own && own.checked) return true;
            }
            return false;
        }"""))
    except Exception:
        return False


def read_cta(page) -> tuple[str | None, str]:
    """Look at the buy button. Returns (state, text) with state in/out/None."""
    # "Add to cart" is checked first: the page can carry a dormant "notify me"
    # elsewhere in the DOM.
    for state, pattern in (("in", IN_STOCK_CTA), ("out", OOS_CTA)):
        try:
            btn = page.get_by_role("button", name=pattern)
            for i in range(min(btn.count(), 8)):
                el = btn.nth(i)
                try:
                    if el.is_visible():
                        return state, (el.inner_text() or "").strip()[:80]
                except Exception:
                    continue
        except Exception:
            pass

    # Nothing matched: report what was on screen, skipping the site chrome so
    # the log shows the product area rather than the nav bar.
    seen: list[str] = []
    try:
        buttons = page.locator("main button, [role='main'] button, form button")
        for i in range(min(buttons.count(), 40)):
            try:
                el = buttons.nth(i)
                if not el.is_visible():
                    continue
                txt = (el.inner_text() or "").strip()
            except Exception:
                continue
            if txt and txt not in seen:
                seen.append(txt)
    except Exception:
        pass
    return None, " | ".join(seen)[:400]


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
        dump_size_candidates(page, size)
        if shot:
            page.screenshot(path=str(shot), full_page=True)
        return "unknown", note

    if option_is_disabled(el):
        detail = f"{note}; size chip is marked unavailable (crossed out)"
        if shot:
            page.screenshot(path=str(shot), full_page=True)
        return "out", detail

    log(f"matched node: {describe(el)}")

    # A sold-out size is drawn crossed out and cannot be selected. That chip
    # state is the real signal; the buy button is NOT, because this page keeps
    # "Add to cart" enabled even with no size chosen.
    if not click_option(page, el, size):
        dump_size_candidates(page, size)
        if shot:
            page.screenshot(path=str(shot), full_page=True)
        return "out", f"{note}; chip cannot be selected (crossed out)"
    page.wait_for_timeout(1500)

    if option_is_disabled(el):
        return "out", f"{note}; chip disabled after selection"

    if not is_selected(el):
        # The click landed but nothing got selected, so any CTA reading below
        # would be about the page's default state, not about this size.
        dump_size_candidates(page, size)
        if shot:
            page.screenshot(path=str(shot), full_page=True)
        return "unknown", f"{note}; click did not select the size"

    state, cta = read_cta(page)
    if shot:
        page.screenshot(path=str(shot), full_page=True)
    if state == "in":
        return "in", f"{note}; selected, CTA says {cta!r}"
    if state == "out":
        return "out", f"{note}; selected, CTA says {cta!r}"
    dump_size_candidates(page, size)
    return "unknown", f"{note}; selected but no recognisable CTA. Buttons seen: {cta}"


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


def notify(title: str, body: str, url: str) -> list[str]:
    """Fire every notifier that has credentials. Returns the ones that worked."""
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
        # SMTP_TO may list several addresses, separated by comma or semicolon.
        recipients = [a.strip() for a in re.split(r"[,;]", to) if a.strip()]
        if not recipients:
            log("SMTP_TO is set but contains no usable address")
        else:
            try:
                msg = EmailMessage()
                msg["Subject"] = title
                msg["From"] = (os.environ.get("SMTP_FROM")
                               or os.environ.get("SMTP_USER") or recipients[0])
                msg["To"] = ", ".join(recipients)
                msg.set_content(f"{body}\n\n{url}\n")
                port = int(os.environ.get("SMTP_PORT", "465"))
                user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
                if port == 465:
                    srv = smtplib.SMTP_SSL(host, port, timeout=25)
                else:
                    srv = smtplib.SMTP(host, port, timeout=25)
                    srv.ehlo()
                    if srv.has_extn("starttls"):
                        srv.starttls()
                        srv.ehlo()
                    elif user and pw:
                        # Never hand the password to an unencrypted connection.
                        srv.quit()
                        raise RuntimeError(
                            f"{host}:{port} offers no STARTTLS; refusing to send credentials "
                            "in the clear. Use port 465, or 587 on a server that supports TLS.")
                with srv:
                    if user and pw:
                        srv.login(user, pw)
                    # send_message would derive recipients from the header; pass
                    # them explicitly so a malformed header cannot drop one.
                    refused = srv.send_message(msg, to_addrs=recipients)
                if refused:
                    log(f"email refused for: {', '.join(redact(a) for a in refused)}")
                delivered = [a for a in recipients if a not in (refused or {})]
                if delivered:
                    sent.append(f"email->{len(delivered)}")
                    log(f"emailed {len(delivered)} recipient(s): "
                        f"{', '.join(redact(a) for a in delivered)}")
            except Exception as exc:
                log(f"email failed: {exc.__class__.__name__}: {exc}")

    log(f"notified via: {', '.join(sent) if sent else 'nothing configured (set BARK_URL / TELEGRAM_* / SERVERCHAN_KEY / WEBHOOK_URL / SMTP_*)'}")
    return sent


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
    p.add_argument("--test-notify", action="store_true",
                   help="send a test notification through every configured channel and exit")
    args = p.parse_args()

    if args.test_notify:
        sent = notify("[测试] Arc'teryx 监控通知测试",
                      f"这是一封测试邮件，用来确认通知渠道正常。"
                      f"监控目标：{args.size}。真正补货时你会收到一封标题不含[测试]的邮件。",
                      args.url)
        if not sent:
            log("TEST FAILED: no notification channel succeeded. "
                "Check the secrets and the errors above.")
            return 1
        log(f"TEST PASSED: delivered through {', '.join(sent)}")
        return 0

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
