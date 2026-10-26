#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
站点签到模式探测

用你的真实凭据实测一个站点需要哪种签到方式，并输出推荐配置 JSON。
适合站点很多时快速录入：跑一遍就知道每个站该配什么。

用法：
    python probe.py https://站点地址 <session_cookie或access_token> [user_id]

示例：
    python probe.py https://api.example.com MTc2NzQx... 12345
    python probe.py https://api.example.com sk-xxxx

判定逻辑：
    1. GET /api/user/self        认证是否有效
    2. GET /api/status           签到开关 / Turnstile / PoW 配置
    3. GET /api/user/checkin     今日是否已签到
    4. POST /api/user/checkin    真实签一次，按返回判定模式：
         success                  → plain（普通签到，脚本直接过）
         需要 turnstile            → turnstile（真浏览器自动解）
         需要 pow                  → pow（本地解算）
         需要 滑块/slide/拼图      → slider（ddddocr 拖拽）
         需要 签到码/口令/code     → code（配 checkin_code）
         CF 拦截(403/HTML)         → cf（Playwright 过 CF）
         session 过期              → 认证失败

⚠️ 注意：第 4 步是真实签到请求（今天还没签的话会直接帮你签掉），
   已签到则跳过 POST，只报告「plain（已签到，模式待确认）」。
"""

import sys
import json
import requests


def probe(base_url: str, credential: str, user_id: str = None) -> dict:
    base_url = base_url.rstrip('/')
    s = requests.Session()
    s.headers.update({
        'Accept': 'application/json, text/plain, */*',
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
    })

    is_token = credential.startswith('sk-') or len(credential) > 200
    if is_token:
        s.headers['Authorization'] = credential
        auth_kind = 'access_token'
    else:
        s.cookies.set('session', credential, domain=base_url.split('//')[-1].split('/')[0])
        auth_kind = 'session'

    report = {'url': base_url, 'auth_kind': auth_kind, 'steps': []}

    # 1. 认证
    print(f'[1/4] 验证凭据 ({auth_kind})...')
    try:
        r = s.get(f'{base_url}/api/user/self', timeout=30)
        if r.status_code == 401:
            report['mode'] = 'auth_failed'
            report['message'] = '认证失败：session 过期或 access_token 无效，请重新获取'
            return report
        data = r.json()
        if not data.get('success'):
            report['mode'] = 'auth_failed'
            report['message'] = f'认证失败: {data.get("message", "未知")}'
            return report
        user = data.get('data') or {}
        uid = user.get('id')
        if uid and not user_id:
            user_id = str(uid)
        s.headers['new-api-user'] = str(user_id) if user_id else str(uid)
        print(f'  ✅ 认证有效，用户: {user.get("username", "?")} (ID: {user.get("id")})')
        report['steps'].append(f'auth ok, user_id={user.get("id")}')
    except Exception as e:
        report['mode'] = 'error'
        report['message'] = f'请求 /api/user/self 异常: {e}'
        return report

    # 2. 站点状态
    print('[2/4] 读取站点配置 /api/status...')
    turnstile_key = None
    pow_mode = None
    checkin_enabled = None
    try:
        r = s.get(f'{base_url}/api/status', timeout=30)
        if r.status_code == 200:
            st = (r.json() or {}).get('data') or {}
            turnstile_key = st.get('turnstile_site_key')
            pow_mode = st.get('pow_mode')
            checkin_enabled = st.get('checkin_enabled', st.get('check_in_enabled'))
            print(f'  签到开关: {checkin_enabled}, Turnstile: {"有" if turnstile_key else "无"}, '
                  f'PoW: {pow_mode or "无"}')
            report['steps'].append(f'status: checkin_enabled={checkin_enabled}, '
                                   f'turnstile={"y" if turnstile_key else "n"}, pow={pow_mode}')
    except Exception as e:
        print(f'  （status 读取失败: {e}）')

    if checkin_enabled is False:
        report['mode'] = 'disabled'
        report['message'] = '站点关闭了签到功能'
        return report

    # 3. 今日是否已签到
    print('[3/4] 查询今日签到状态...')
    from datetime import datetime
    month = datetime.now().strftime('%Y-%m')
    try:
        r = s.get(f'{base_url}/api/user/checkin', params={'month': month}, timeout=30)
        if r.status_code == 200:
            hist = (r.json() or {}).get('data') or {}
            if hist.get('checked_in_today'):
                report['mode'] = 'plain'
                report['message'] = '今日已签到（模式未实测，按 plain 处理）'
                report['user_id'] = user_id
                report['steps'].append('already checked in today')
                print('  ✅ 今日已签到')
                return report
    except Exception:
        pass

    # 4. 真实签到探测
    print('[4/4] 发送真实签到请求探测模式（若今天未签，将直接完成签到）...')
    try:
        r = s.post(f'{base_url}/api/user/checkin', timeout=30)
        text = r.text or ''
        low = text.lower()
        try:
            data = r.json()
        except Exception:
            data = None

        if data is None:
            # 非 JSON → CF 拦截
            report['mode'] = 'cf'
            report['message'] = '响应非 JSON，疑似 Cloudflare 拦截（脚本会自动用 Playwright 过 CF）'
            report['steps'].append(f'POST -> non-json ({r.status_code})')
            return report

        msg = str(data.get('message', ''))
        if data.get('success'):
            report['mode'] = 'plain'
            report['message'] = f'普通签到即可: {msg}'
        elif 'turnstile' in low:
            report['mode'] = 'turnstile'
            report['message'] = '站点开启 Turnstile 人机验证（脚本用真实 Chrome 自动点击）'
        elif 'pow' in low:
            report['mode'] = 'pow'
            report['message'] = '站点开启 PoW 工作量证明（脚本本地解算）'
        elif any(k in msg for k in ('滑块', 'slide', '拼图', '拖动')) or 'slide' in low:
            report['mode'] = 'slider'
            report['message'] = '站点开启滑块验证（ddddocr 自动识别拖拽，需 pip install ddddocr）'
        elif any(k in msg for k in ('签到码', '口令')) or ('code' in low and 'captcha' not in low):
            report['mode'] = 'code'
            report['message'] = '站点需要签到码（配置 checkin_code 或 checkin_code_url）'
        elif r.status_code == 403 or 'just a moment' in low or 'cloudflare' in low:
            report['mode'] = 'cf'
            report['message'] = 'Cloudflare 拦截（脚本自动用 Playwright 过 CF）'
        else:
            report['mode'] = 'unknown'
            report['message'] = f'未能识别: HTTP {r.status_code} {msg[:100]}'
        report['steps'].append(f'POST -> {report["mode"]}: {msg[:80]}')
    except Exception as e:
        report['mode'] = 'error'
        report['message'] = f'签到请求异常: {e}'
        return report

    report['user_id'] = user_id
    return report


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    url = sys.argv[1]
    credential = sys.argv[2]
    user_id = sys.argv[3] if len(sys.argv) > 3 else None

    print('=' * 50)
    print(f'探测站点: {url}')
    print('=' * 50)
    report = probe(url, credential, user_id)

    print()
    print('=' * 50)
    print(f'判定模式: {report["mode"]}')
    print(f'说明: {report["message"]}')
    print('=' * 50)

    # 输出推荐配置
    if report['mode'] not in ('auth_failed', 'error', 'disabled'):
        is_token = report['auth_kind'] == 'access_token'
        cfg = {
            'url': report['url'],
            'name': url.split('//')[-1].split('/')[0],
        }
        if is_token:
            cfg['session'] = ''
            cfg['access_token'] = credential
        else:
            cfg['session'] = credential
        if report.get('user_id'):
            cfg['user_id'] = str(report['user_id'])
        print('\n推荐配置（粘贴到 NEWAPI_ACCOUNTS 的 JSON 数组里）：')
        print(json.dumps(cfg, ensure_ascii=False, indent=2))

        if report['mode'] == 'code':
            print('\n⚠️ 该站需要签到码，请再补充：')
            print('  "checkin_code": "当天签到码"     （固定值）')
            print('  或 "checkin_code_url": "https://签到码发布页"   （每天自动抓）')
        if report['mode'] == 'slider':
            print('\n⚠️ 该站带滑块验证，本地运行请先: pip install ddddocr pillow')


if __name__ == '__main__':
    main()
