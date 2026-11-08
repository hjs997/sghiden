#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, time, random, requests
try:
    from patchright.sync_api import sync_playwright
    USING_PATCHRIGHT = True
except ImportError:
    from playwright.sync_api import sync_playwright
    USING_PATCHRIGHT = False

# --- 环境变量 (可在Settings里设置secrets或者私库直接填写在双引号里)---
COOKIE_VALUE = os.environ.get('COOKIE_VALUE') or ""    # remember_web cookie 值，必填
EMAIL        = os.environ.get('EMAIL') or ""           # 登录邮箱,可选，作为备用, 必填
PASSWORD     = os.environ.get('PASSWORD') or ""        # 登录密码,可选，作为备用, 必填
TG_CHAT_ID   = os.environ.get('TG_CHAT_ID') or ""      # Telegram Chat ID,可选，通知
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN') or ""    # Telegram Bot Token,可选    

BASE_URL = "https://dash.hidencloud.com"
LOGIN_URL = f"{BASE_URL}/auth/login"

# --- 代理配置（由工作流 shell 脚本写入 $GITHUB_ENV）---
IS_PROXY      = os.environ.get('IS_PROXY', 'false').lower() == 'true'
PROXY_SERVER  = os.environ.get('PROXY_SERVER') or "http://127.0.0.1:1081"
REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None

# Cloudflare 整页挑战 / Turnstile 组件共用的 iframe 选择器
CF_IFRAME_SELECTOR = 'iframe[src*="challenges.cloudflare.com"]'

# 按运行平台选择 UA
if sys.platform == 'win32':
    DEFAULT_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
else:
    DEFAULT_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'

# 日志
def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)

STEALTH_JS = """
try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
} catch (e) {}
"""

def _remove_overlays(page):
    """移除阻挡按钮点击的 Cookie 隐私横幅 (#ncmp__tool)"""
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

def get_current_ip(proxy_server=None):
    """获取当前出口IP"""
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
    """发送 Telegram 通知"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("⚠️ Telegram 未配置，跳过通知")
        return False
    
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
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
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10, proxies=REQUESTS_PROXIES)
        if resp.status_code == 200:
            log("✅ Telegram 通知发送成功")
            return True
        else:
            log(f"❌ Telegram 通知失败: {resp.text}")
            return False
    except Exception as e:
        log(f"❌ Telegram 通知异常: {e}")
        return False

def _get_turnstile_token(page):
    try:
        return page.evaluate(
            """
            () => {
                try {
                    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                        let t = null;
                        try { t = window.turnstile.getResponse(); } catch (e) {}
                        if (t && t.length > 30) return t;
                        for (let i = 0; i < 5; i++) {
                            try { t = window.turnstile.getResponse(String(i)); } catch (e) { t = null; }
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
            try:
                box = handle.bounding_box()
                if box and box['width'] > 10 and box['height'] > 10:
                    result.append(handle)
            except Exception:
                continue
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
        try:
            frame_el.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        box = frame_el.bounding_box()
        if not box:
            return False
        x = box['x'] + random.uniform(18, 34)
        y = box['y'] + box['height'] / 2 + random.uniform(-4, 4)
        page.mouse.move(x - random.uniform(30, 60), y + random.uniform(-8, 8))
        time.sleep(random.uniform(0.15, 0.4))
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
    try:
        title = (page.title() or '').lower()
        if any(h in title for h in SECURITY_TITLE_HINTS):
            return True
    except Exception:
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

def _click_security_submit(page):
    _remove_overlays(page)
    for text in ('Verify', 'Continue', 'Submit', 'Proceed', '验证'):
        try:
            btn = page.locator(f'button:has-text("{text}"):visible, input[type="submit"]:visible').first
            if btn.count() > 0:
                btn.click(timeout=3000)
                log(f"🖱️ 已点击安全验证页的确认按钮（{text}）...")
                return True
        except Exception:
            continue
    return False

def handle_cloudflare(page, timeout=240):
    def challenge_active():
        return _has_interstitial_iframe(page) or _is_security_check_page(page)

    if not challenge_active():
        for _ in range(3):
            time.sleep(1)
            if challenge_active():
                break
        else:
            return True

    log("🔒 检测到 Cloudflare 安全验证...")
    effective_timeout = timeout + (180 if timeout > 60 else 0)
    manual_hinted = False
    start_time = time.time()
    last_click = 0
    clear_rounds = 0
    while time.time() - start_time < effective_timeout:
        if not challenge_active():
            clear_rounds += 1
            if clear_rounds >= 2: 
                return True
            time.sleep(1)
            continue
        clear_rounds = 0
        if not manual_hinted and timeout > 60 and time.time() - start_time > timeout - 30:
            log("🤝 自动点击未能通过验证，脚本会继续等待...")
            manual_hinted = True
        if time.time() - last_click > 6:
            frames = _find_visible_cf_frames(page)
            if frames:
                log("🖱️ 点击 Turnstile 验证......")
                if _click_cf_checkbox(page, frames[-1]):
                    last_click = time.time()
                    time.sleep(random.uniform(3, 5))
                    continue
            elif _get_turnstile_token(page) and _is_security_check_page(page):
                if _click_security_submit(page):
                    last_click = time.time()
                    time.sleep(random.uniform(2, 4))
                    continue
        time.sleep(1)
    log("❌ 验证超时。")
    return False

def solve_modal_turnstile(page, timeout=90):
    log("🛡️ 开始处理 Turnstile 验证...")
    start = time.time()
    last_click = 0
    clicks = 0
    no_frame_seconds = 0
    while time.time() - start < timeout:
        _remove_overlays(page)
        if _get_turnstile_token(page):
            log("✅ Turnstile 验证通过！")
            return True
        frames = _find_visible_cf_frames(page)
        if not frames:
            no_frame_seconds += 1
            if no_frame_seconds >= 20 and clicks == 0:
                log("ℹ️ 未检测到 Turnstile 验证框，无需验证。")
                return True
            time.sleep(1)
            continue
        no_frame_seconds = 0
        if clicks > 0 and time.time() - last_click > 4 and not _cf_checkbox_visible(page):
            time.sleep(2)
            if not _cf_checkbox_visible(page):
                log("✅ Turnstile 复选框已消失，视为验证通过！")
                return True
        if time.time() - last_click > 6 and _cf_checkbox_visible(page):
            log(f"🖱️ 点击 Turnstile 复选框（第 {clicks + 1} 次）...")
            if _click_cf_checkbox(page, frames[-1]):
                clicks += 1
                last_click = time.time()
                time.sleep(random.uniform(3, 5))
                continue
        if clicks == 0 and time.time() - start > 20 and time.time() - last_click > 6:
            if _click_cf_checkbox(page, frames[-1]):
                clicks += 1
                last_click = time.time()
                time.sleep(random.uniform(3, 5))
                continue
        time.sleep(1)
    if _get_turnstile_token(page):
        log("✅ Turnstile 验证通过！")
        return True
    log("❌ Turnstile 验证超时。")
    return False

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
    try:
        url = page.url or ''
        if 'dash.hidencloud.com' not in url:
            return False
        if 'auth/login' in url or _is_security_check_page(page):
            return False
        return True
    except Exception:
        return False

def login(page):
    # 1. Cookie 登录尝试
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
            time.sleep(2)
            _remove_overlays(page)
            if _is_logged_in(page):
                log("✅ Cookie 登录成功！当前已到达dashboard页面")
                return True
            log("❌ Cookie 失效，请更换或使用账号密码")
        except Exception as e:
            log(f"⚠️ Cookie 登录尝试异常: {e}")

    # 2. 账号密码登录
    if not EMAIL or not PASSWORD:
        return False
    log("💣 尝试账号密码登录...")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page, timeout=120)
        time.sleep(5)
        _remove_overlays(page)
        log("⌨️ 输入账号密码...")
        user_input = page.locator('input[name="username"], input[name="email"], input[type="email"]').first
        pwd_input = page.locator('input[name="password"], input[type="password"]').first
        user_input.wait_for(state="visible", timeout=30000)
        user_input.fill(EMAIL, timeout=10000)
        pwd_input.fill(PASSWORD, timeout=10000)
        time.sleep(0.5)
        handle_cloudflare(page, timeout=60)
        _remove_overlays(page)
        
        if not solve_modal_turnstile(page, timeout=90):
            log("⚠️ 登录表单的 Turnstile 未确认通过，仍将尝试提交...")
        
        log("🖱️ 点击登录按钮提交...")
        time.sleep(2)
        _remove_overlays(page)
        try:
            page.click('button[type="submit"]', timeout=5000)
        except Exception:
            page.click('button[type="submit"]', force=True, timeout=5000)

        time.sleep(3)
        handle_cloudflare(page, timeout=120)
        page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page, timeout=60)
        _remove_overlays(page)
        if not _is_logged_in(page):
            log("❌ 登录失败。")
            send_telegram_notification("❌ 登录失败")
            return False
        log("✅ 账号密码登录成功！当前已到达dashboard页面")
        return True
    except Exception as e:
        log(f"❌ 登录异常: {e}")
        return False

def get_server_id(page):
    try:
        if 'dash.hidencloud.com' not in page.url:
            page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            time.sleep(2)
        handle_cloudflare(page)
        time.sleep(3)
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

        log("🖱️ 准备点击 Renew 按钮...")
        renew_btn = page.locator('button:has-text("Renew")')
        create_btn = page.locator('button:has-text("Create Invoice")')
        modal_opened = False
        for i in range(6):
            try:
                _remove_overlays(page)
                renew_btn.wait_for(state="visible", timeout=10000)
                renew_btn.scroll_into_view_if_needed()
                log(f"🖱️ 第 {i+1} 次尝试点击 Renew...")
                try:
                    renew_btn.click(timeout=5000)
                except Exception:
                    renew_btn.click(force=True, timeout=5000)

                time.sleep(3)
                page_text = page.locator("body").inner_text()
                if "Renewal Restricted" in page_text or "can only renew" in page_text.lower():
                    log("⚠️ 未到续期时间，无法续期。")
                    return "NOT_TIME" 

                log("🖲️ 等待弹窗出现...")
                try:
                    create_btn.wait_for(state="visible", timeout=8000)
                    modal_opened = True
                    log("✅ 弹窗已成功弹出！")
                    break
                except Exception:
                    time.sleep(2)
            except Exception as e:
                log(f"❌ 点击尝试出错: {e}")

        if not modal_opened:
            log("❌ 错误：尝试多次后，续费弹窗仍未出现。")
            return False

        # 弹窗内先完成 Turnstile 人机验证
        handle_cloudflare(page, timeout=60)
        _remove_overlays(page)
        solve_modal_turnstile(page, timeout=90)

        # 点击 Create Invoice
        new_invoice_url = None
        time.sleep(2)
        for attempt in range(4):
            # 关键保护：先检查是否已经被踢到登录页！
            if "auth/login" in (page.url or ""):
                log("🚨 检测到 Session 丢失被重定向至登录页，正在重新登录...")
                if not login(page):
                    return False
                log("🔄 重新登录成功，直接前往发票列表查找已生成的账单...")
                break

            log(f"🖱️ 点击 Create Invoice（第 {attempt + 1} 次）...")
            try:
                _remove_overlays(page)
                create_btn.wait_for(state="visible", timeout=8000)
                try:
                    create_btn.click(timeout=5000)
                except Exception:
                    create_btn.click(force=True, timeout=5000)
            except Exception as e:
                log(f"⚠️ 点击 Create Invoice 异常: {e}")

            # 等待跳转
            start_wait = time.time()
            session_lost = False
            while time.time() - start_wait < 15:
                curr_url = page.url or ""
                # 如果被踢到登录页，跳出等待进行重登
                if "auth/login" in curr_url:
                    log("🚨 提交后被重定向至登录页！")
                    session_lost = True
                    break
                
                # 识别发票页
                if any(k in curr_url.lower() for k in ("/payment/invoice/", "/invoices/", "/invoice/")) and "/manage" not in curr_url:
                    new_invoice_url = curr_url
                    log(f"🎉 页面已跳转: {new_invoice_url}")
                    break
                
                if page.locator('a:has-text("Pay"):visible, button:has-text("Pay"):visible').count() > 0:
                    new_invoice_url = curr_url
                    log(f"🎉 页面已检测到 Pay 按钮: {new_invoice_url}")
                    break
                time.sleep(1)

            if new_invoice_url:
                break

            if session_lost:
                log("🚨 正在恢复登录会话...")
                login(page)
                break

            # 如果弹窗已经不见了，说明后端已接收并处理，不要再原地点击了
            if create_btn.count() == 0 or not create_btn.is_visible():
                log("ℹ️ 弹窗已关闭，进入发票确认阶段...")
                break

            log(f"⚠️ 当前 URL [{page.url}] 未检测到发票跳转，重新校验 Turnstile...")
            solve_modal_turnstile(page, timeout=30)

        # 兜底查找发票并支付：如果没有直接停留在带 Pay 按钮的页面，前往 /invoices 查找未付账单
        if not new_invoice_url or page.locator('a:has-text("Pay"):visible, button:has-text("Pay"):visible').count() == 0:
            log("🔎 前往发票中心 (/invoices) 查找待支付发票...")
            page.goto(f"{BASE_URL}/invoices", wait_until="domcontentloaded", timeout=60000)
            handle_cloudflare(page)
            _remove_overlays(page)
            time.sleep(3)

            # 寻找 Unpaid 发票
            unpaid_row = page.locator('tr:has-text("Unpaid"), tr:has-text("Pending"), a:has-text("Unpaid")').first
            if unpaid_row.count() > 0:
                inv_link = unpaid_row.locator('a').first
                href = inv_link.get_attribute("href")
                if href:
                    if href.startswith("/"):
                        href = f"{BASE_URL}{href}"
                    log(f"✅ 找到未支付发票，前往支付: {href}")
                    page.goto(href, wait_until="domcontentloaded", timeout=60000)
                    new_invoice_url = href

        # 执行 Pay
        handle_cloudflare(page)
        _remove_overlays(page)
        pay_btn = page.locator('a:has-text("Pay"):visible, button:has-text("Pay"):visible').first
        if pay_btn.count() > 0:
            log("🔎 找到 Pay 按钮，点击付款...")
            try:
                pay_btn.click(timeout=5000)
            except Exception:
                pay_btn.click(force=True, timeout=5000)
            log("✅ Pay 按钮已点击！")
            time.sleep(6)
        else:
            log("ℹ️ 未找到待支付 Pay 按钮，检查是否已续期成功...")

        # 返回服务页核实到期时间
        page.goto(SERVICE_URL, wait_until="domcontentloaded", timeout=60000)
        handle_cloudflare(page)
        return True

    except Exception as e:
        log(f"❌ 续费异常: {e}")
        return False

def main():
    log(f"🔍 凭证检测: COOKIE_VALUE={'已配置' if COOKIE_VALUE else '未配置'}, "
        f"EMAIL={'已配置' if EMAIL else '未配置'}, PASSWORD={'已配置' if PASSWORD else '未配置'}")
    if not COOKIE_VALUE and not (EMAIL and PASSWORD):
        log("❌ 缺少登录凭证")
        sys.exit(1)

    global SERVICE_URL

    with sync_playwright() as p:
        browser = None
        try:
            if IS_PROXY:
                log(f"⚙️ 代理已启用: {PROXY_SERVER}")
            else:
                log("🌐 直连模式（未使用代理）")
            
            current_ip = get_current_ip(PROXY_SERVER)
            log(f"🎯 当前出口IP: {current_ip}")

            log("🚀 启动反检测内核浏览器...")
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
                log("⏳ 未到续期时间，目前无法续期")
                status = "⏳ 未到续期时间"
            elif renew_result is False:
                log("❌ 续费失败，脚本退出。")
                status = "❌ 续期失败"
            else:
                new_due = get_due_date(page)
                log(f"📆 续费后到期时间：{new_due}")
                status = "✅ 续期成功"

            send_telegram_notification(status, old_due, new_due, current_ip)
            sys.exit(0 if renew_result is not False else 1)

        except Exception as e:
            log(f"❌ 浏览器启动出错: {e}")
            sys.exit(1)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
                
if __name__ == "__main__":
    main()
