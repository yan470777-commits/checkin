#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
滑块验证码求解器（ddddocr 本地识别）

场景：部分中转站签到页/签到接口挂「滑块拼图」人机验证。
纯 HTTP 无法完成拖拽，本模块用真实 Chrome（CDP 连接，无自动化注入标志，
与 turnstile_solver 同一套启动逻辑）+ ddddocr 离线识别缺口坐标，拟人化拖拽通过。

支持两类常见实现：
- 自研 div 滑块（背景图 + 滑块小图 + 拖动按钮）
- 极验 GEETEST 滑块模式（.geetest_btn / .geetest_slice_bg / .geetest_slice）

流程：
1. 真实 Chrome 打开站点，等 CF 挑战通过（有 CF 防护的站）
2. 找到并点击「签到」按钮（滑块有时是点了签到才弹出）
3. 探测滑块元素 → 下载背景图/滑块图 → ddddocr slide_match 算缺口
4. 拟人化拖拽（加速→减速→轻微过冲回弹）
5. 验证通过后：优先点站点自己的签到按钮拿结果；同时调 API 拿结构化奖励数据

依赖：ddddocr（pip install ddddocr pillow）
未安装时所有函数优雅降级，返回明确错误信息。
"""

import time
import random
from typing import Optional

# ─── 依赖探测 ───────────────────────────────────────────────

DDDOCR_AVAILABLE = False
try:
    import ddddocr
    DDDOCR_AVAILABLE = True
except ImportError:
    pass


def is_available() -> bool:
    """ddddocr 是否可用"""
    return DDDOCR_AVAILABLE


# ─── 浏览器启动（复用 turnstile_solver） ─────────────────────

def _launch_chrome(proxy=None, url=None):
    """启动真实 Chrome，返回 (proc, dbg_port)"""
    from turnstile_solver import _launch_chrome as _ts_launch
    return _ts_launch(proxy, url)


def _get_system_proxy():
    from turnstile_solver import _get_system_proxy as _ts_proxy
    return _ts_proxy()


def _wait_cf_pass(page, base_url, timeout=60, verbose=True):
    from turnstile_solver import _wait_cf_pass as _ts_wait
    return _ts_wait(page, base_url, timeout=timeout, verbose=verbose)


# ─── 页面内探测：滑块元素 ───────────────────────────────────

# 收集页面上所有可能是滑块的图片（img + background-image）及拖动按钮
COLLECT_SLIDER_JS = '''() => {
    const out = { images: [], buttons: [], geetest: false, url: location.href };
    const push = (arr, o) => { if (o && o.src) arr.push(o); };

    // <img> 元素
    document.querySelectorAll('img').forEach(im => {
        const r = im.getBoundingClientRect();
        if (r.width < 20 || r.height < 20) return;
        push(out.images, {
            src: im.currentSrc || im.src,
            dispW: Math.round(r.width), dispH: Math.round(r.height),
            natW: im.naturalWidth, natH: im.naturalHeight,
            cls: (im.className || '').toString().slice(0, 80),
            id: (im.id || '').slice(0, 80)
        });
    });

    // background-image 元素（极验等把图放在 div 背景里）
    document.querySelectorAll('div,span,a,li').forEach(el => {
        const s = getComputedStyle(el).backgroundImage;
        if (!s || s === 'none' || !s.startsWith('url(')) return;
        const m = s.match(/url\\(["']?(.*?)["']?\\)/);
        if (!m) return;
        const r = el.getBoundingClientRect();
        if (r.width < 40 || r.height < 20) return;
        push(out.images, {
            src: m[1],
            dispW: Math.round(r.width), dispH: Math.round(r.height),
            natW: 0, natH: 0,
            cls: (el.className || '').toString().slice(0, 80),
            id: (el.id || '').slice(0, 80)
        });
    });

    // 拖动按钮候选
    const btnSel = '[class*="geetest_btn"], [class*="slide" i], [class*="slider" i], '
                 + '[class*="drag" i], [id*="slide" i], [id*="slider" i], [class*="verify" i][class*="btn"]';
    document.querySelectorAll(btnSel).forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width < 20 || r.height < 10 || r.width > 600) return;
        out.buttons.push({
            x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
            w: Math.round(r.width), h: Math.round(r.height),
            cls: (el.className || '').toString().slice(0, 80),
            id: (el.id || '').slice(0, 80),
            text: (el.innerText || '').trim().slice(0, 40)
        });
    });

    out.geetest = !!document.querySelector('[class*="geetest"]');
    return out;
}'''

# 从探测结果里挑出「背景图」和「滑块小图」：
# 背景图 = 显示面积最大的图；滑块小图 = 面积明显更小的图（优先带 slice/piece/block 类名）
def _pick_slider_images(images: list):
    if not images:
        return None, None

    scored = []
    for im in images:
        area = (im.get('dispW') or 0) * (im.get('dispH') or 0)
        cls = (im.get('cls') or '').lower()
        is_slice = any(k in cls for k in ('slice', 'piece', 'block', 'slide_block', 'slide-block', 'huji', 'fx'))
        is_bg = any(k in cls for k in ('bg', 'background', 'full', 'main', 'code_bg', 'cpt_bg'))
        scored.append((im, area, is_slice, is_bg))

    bg = None
    slice_im = None
    for im, area, is_slice, is_bg in scored:
        if is_bg and (bg is None or area > bg[1]):
            bg = (im, area)
    if bg is None:
        # 没有类名提示就取面积最大的当背景
        for im, area, is_slice, is_bg in scored:
            if bg is None or area > bg[1]:
                bg = (im, area)

    if bg is not None:
        bg_im, bg_area = bg
        for im, area, is_slice, is_bg in scored:
            if im is bg_im:
                continue
            if is_slice or (0 < area < bg_area * 0.5):
                if slice_im is None or area > slice_im[1]:
                    slice_im = (im, area)

    return (bg[0] if bg else None), (slice_im[0] if slice_im else None)


def _download_image(context, url: str, referer: str = None) -> Optional[bytes]:
    """用浏览器同源请求下载图片（带 cookie，过防盗链）"""
    try:
        headers = {}
        if referer:
            headers['Referer'] = referer
        resp = context.request.get(url, headers=headers, timeout=20000)
        if resp.status == 200:
            return resp.body()
    except Exception as e:
        print(f'[滑块] 图片下载失败: {e}')
    return None


# ─── ddddocr 缺口识别 ───────────────────────────────────────

_detector = None


def _get_detector():
    global _detector
    if _detector is None:
        import ddddocr
        _detector = ddddocr.DdddOcr(det=False, show_ad=False)
    return _detector


def match_gap(slice_bytes: bytes, bg_bytes: bytes) -> Optional[dict]:
    """
    ddddocr 滑块匹配

    注意：ddddocr >=1.5 的 slide_match 基于 OpenCV 边缘模板匹配，
    返回的 target[0] 是【匹配区域中心点 x】（背景图自然像素坐标系）。

    Returns:
        {'center_x': int, 'raw': dict}；失败返回 None
    """
    if not is_available():
        print('[滑块] ddddocr 未安装，请执行 pip install ddddocr pillow')
        return None
    try:
        det = _get_detector()
        res = det.slide_match(slice_bytes, bg_bytes)
        if isinstance(res, dict) and res.get('target'):
            return {'center_x': int(res['target'][0]), 'raw': res}
        # 兼容旧版返回 list 的情况
        if isinstance(res, (list, tuple)) and len(res) >= 1:
            return {'center_x': int(res[0]), 'raw': {'target': list(res)}}
    except Exception as e:
        print(f'[滑块] ddddocr 识别异常: {e}')
    return None


# ─── 拟人化拖拽 ─────────────────────────────────────────────

def human_drag(page, start_xy, distance: int, verbose: bool = True):
    """
    拟人化鼠标拖拽：
    按下 → 分段加速-减速移动（带垂直抖动）→ 轻微过冲 → 回弹 → 松开
    """
    import math
    x, y = start_xy
    page.mouse.move(x - random.uniform(2, 6), y + random.uniform(-2, 2))
    time.sleep(random.uniform(0.15, 0.4))
    page.mouse.down()
    time.sleep(random.uniform(0.05, 0.15))

    steps = max(24, min(60, int(distance / 8)))
    for i in range(1, steps + 1):
        p = i / steps
        # ease-out：前快后慢
        ease = 1 - (1 - p) ** 3
        nx = x + distance * ease
        ny = y + random.uniform(-1.2, 1.2)
        page.mouse.move(nx, ny)
        time.sleep(random.uniform(0.004, 0.018))

    # 过冲 + 回弹（人类常见特征）
    if distance > 30:
        page.mouse.move(x + distance + random.uniform(1.5, 4), y + random.uniform(-1, 1))
        time.sleep(random.uniform(0.08, 0.2))
        page.mouse.move(x + distance - random.uniform(0.5, 2.5), y)
        time.sleep(random.uniform(0.1, 0.25))

    page.mouse.up()
    if verbose:
        print(f'[滑块] 拖拽完成: 距离 {distance:.0f}px')


# ─── 页面状态判断 ───────────────────────────────────────────

FIND_CHECKIN_BTN_JS = '''() => {
    const els = [...document.querySelectorAll('button, a, div[role="button"], span')];
    for (const el of els) {
        const t = (el.innerText || '').trim();
        if (/^(立即)?签到|每日签到|check\\s*in/i.test(t) && t.length < 20) {
            const r = el.getBoundingClientRect();
            if (r.width > 10 && r.height > 10) {
                return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
                         text: t, disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true' };
            }
        }
    }
    return null;
}'''

PAGE_TEXT_JS = '() => document.body ? document.body.innerText.slice(0, 3000) : ""'

SUCCESS_HINTS = ['签到成功', '已签到', '签到完成', '领取成功', 'check-in successful']
SLIDER_ALIVE_JS = '''() => {
    // 滑块容器是否还可见（未通过验证时通常还在）
    const sels = ['[class*="geetest"][class*="panel"]', '[class*="slider" i]', '[class*="slide" i]'];
    for (const s of sels) {
        document.querySelectorAll(s).forEach(el => {
            const r = el.getBoundingClientRect();
            const st = getComputedStyle(el);
            if (r.width > 30 && r.height > 10 && st.visibility !== 'hidden' && st.display !== 'none') return true;
        });
    }
    return false;
}'''


def _result_from_text(text: str) -> Optional[dict]:
    """从页面文本判断签到结果"""
    low = text.lower()
    for hint in SUCCESS_HINTS:
        if hint.lower() in low:
            return {'success': True, 'message': hint}
    if '今日已签到' in text or '已经签到' in text or '重复签到' in text:
        return {'success': True, 'message': '今日已签到'}
    return None


# ─── 主流程 ─────────────────────────────────────────────────

POST_JS = '''async (args) => {
    const headers = Object.assign({'Content-Type': 'application/json'}, args.headers || {});
    const resp = await fetch('/api/user/checkin' + (args.query || ''), {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(args.body || {}),
        credentials: 'include',
    });
    const text = await resp.text();
    try { return JSON.parse(text); }
    catch (e) { return { success: false, message: '响应非JSON: ' + text.substring(0, 120) }; }
}'''


def solve_and_checkin(base_url: str, auth_headers: dict = None, proxy: str = None,
                      max_attempts: int = 3, verbose: bool = True,
                      checkin_path: str = None):
    """
    在真实浏览器中完成「滑块验证 + 签到」全流程

    Args:
        base_url: 站点地址
        auth_headers: 认证头（Authorization / New-Api-User）
        proxy: 代理；默认读系统代理
        max_attempts: 滑块最多尝试次数
        checkin_path: 签到页路径（默认依次尝试 /personal 和首页）

    Returns:
        结果 dict: {success, message, quota_awarded, checkin_date}
    """
    result = {'success': False, 'message': '', 'quota_awarded': None, 'checkin_date': None}

    if not is_available():
        result['message'] = '滑块求解需要 ddddocr：pip install ddddocr pillow'
        return result

    if proxy is None:
        proxy = _get_system_proxy()

    auth_headers = auth_headers or {}
    target = base_url.rstrip('/')
    if checkin_path:
        target = target + checkin_path

    proc, dbg_port = _launch_chrome(proxy, url=target)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = None
            last_err = None
            for _ in range(3):
                try:
                    browser = p.chromium.connect_over_cdp(f'http://127.0.0.1:{dbg_port}')
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(3)
            if browser is None:
                raise RuntimeError(f'CDP 连接失败: {last_err}')
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            if not _wait_cf_pass(page, base_url, verbose=verbose):
                result['message'] = 'CF 验证未通过（数据中心 IP 可能被限制）'
                return result

            time.sleep(2)

            # 找签到按钮（找不到也行，滑块可能直接摆在页面上）
            btn = None
            try:
                btn = page.evaluate(FIND_CHECKIN_BTN_JS)
            except Exception:
                pass

            # 若当前页没有签到按钮且未指定路径，去 /personal 看看
            if not btn and not checkin_path:
                try:
                    page.goto(base_url.rstrip('/') + '/personal',
                              wait_until='domcontentloaded', timeout=30000)
                    time.sleep(2)
                    btn = page.evaluate(FIND_CHECKIN_BTN_JS)
                except Exception:
                    pass

            # 有签到按钮且滑块未弹出 → 先点一下（很多站是点了签到才出滑块）
            slider_popped = False
            try:
                info = page.evaluate(COLLECT_SLIDER_JS)
                if info['images']:
                    slider_popped = True
            except Exception:
                pass

            if btn and not slider_popped and not btn.get('disabled'):
                if verbose:
                    print(f"[滑块] 点击签到按钮: {btn.get('text')}")
                try:
                    page.mouse.click(btn['x'], btn['y'])
                    time.sleep(2.5)
                except Exception:
                    pass

            # 滑块求解循环
            solved = False
            no_slider_seen = False
            for attempt in range(1, max_attempts + 1):
                try:
                    info = page.evaluate(COLLECT_SLIDER_JS)
                except Exception as e:
                    result['message'] = f'滑块探测失败: {e}'
                    return result

                bg, slice_im = _pick_slider_images(info['images'])
                if not bg:
                    # 没有滑块了 —— 要么已通过，要么本来就没有
                    if attempt == 1:
                        no_slider_seen = True
                    break

                if not info['buttons']:
                    result['message'] = '发现滑块图片但未找到拖动按钮'
                    return result
                btn_box = max(info['buttons'], key=lambda b: b.get('w', 0))

                if verbose:
                    print(f'[滑块] 第 {attempt} 次尝试: 背景图 {bg.get("dispW")}x{bg.get("dispH")}, '
                          f'滑块图 {slice_im.get("dispW") if slice_im else "?"}x'
                          f'{slice_im.get("dispH") if slice_im else "?"}, '
                          f'按钮 ({btn_box["x"]}, {btn_box["y"]})')

                # 下载图片
                bg_bytes = _download_image(ctx, bg['src'], referer=page.url)
                if not bg_bytes:
                    result['message'] = '背景图下载失败'
                    return result
                if not slice_im:
                    result['message'] = '未识别到滑块小图（只有背景图）'
                    return result
                slice_bytes = _download_image(ctx, slice_im['src'], referer=page.url)
                if not slice_bytes:
                    result['message'] = '滑块图下载失败'
                    return result

                # 识别缺口（ddddocr 返回匹配区域中心点 x，背景图自然像素）
                match = match_gap(slice_bytes, bg_bytes)
                if match is None:
                    result['message'] = 'ddddocr 未能识别缺口'
                    return result
                center_x = match['center_x']
                conf = (match.get('raw') or {}).get('confidence')

                # 背景缩放比例：显示宽 / 自然宽
                scale = 1.0
                nat_w = bg.get('natW') or 0
                disp_w = bg.get('dispW') or 0
                if nat_w and disp_w:
                    scale = disp_w / nat_w

                # 缺口左边缘（自然像素）= 中心x - 滑块图自然宽/2
                slice_nat_w = slice_im.get('dispW') or 40
                try:
                    from PIL import Image as _PILImage
                    import io as _io
                    slice_nat_w = _PILImage.open(_io.BytesIO(slice_bytes)).width or slice_nat_w
                except Exception:
                    pass
                gap_left_nat = center_x - slice_nat_w / 2.0

                # 换算到屏幕坐标：滑块初始在轨道最左，拖拽距离 ≈ 缺口左边缘的显示坐标
                gap_disp = gap_left_nat * scale
                distance = max(10.0, gap_disp)

                if verbose:
                    conf_str = f', 置信度 {conf:.3f}' if isinstance(conf, (int, float)) else ''
                    print(f'[滑块] 缺口识别: 中心x={center_x}{conf_str}, 缩放={scale:.3f}, '
                          f'屏幕距离≈{distance:.0f}px')

                human_drag(page, (btn_box['x'], btn_box['y']), distance, verbose=verbose)

                # 等验证结果
                time.sleep(2.5)
                try:
                    text = page.evaluate(PAGE_TEXT_JS)
                except Exception:
                    text = ''
                res = _result_from_text(text)
                if res:
                    solved = True
                    result['success'] = True
                    result['message'] = res['message'] + ' (滑块)'
                    break
                # 滑块还在 → 失败，刷新后重试
                try:
                    alive = page.evaluate(SLIDER_ALIVE_JS)
                except Exception:
                    alive = True
                if alive and attempt < max_attempts:
                    if verbose:
                        print(f'[滑块] 第 {attempt} 次未通过，重试...')
                    # 点刷新图标（极验）或重新点签到触发新滑块
                    try:
                        page.evaluate('''() => {
                            const r = document.querySelector('[class*="refresh" i]');
                            if (r) r.click();
                        }''')
                    except Exception:
                        pass
                    time.sleep(1.5)
                    continue
                if not alive:
                    solved = True
                    break

            if not solved and not no_slider_seen:
                result['message'] = f'滑块验证 {max_attempts} 次尝试均未通过'
                return result

            # ─── 提交签到 ───
            # 1) 点站点自己的签到按钮（让站点 JS 带 token 提交，最稳）
            api_result = None
            try:
                btn = page.evaluate(FIND_CHECKIN_BTN_JS)
                if btn and not btn.get('disabled'):
                    page.mouse.click(btn['x'], btn['y'])
                    time.sleep(2.5)
            except Exception:
                pass

            # 2) 同时调 API 拿结构化结果（已签到视为幂等成功）
            try:
                api_result = page.evaluate(POST_JS, {'headers': auth_headers})
            except Exception as e:
                if verbose:
                    print(f'[滑块] API 调用异常: {e}')

            # 3) 汇总结果
            text = ''
            try:
                text = page.evaluate(PAGE_TEXT_JS)
            except Exception:
                pass
            page_res = _result_from_text(text)

            if isinstance(api_result, dict) and api_result.get('success'):
                result['success'] = True
                result['message'] = api_result.get('message', '签到成功')
                data = api_result.get('data') or {}
                if isinstance(data, dict):
                    result['quota_awarded'] = data.get('quota_awarded')
                    result['checkin_date'] = data.get('checkin_date')
            elif page_res:
                result['success'] = True
                result['message'] = page_res['message'] + ' (滑块)'
            elif isinstance(api_result, dict):
                result['message'] = f'滑块后签到失败: {api_result.get("message", "未知")}'
            else:
                result['message'] = '滑块已通过，但未能确认签到结果（可查看运行日志）'
                # 页面有成功文案但 API 无响应时，保守算成功
                if page_res:
                    result['success'] = True

            browser.close()
            return result
    except Exception as e:
        result['message'] = f'滑块求解异常: {e}'
        if verbose:
            import traceback
            traceback.print_exc()
        return result
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == '__main__':
    # 调试：python slider_solver.py https://站点地址
    import sys
    import os
    if len(sys.argv) < 2:
        print('用法: python slider_solver.py <站点URL> [登录username] [登录password]')
        sys.exit(1)
    url = sys.argv[1]
    headers = {}
    if len(sys.argv) >= 4:
        # 账密登录拿 session 后跑
        import requests
        s = requests.Session()
        r = s.post(url.rstrip('/') + '/api/user/login',
                   json={'username': sys.argv[2], 'password': sys.argv[3]}, timeout=30)
        print('登录:', r.status_code, r.text[:120])
        sess = s.cookies.get('session')
        if sess:
            headers['Cookie'] = f'session={sess}'
    r = solve_and_checkin(url, auth_headers=headers)
    print('结果:', r)
