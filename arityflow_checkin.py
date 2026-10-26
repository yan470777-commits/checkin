#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立签到子站签到（FastAPI 版签到服务，如 arityflow 的 qd.arityflow.top）

背景：
    部分 new-api 站点把签到拆成了独立子站，主站 /api/status 里会给出：
        {"checkin_url": "https://qd.example.top", ...}
    此时主站 POST /api/user/checkin 会返回 403 AUTH_INSUFFICIENT_PRIVILEGE。

子站接口（由其 /openapi.json 定义）：
    POST /api/auth/login   {username, password}   -> {token, user}
    GET  /api/auth/me                              -> 用户信息
    GET  /api/checkin/status                       -> checked_today / code_enabled / verify
    GET  /api/captcha                              -> {captcha_id, image_url, length}
    POST /api/checkin      {code, captcha_id, captcha_answer, cf_token}

验证码是 4 位图形码（图片托管在第三方，如 acg.yaohud.cn），用 ddddocr 本地识别，
识别或校验失败会自动换一张重试，不影响其它站点。
"""

import requests

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

_OCR = None


def _ocr():
    """懒加载 ddddocr（首次约 1-2 秒）"""
    global _OCR
    if _OCR is None:
        import ddddocr
        _OCR = ddddocr.DdddOcr(show_ad=False)
    return _OCR


def _headers(token: str = None) -> dict:
    h = {'Accept': 'application/json, text/plain, */*', 'User-Agent': UA}
    if token:
        h['Authorization'] = f'Bearer {token}'
    return h


def _detail(resp) -> str:
    """取出错误说明（FastAPI: detail；其它: message）"""
    try:
        body = resp.json()
    except ValueError:
        return f'HTTP {resp.status_code}'
    if isinstance(body, dict):
        detail = body.get('detail') or body.get('message')
        if isinstance(detail, list):  # 422 校验错误
            parts = [str(d.get('msg') or d) for d in detail if isinstance(d, dict)]
            return '; '.join(parts) or f'HTTP {resp.status_code}'
        if detail:
            return str(detail)
    return f'HTTP {resp.status_code}'


def sub_site_login(base_url: str, username: str, password: str, timeout: int = 30):
    """子站登录，返回 (token, user_dict)"""
    resp = requests.post(f'{base_url}/api/auth/login',
                         json={'username': username, 'password': password},
                         headers=_headers(), timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f'子站登录失败: {_detail(resp)}')
    body = resp.json() or {}
    token = body.get('token') or (body.get('data') or {}).get('token')
    if not token:
        raise RuntimeError(f'子站登录失败: 未返回 token（{_detail(resp)}）')
    return token, (body.get('user') or {})


def fetch_status(base_url: str, token: str, timeout: int = 30) -> dict:
    """查询子站签到状态（只读）"""
    resp = requests.get(f'{base_url}/api/checkin/status',
                        headers=_headers(token), timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f'状态查询失败: {_detail(resp)}')
    return resp.json() or {}



def _solve_captcha(base_url: str, token: str, timeout: int = 30):
    """取一张图形验证码并本地识别，返回 (captcha_id, answer)"""
    resp = requests.get(f'{base_url}/api/captcha', headers=_headers(token), timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f'验证码获取失败: {_detail(resp)}')
    cap = resp.json() or {}
    captcha_id, image_url = cap.get('captcha_id'), cap.get('image_url')
    if not captcha_id or not image_url:
        raise RuntimeError(f'验证码信息不完整: {cap}')
    img = requests.get(image_url, headers={'User-Agent': UA, 'Referer': base_url}, timeout=timeout)
    if img.status_code != 200:
        raise RuntimeError(f'验证码图片下载失败: HTTP {img.status_code}')
    answer = (_ocr().classification(img.content) or '').strip()
    return captcha_id, answer


def checkin_sub_site(base_url: str, username: str, password: str, code: str = None,
                     timeout: int = 30, max_attempts: int = 4) -> dict:
    """
    独立签到子站签到（已签到 / 验证码失败都会给出明确说明）

    Returns:
        与 checkin.py 一致的 result 字典：success / message / checkin_date / quota_awarded
        另附 already=True 表示本次本就无需签到
    """
    base_url = base_url.rstrip('/')
    result = {'success': False, 'message': '', 'checkin_date': None, 'quota_awarded': None}

    try:
        token, _user = sub_site_login(base_url, username, password, timeout)
    except Exception as e:
        result['message'] = str(e)
        return result

    try:
        status = fetch_status(base_url, token, timeout)
    except Exception as e:
        result['message'] = str(e)
        return result

    if status.get('checked_today'):
        result['success'] = True
        result['already'] = True
        result['message'] = f'今日已签到（连签 {status.get("streak", 0)} 天）'
        return result

    last_error = ''
    for attempt in range(1, max_attempts + 1):
        try:
            captcha_id, answer = _solve_captcha(base_url, token, timeout)
        except Exception as e:
            result['message'] = str(e)
            return result

        if not answer:
            last_error = '验证码识别结果为空'
            continue

        payload = {'code': code or '', 'captcha_id': captcha_id,
                   'captcha_answer': answer, 'cf_token': ''}
        try:
            resp = requests.post(f'{base_url}/api/checkin', json=payload,
                                 headers=_headers(token), timeout=timeout)
        except Exception as e:
            result['message'] = f'签到请求异常: {e}'
            return result

        if resp.status_code == 200:
            body = resp.json() or {}
            data = body.get('data') if isinstance(body.get('data'), dict) else body
            reward = (data.get('reward') or data.get('quota_awarded')
                      or data.get('amount') or data.get('silver'))
            if reward is not None:
                try:
                    result['quota_awarded'] = int(float(reward))
                except (TypeError, ValueError):
                    result['quota_awarded'] = None
            result['checkin_date'] = data.get('date') or data.get('checkin_date')
            msg = body.get('message') or data.get('message') or '签到成功'
            if data.get('already_checked_in'):
                result['already'] = True
            result['success'] = True
            result['message'] = f'{msg}（识别码 {answer}）'
            return result

        last_error = f'{_detail(resp)}（识别为 {answer}）'
        if resp.status_code == 422 or '验证码' in last_error or 'captcha' in last_error.lower():
            print(f'  [验证码] 第 {attempt} 次未通过，换一张重试')
            continue
        result['message'] = f'签到失败: {last_error}'
        return result

    result['message'] = f'签到失败（验证码重试 {max_attempts} 次）: {last_error}'
    return result


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 4:
        print('用法: python arityflow_checkin.py <子站地址> <用户名> <密码> [签到码]')
        sys.exit(2)
    print(checkin_sub_site(sys.argv[1], sys.argv[2], sys.argv[3],
                           sys.argv[4] if len(sys.argv) > 4 else None))
