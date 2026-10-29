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

import time
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



def _verify_cfg(status: dict) -> dict:
    """新版子站把「人机验证」配置放在 status.verify 里"""
    v = status.get('verify')
    return v if isinstance(v, dict) else {}


def _sitekey_of(status: dict) -> str:
    """从 status 里取 Turnstile sitekey（未启用验证时返回空串）"""
    v = _verify_cfg(status)
    if not v.get('enabled'):
        return ''
    return str(v.get('cf_site_key') or v.get('sitekey') or '')


def _turnstile_token(base_url: str, sitekey: str, timeout: int = 120,
                     verbose: bool = True) -> str:
    """在子站自己的域名下渲染 Turnstile 组件拿 token（失败返回空串）

    必须同源：子站可能校验 token 的 hostname，本地 127.0.0.1 页面拿到的 token 会被拒。
    """
    if not sitekey:
        return ''
    try:
        from turnstile_solver import get_token_on_site
    except ImportError:
        print('  [Turnstile] 缺少依赖（playwright + Chrome），无法自动解人机验证')
        return ''
    print(f'  [Turnstile] 子站要求人机验证，启动真实浏览器求解 (sitekey {sitekey})...')
    token = get_token_on_site(base_url, sitekey, max_wait=timeout, verbose=verbose)
    print('  [Turnstile] token 获取成功' if token else '  [Turnstile] token 获取失败')
    return token or ''


def _solve_captcha_safe(base_url: str, token: str, timeout: int = 30):
    """取图形验证码并识别；站点没开验证码时返回 (None, None) 而不是中断签到"""
    try:
        return _solve_captcha(base_url, token, timeout)
    except Exception as e:
        print(f'  [验证码] 获取失败，按「该站无需验证码」继续: {e}')
        return None, None


def _solve_captcha(base_url: str, token: str, timeout: int = 30):
    """取一张图形验证码并本地识别，返回 (captcha_id, answer)

    新旧子站两种取图方式都支持：
    - 旧版: {"captcha_id": ..., "image_url": "https://acg.yaohud.cn/..."}
    - 新版: {"captcha_id": ..., "image_base64": "iVBOR..."}（无图片链接，图直接内嵌）
    """
    resp = requests.get(f'{base_url}/api/captcha', headers=_headers(token), timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f'验证码获取失败: {_detail(resp)}')
    cap = resp.json() or {}
    captcha_id = cap.get('captcha_id')
    image_url = cap.get('image_url')
    image_b64 = cap.get('image_base64') or cap.get('image')

    if not captcha_id:
        raise RuntimeError(f'验证码信息不完整: {cap}')
    if image_url:
        img = requests.get(image_url, headers={'User-Agent': UA, 'Referer': base_url}, timeout=timeout)
        if img.status_code != 200:
            raise RuntimeError(f'验证码图片下载失败: HTTP {img.status_code}')
        image_bytes = img.content
    elif image_b64:
        try:
            import base64 as _b64
            image_bytes = _b64.b64decode(image_b64.split(',', 1)[-1])
        except Exception as e:
            raise RuntimeError(f'验证码图片解码失败: {e}')
    else:
        raise RuntimeError(f'验证码信息不完整: {cap}')
    answer = (_ocr().classification(image_bytes) or '').strip()
    return captcha_id, answer


def checkin_sub_site(base_url: str, username: str, password: str, code: str = None,
                     timeout: int = 30, max_attempts: int = 4,
                     turnstile_wait: int = 120) -> dict:
    """
    独立签到子站签到（已签到 / 验证码 / 人机验证失败都会给出明确说明）

    2026-10 之后的新版子站，/api/checkin/status 里多了一段：
        "verify": {"enabled": true, "provider": "arityflow",
                   "cf_site_key": "0x4AAA..."}
    此时 /api/checkin 必须带上 Cloudflare Turnstile 的 cf_token，
    否则只返回「请完成人机验证」，光有图形验证码也签不上。

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

    sitekey = _sitekey_of(status)
    if sitekey:
        verify = _verify_cfg(status)
        print(f'  [子站] 新版签到需要人机验证 (provider={verify.get("provider") or "turnstile"})')

    def _recheck(why: str):
        """请求超时/异常后只读复查：服务端可能已经入账，别误报失败"""
        try:
            st = fetch_status(base_url, token, timeout)
        except Exception:
            return None
        if st.get('checked_today'):
            return {'success': True, 'already': True,
                    'message': f'{why}，复查确认今日已签到（连签 {st.get("streak", 0)} 天）'}
        return None

    cf_token = ''
    ts_solves = 0
    last_error = ''
    for attempt in range(1, max_attempts + 1):
        if sitekey and not cf_token:
            if ts_solves >= 3:
                result['message'] = (last_error + ' | 子站要求人机验证（Turnstile），'
                                     '自动求解连续失败：请确认本机装有 Chrome，'
                                     '或到网页手动签到').lstrip(' | ')
                return result
            ts_solves += 1
            cf_token = _turnstile_token(base_url, sitekey, turnstile_wait)
            if not cf_token:
                last_error = f'Turnstile 求解失败（第 {ts_solves} 次）'
                print(f'  [Turnstile] 第 {ts_solves} 次求解失败，稍后重试')
                time.sleep(3)
                continue

        captcha_id, answer = _solve_captcha_safe(base_url, token, timeout)
        if captcha_id and not answer:
            last_error = '验证码识别结果为空'
            print(f'  [验证码] 第 {attempt} 次识别为空，换一张重试')
            continue

        payload = {'code': code or '', 'captcha_id': captcha_id or '',
                   'captcha_answer': answer or '', 'cf_token': cf_token}
        try:
            resp = requests.post(f'{base_url}/api/checkin', json=payload,
                                 headers=_headers(token), timeout=timeout)
        except requests.exceptions.Timeout:
            done = _recheck('签到请求超时')
            if done:
                result.update(done)
                return result
            result['message'] = '签到请求超时，复查显示今日仍未签到'
            return result
        except Exception as e:
            done = _recheck('签到请求异常')
            if done:
                result.update(done)
                return result
            result['message'] = f'签到请求异常: {e}'
            return result

        if resp.status_code == 200:
            try:
                body = resp.json() or {}
            except ValueError:
                body = {}
            if body.get('success') is False:
                last_error = str(body.get('message') or body.get('detail') or '签到失败')
                low = last_error.lower()
                if 'already' in low or '已签到' in last_error:
                    done = _recheck(last_error)
                    if done:
                        result.update(done)
                    else:
                        result.update(success=True, already=True, message=last_error)
                    return result
                if any(k in low for k in ('turnstile', 'cf_token', 'verify', '人机')):
                    print(f'  [Turnstile] 第 {attempt} 次校验未通过，重新求解')
                    cf_token = ''
                    continue
                if 'captcha' in low or '验证码' in last_error:
                    print(f'  [验证码] 第 {attempt} 次未通过，换一张重试')
                    continue
                result['message'] = f'签到失败: {last_error}'
                return result

            data = body.get('data') if isinstance(body.get('data'), dict) else body
            reward = (data.get('reward') or data.get('quota_awarded')
                      or data.get('amount') or data.get('silver') or data.get('credits'))
            if reward is not None:
                try:
                    result['quota_awarded'] = int(float(reward))
                except (TypeError, ValueError):
                    result['quota_awarded'] = None
            result['checkin_date'] = data.get('date') or data.get('checkin_date')
            msg = body.get('message') or data.get('message') or '签到成功'
            if data.get('already_checked_in') or data.get('already'):
                result['already'] = True
            result['success'] = True
            result['message'] = (f'{msg}（识别码 {answer}）' if answer
                                 else f'{msg}（人机验证通过）')
            return result

        last_error = f'{_detail(resp)}（识别为 {answer or "无验证码"}）'
        low = last_error.lower()
        if any(k in low for k in ('turnstile', 'cf_token', 'verify', '人机验证')):
            print(f'  [Turnstile] 第 {attempt} 次校验未通过，重新求解')
            cf_token = ''
            continue
        if resp.status_code == 422 or '验证码' in last_error or 'captcha' in low:
            print(f'  [验证码] 第 {attempt} 次未通过，换一张重试')
            continue
        done = _recheck(last_error)
        if done:
            result.update(done)
            return result
        result['message'] = f'签到失败: {last_error}'
        return result

    done = _recheck(last_error)
    if done:
        result.update(done)
        return result
    result['message'] = f'签到失败（重试 {max_attempts} 次）: {last_error}'
    return result


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 4:
        print('用法: python arityflow_checkin.py <子站地址> <用户名> <密码> [签到码]')
        sys.exit(2)
    print(checkin_sub_site(sys.argv[1], sys.argv[2], sys.argv[3],
                           sys.argv[4] if len(sys.argv) > 4 else None))
