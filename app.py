#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, time, random, requests
try:
    from patchright.sync_api import sync_playwright
    USING_PATCHRIGHT = True
except ImportError:
    from playwright.sync_api import sync_playwright
    USING_PATCHRIGHT = False

# --- 环境变量 ---
COOKIE_VALUE = os.environ.get('COOKIE_VALUE') or ""    # remember_web cookie 值，必填
EMAIL        = os.environ.get('EMAIL') or ""           # 登录邮箱
PASSWORD     = os.environ.get('PASSWORD') or ""        # 登录密码
TG_CHAT_ID   = os.environ.get('TG_CHAT_ID') or ""      # Telegram Chat ID
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN') or ""    # Telegram Bot Token

BASE_URL = "https://dash.hidencloud.com"
LOGIN_URL = f"{BASE_URL}/auth/login"

# --- 代理配置 ---
IS_PROXY         = os.environ.get('IS_PROXY', 'false').lower() == 'true'
PROXY_SERVER     = os.environ.get('PROXY_SERVER') or "http://127.0.0.1:1081"
REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None

CF_IFRAME_SELECTOR = 'iframe[src*="challenges.cloudflare.com"]'

if sys.platform == 'win32':
    DEFAULT_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
else:
    DEFAULT_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'

def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)

STEALTH_JS = """
try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
} catch (e) {}
"""

def get_current_ip(proxy_server=None):
    proxies = {"http": proxy_server, "https": proxy_server} if (proxy_server and IS_PROXY) else None
    try:
        resp = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
        if resp.status_code == 200:
            return resp.text.strip()
        return "获取失败"
    except Exception as e:
        log(f"❌ 获取出口IP失败: {e}")
        return "获取失败"

def send_telegram_notification(status, old_due="未知", new_due="未知", current_ip="未知"):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("⚠️ Telegram 未配置，跳过通知")
        return False
    
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        masked_email = f"{name[:2]}****{name[-2:]}@{domain}" if len(name) > 4 else f"{name}@{domain}"
    else:
        masked_email = (EMAIL[:2] + '****') if EMAIL else "未配置邮箱"

    text = (
        f"🎉 HidenCloud 续期通知\n\n"
        f"{status}\n"
        f"👤 账号: {masked_email}\n"
        f"📅 续期前到期：{old_due}\n"
        f"📅 续期后到期：{new_due}\n"
        f"🌐 续期使用IP: {current_ip}\n"
        f"🕒 续期时间：{now}"
    )
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10, proxies=REQUESTS_PROXIES)
        return resp.status_code == 200
    except Exception as e:
        log(f"❌ Telegram 通知异常: {e}")
        return False

def _remove_overlays(page):
    """移除阻挡点击的遮罩或隐私横幅"""
    try:
        page.evaluate(
            """
            () => {
                if (!document.getElementById('hide-ncmp-style')) {
                    const style = document.createElement('style');
                    style.id = 'hide-ncmp-style';
                    style.innerHTML = '#ncmp__tool, .ncmp__banner-inner, .ncmp__normalise { display: none !important; pointer-events: none !important; visibility: hidden !important; z-index: -9999 !important; }';
                    document.head.appendChild(style);
                }
                document.querySelectorAll('#ncmp__tool, .ncmp__banner-inner').forEach(el => el.remove());
            }
            """
        )
    except Exception:
        pass

def _get_turnstile_token(page):
    try:
        return page.evaluate(
            """
            () => {
                try {
                    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                        let t = window.turnstile.getResponse();
                        if (t && t.length > 30) return t;
                        for (let i = 0; i < 5; i++) {
                            try { t = window.turnstile.getResponse(String(i)); } catch (e) {}
                            if (t && t.length > 30) return t;
                        }
                    }
                } catch (e) {}
                const inputs = document.querySelectorAll(
                    'input[name="cf-turnstile-response"], input[id$="_response"]'
                );
                for (const el of inputs) {
                    if (el.value && el.value.length > 30) return el.value;
                }
                return null;
            }
            """
        )
    except Exception:
        return None

def _find_visible_cf_frames(page):
    result = []
    try:
        for handle in page.query_selector_all(CF_IFRAME_SELECTOR):
            box = handle.bounding_box()
            if box and box['width'] > 10 and box['height'] > 10:
                result.append(handle)
    except Exception:
        pass
    return result

def _cf_checkbox_visible(page):
    for frame in page.frames:
        if 'challenges.cloudflare.com' not in (frame.url or ''):
            continue
        try:
            if frame.locator('input[type="checkbox"]:visible').count() > 0:
                return True
        except Exception:
            continue
    return False

def _click_cf_checkbox(page, frame_el=None):
    _remove_overlays(page)
    for frame in page.frames:
        if 'challenges.cloudflare.com' not in (frame.url or ''):
            continue
        try:
            frame.locator('input[type="checkbox"]').first.click(timeout=2500)
            return True
        except Exception:
            continue
    if frame_el is None:
        return False
    try:
        box = frame_el.bounding_box()
        if not box:
            return False
        x = box['x'] + random.uniform(18, 34)
        y = box['y'] + box['height'] / 2 + random.uniform(-4, 4)
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.05, 0.2))
        page.mouse.down()
        time.sleep(random.uniform(0.04, 0.1))
        page.mouse.up()
        return True
    except Exception:
        return False

SECURITY_TITLE_HINTS = ('security verification', 'just a moment', 'attention required', 'checking your browser', '请稍候', 'please wait', 'one more step')

def _is_security_check_page(page):
    title = (page.title() or '').lower()
    if any(h in title for h in SECURITY_TITLE_HINTS):
        return True
    try:
        return page.evaluate(
            """
            () => {
                const t = document.body ? document.body.innerText.slice(0, 5000).toLowerCase() : '';
                return t.includes('verify you are human') || t.includes('checking your browser')
                    || t.includes('security verification');
            }
            """
        )
    except Exception:
        return True

def _has_interstitial_iframe(page):
    try:
        for handle in page.query_selector_all(CF_IFRAME_SELECTOR):
            if '/turnstile/' not in (handle.get_attribute('src') or ''):
                return True
    except Exception:
        pass
    return False

def handle_cloudflare(page, timeout=120):
    def challenge_active():
        return _has_interstitial_iframe(page) or _is_security_check_page(page)

    if not challenge_active():
        return True

    log("🔒 检测到 Cloudflare 安全验证...")
    start_time = time.time()
    last_click = 0
    while time.time() - start_time < timeout:
        if not challenge_active():
            return True
        if time.time() - last_click > 6:
            frames = _find_visible_cf_frames(page)
            if frames:
                log("🖱️ 点击 Cloudflare 验证框...")
                if _click_cf_checkbox(page, frames[-1]):
                    last_click = time.time()
                    time.sleep(3)
                    continue
        time.sleep(1)
    return False

def solve_modal_turnstile(page, timeout=60):
    log("🛡️ 开始处理 Turnstile 验证...")
    start = time.time()
    last_click = 0
    clicks = 0
    while time.time() - start < timeout:
        _remove_overlays(page)
        if _get_turnstile_token(page):
            log("✅ Turnstile 验证通过！")
            return True
        frames = _find_visible_cf_frames(page)
        if not frames:
            time.sleep(1)
            continue
        if time.time() - last_click > 5 and _cf_checkbox_visible(page):
            log(f"🖱️ 点击 Turnstile 复选框（第 {clicks + 1} 次）...")
            if _click_cf_checkbox(page, frames[-1]):
                clicks += 1
                last_click = time.time()
                time.sleep(3)
                continue
        time.sleep(1)
    return _get_turnstile_token(page) is not None

def open_browser(p):
    proxy_arg = {"server": PROXY_SERVER} if IS_PROXY else None
    if USING_PATCHRIGHT:
        browser = p.chromium.launch(
            channel="chrome",
            headless=False,
            args=['--disable-infobars'],
            proxy=proxy_arg,
        )
        ctx = browser.new_context(viewport=None, proxy=proxy_arg)
        page = ctx.new_page()
        return browser, page
    browser = p.chromium.launch(
        channel="chrome",
        headless=False,
        args=['--no-sandbox', '--disable-blink-features=AutomationControlled', '--disable-infobars']
    )
    ctx = browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        user_agent=DEFAULT_UA,
        proxy=proxy_arg,
    )
    page = ctx.new_page()
    page.add_init_script(STEALTH_JS)
    return browser, page

def _is_logged_in(page):
    url = page.url or ''
    if 'dash.hidencloud.com' not in url:
        return False
    if 'auth/login' in url or _is_security_check_page(page):
        return False
    return True

def login(page):
    if COOKIE_VALUE:
        log("📇 尝试 Cookie 登录...")
        try:
            page.context.add_cookies([{
                'name': 'remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d',
                'value': COOKIE_VALUE,
                'domain': 'dash.hidencloud.com',
                'path': '/',
                'expires': int(time.time()) + 3600 * 24 * 365,
                'httpOnly': True,
                'secure': True,
                'sameSite': 'Lax'
            }])
            page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page, timeout=60)
            _remove_overlays(page)
            if _is_logged_in(page):
                log("✅ Cookie 登录成功！")
                return True
        except Exception as e:
            log(f"⚠️ Cookie 登录异常: {e}")

    if not EMAIL or not PASSWORD:
        return False
    log("💣 尝试账号密码登录...")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page, timeout=120)
        time.sleep(5)
        _remove_overlays(page)

        user_input = page.locator('input[name="username"], input[name="email"], input[type="email"]').first
        pwd_input = page.locator('input[name="password"], input[type="password"]').first
        user_input.wait_for(state="visible", timeout=30000)
        user_input.fill(EMAIL)
        pwd_input.fill(PASSWORD)
        
        solve_modal_turnstile(page, timeout=60)
        _remove_overlays(page)
        try:
            page.click('button[type="submit"]', timeout=5000)
        except Exception:
            page.click('button[type="submit"]', force=True, timeout=5000)

        time.sleep(5)
        handle_cloudflare(page, timeout=120)
        page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
        return _is_logged_in(page)
    except Exception as e:
        log(f"❌ 登录异常: {e}")
        return False

def get_server_id(page):
    try:
        if 'dash.hidencloud.com' not in page.url:
            page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
        _remove_overlays(page)
        html = page.content()

        matches = re.findall(r'/service/(\d+)/manage', html)
        if matches:
            return matches[0]

        matches = [m for m in re.findall(r'#(\d{4,})', html) if set(m) != {'0'}]
        if matches:
            return matches[0]
        return None
    except Exception as e:
        log(f"❌ 获取 Server ID 失败: {e}")
        return None

def get_due_date(page):
    try:
        if SERVICE_URL not in page.url:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        _remove_overlays(page)
        body_text = page.locator("body").inner_text()
        patterns = [
            r"Due date\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Due date\s*\n\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            r"Due date.*?(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
        ]
        for pattern in patterns:
            match = re.search(pattern, body_text, re.IGNORECASE | re.DOTALL)
            if match:
                return match.group(1).strip()
    except Exception:
        pass
    return "未知"

def renew_service(page):
    try:
        log("➡ 进入续期流程...")
        if page.url != SERVICE_URL:
            page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        time.sleep(8)
        _remove_overlays(page)

        # 1. 点击 Renew 按钮唤出弹窗
        log("🖱️ 准备点击 Renew 按钮...")
        renew_btn = page.locator('button:has-text("Renew")').first
        create_btn = page.locator('button:has-text("Create Invoice")').first
        
        modal_opened = False
        for i in range(5):
            try:
                _remove_overlays(page)
                renew_btn.wait_for(state="visible", timeout=8000)
                renew_btn.scroll_into_view_if_needed()
                log(f"🖱️ 第 {i+1} 次点击 Renew...")
                try:
                    renew_btn.click(timeout=4000)
                except Exception:
                    renew_btn.click(force=True, timeout=4000)

                time.sleep(2)
                page_text = page.locator("body").inner_text().lower()
                if "renewal restricted" in page_text or "can only renew" in page_text:
                    log("⏳ 未到续期时间，无法续期。")
                    return "NOT_TIME"

                if create_btn.count() > 0 and create_btn.is_visible():
                    modal_opened = True
                    log("✅ 续费弹窗已成功弹出！")
                    break
            except Exception as e:
                log(f"⚠️ 点击 Renew 出错: {e}")
                time.sleep(2)

        if not modal_opened:
            log("❌ 错误：多次尝试后弹窗仍未出现。")
            return False

        # 2. 完成弹窗内的 Turnstile 验证
        handle_cloudflare(page, timeout=30)
        _remove_overlays(page)
        solve_modal_turnstile(page, timeout=60)

        # 3. 点击 Create Invoice 提交订单
        log("🖱️ 点击 Create Invoice...")
        try:
            _remove_overlays(page)
            try:
                create_btn.click(timeout=5000)
            except Exception:
                create_btn.click(force=True, timeout=5000)
            log("✅ Create Invoice 已点击，等待页面响应...")
        except Exception as e:
            log(f"⚠️ 点击 Create Invoice 失败: {e}")

        # 4. 智能检测跳转或提示（不再死等）
        new_invoice_url = None
        start_wait = time.time()
        while time.time() - start_wait < 25:
            current_url = page.url or ''
            # 匹配任意发票相关的 URL
            if any(k in current_url.lower() for k in ('/payment/invoice/', '/invoices/', '/invoice/')) and "/manage" not in current_url:
                new_invoice_url = current_url
                log(f"🎉 页面已跳转到发票页: {new_invoice_url}")
                break

            # 检查当前页是否直接出现了 Pay 按钮
            if page.locator('a:has-text("Pay"):visible, button:has-text("Pay"):visible').count() > 0:
                new_invoice_url = current_url
                log(f"🎉 页面已出现 Pay 按钮: {current_url}")
                break

            # 抓取页面弹出的提示信息（例如已有未付账单）
            try:
                alert_text = page.locator('.toast, [role="alert"], .alert, .notification').all_inner_texts()
                if alert_text:
                    combined = " ".join([t.strip() for t in alert_text if t.strip()])
                    if combined:
                        log(f"ℹ️ 页面提示: {combined}")
                        if "already" in combined.lower() or "unpaid" in combined.lower():
                            log("⚠️ 检测到已有未支付账单，跳过等待直接去支付...")
                            break
            except Exception:
                pass

            # 如果弹窗已关闭且按钮不可见，说明请求已提交，无需继续在此死等
            if create_btn.count() == 0 or not create_btn.is_visible():
                time.sleep(2)
                break

            time.sleep(1)

        # 5. 兜底方案：如果没跳转到具体发票，直接去账单列表 (/invoices) 找
        if not new_invoice_url or page.locator('a:has-text("Pay"):visible, button:has-text("Pay"):visible').count() == 0:
            log("🔎 前往发票中心 (/invoices) 寻找待支付账单...")
            invoices_url = f"{BASE_URL}/invoices"
            page.goto(invoices_url, wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            _remove_overlays(page)
            time.sleep(3)

            # 查找 Unpaid 状态的发票链接
            unpaid_link = page.locator('tr:has-text("Unpaid") a, tr:has-text("Pending") a, a:has-text("Unpaid")').first
            if unpaid_link.count() > 0:
                href = unpaid_link.get_attribute("href")
                if href:
                    if href.startswith("/"):
                        href = f"{BASE_URL}{href}"
                    log(f"✅ 找到未支付发票: {href}")
                    page.goto(href, wait_until="domcontentloaded", timeout=60000)
                    new_invoice_url = href

        # 6. 点击 Pay 按钮支付
        handle_cloudflare(page)
        _remove_overlays(page)
        pay_btn = page.locator('a:has-text("Pay"):visible, button:has-text("Pay"):visible').first
        if pay_btn.count() > 0:
            log("🔎 找到 Pay 按钮，正在执行支付...")
            try:
                pay_btn.click(timeout=5000)
            except Exception:
                pay_btn.click(force=True, timeout=5000)
            log("✅ Pay 按钮已点击！")
            time.sleep(6)
        else:
            log("ℹ️ 当前页面未找到 Pay 按钮，正在返回服务页验证是否已自动续期完成...")

        # 7. 回到服务管理页面确认最新到期时间
        page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        return True

    except Exception as e:
        log(f"❌ 续费异常: {e}")
        page.screenshot(path="renew_error.png")
        return False

def main():
    if not COOKIE_VALUE and not (EMAIL and PASSWORD):
        log("❌ 缺少登录凭证")
        sys.exit(1)

    global SERVICE_URL

    with sync_playwright() as p:
        browser = None
        try:
            current_ip = get_current_ip(PROXY_SERVER)
            log(f"🎯 当前出口IP: {current_ip}")
            log("🚀 启动浏览器...")
            browser, page = open_browser(p)

            if not login(page):
                sys.exit(1)

            server_id = get_server_id(page)
            if not server_id:
                log("❌ 无法获取 Server ID，退出。")
                sys.exit(1)
            SERVICE_URL = f"{BASE_URL}/service/{server_id}/manage"

            old_due = get_due_date(page)
            log(f"📆 续费前到期时间：{old_due}")

            renew_result = renew_service(page)

            new_due = old_due
            if renew_result == "NOT_TIME":
                status = "⏳ 未到续期时间"
            elif renew_result is False:
                status = "❌ 续期失败"
            else:
                new_due = get_due_date(page)
                log(f"📆 续费后到期时间：{new_due}")
                status = "✅ 续期成功"

            send_telegram_notification(status, old_due, new_due, current_ip)
            sys.exit(0 if renew_result is not False else 1)

        except Exception as e:
            log(f"❌ 执行出错: {e}")
            sys.exit(1)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass

if __name__ == "__main__":
    main()
