"""
新版 new-api fork 的「活动中心」签到适配

背景：部分中转站使用了深度定制的 new-api fork，把传统签到接口
(POST /api/user/checkin) 替换成了新机制：

1. nemodesk 系（活动中心 / 白银 silver）：
   - 旧接口返回 {"message": "Use Activity Center to collect silver"}
   - 新流程：POST /api/user/login 拿 JWT → GET /api/user/activities/status
     → POST /api/user/activities/command {kind:"checkin",target,request_id}
   - command 接口校验 Origin/Referer 头（CSRF 防护），必须带上

2. xingya 系（星河专属签到）：
   - 旧接口返回 410 {"code": "checkin_endpoint_retired"}
   - 新流程：POST /api/user/login 拿 JWT → POST /api/user/xingya-daily-claim

两个 fork 的写操作都要求 JWT（账号密码登录获得），系统访问令牌(nap_)只读。
"""

import uuid
import time
import requests


def jwt_login(base_url: str, username: str, password: str, timeout: int = 30) -> str:
    """
    账号密码登录，返回 JWT access_token

    Returns:
        JWT 字符串；失败返回 None
    """
    try:
        resp = requests.post(
            f'{base_url}/api/user/login',
            json={'username': username, 'password': password},
            timeout=timeout
        )
        data = resp.json()
        token = (data.get('data') or {}).get('access_token')
        if token:
            return token
        print(f'[活动中心] 登录失败: {data.get("message") or resp.status_code}')
    except Exception as e:
        print(f'[活动中心] 登录异常: {e}')
    return None


def _headers(base_url: str, jwt: str, user_id) -> dict:
    return {
        'Authorization': f'Bearer {jwt}',
        'New-Api-User': str(user_id or ''),
        'Origin': base_url,
        'Referer': f'{base_url}/activities/checkin',
        'Content-Type': 'application/json',
    }


def checkin_activity_center(base_url: str, jwt: str, user_id, timeout: int = 30) -> dict:
    """
    nemodesk 系「活动中心」签到

    流程：查状态 → 今日未签则发 checkin 命令
    """
    headers = _headers(base_url, jwt, user_id)

    # 1. 查签到状态
    try:
        resp = requests.get(f'{base_url}/api/user/activities/status',
                            headers=headers, timeout=timeout)
        status = resp.json()
    except Exception as e:
        return {'success': False, 'message': f'活动中心状态查询异常: {e}'}

    data = status.get('data') or {}
    checkin = data.get('checkin') or {}
    today = data.get('today')
    calendar = checkin.get('calendar') or []

    # 今日已签到（含补签记录）
    today_records = [r for r in calendar if r.get('day') == today]
    if today_records:
        silver = today_records[0].get('silver', 0)
        return {'success': True, 'message': f'今日已签到（+{silver} 白银）',
                'already': True, 'quota_awarded': silver}

    # 站点暂停或未启用
    if data.get('paused') or not (data.get('config') or {}).get('enabled', True):
        return {'success': False, 'message': '活动中心签到未启用或已暂停'}

    # 2. 发签到命令（kind=checkin, target=null, request_id=UUID 幂等键）
    request_id = str(uuid.uuid4())
    try:
        resp = requests.post(
            f'{base_url}/api/user/activities/command',
            headers=headers,
            json={'kind': 'checkin', 'target': None, 'request_id': request_id},
            timeout=timeout
        )
        result = resp.json()
    except Exception as e:
        # 超时/连接被重置 ≠ 没签上：服务端可能已收到并入账。
        # 等两秒后只读复查一次，确认到底成没成再下结论。
        try:
            time.sleep(2)
            recheck = requests.get(f'{base_url}/api/user/activities/status',
                                   headers=headers, timeout=timeout).json()
            data2 = recheck.get('data') or {}
            cal2 = (data2.get('checkin') or {}).get('calendar') or []
            recs = [r for r in cal2 if r.get('day') == data2.get('today')]
            if recs:
                silver = recs[0].get('silver', 0)
                return {'success': True, 'already': True,
                        'message': f'签到请求超时，复查确认今日已签到（+{silver} 白银）',
                        'quota_awarded': silver}
        except Exception:
            pass
        return {'success': False, 'message': f'签到命令异常: {e}'}

    if result.get('success'):
        # 命令成功后重新查状态拿今日奖励
        try:
            status2 = requests.get(f'{base_url}/api/user/activities/status',
                                   headers=headers, timeout=timeout).json()
            cal = ((status2.get('data') or {}).get('checkin') or {}).get('calendar') or []
            rec = [r for r in cal if r.get('day') == today]
            silver = rec[0].get('silver', 0) if rec else 0
        except Exception:
            silver = 0
        return {'success': True, 'message': f'签到成功（+{silver} 白银）',
                'quota_awarded': silver}

    msg = result.get('message') or result.get('error', {}).get('message') or str(result)
    low = msg.lower()
    if 'already' in low or '已签到' in msg or '已领取' in msg:
        # 服务端说「已签到」时以它为准，别当成失败
        done, why, silver = _query_today(base_url, headers, timeout, today=today)
        if done is True:
            return {'success': True, 'already': True, 'message': why,
                    'quota_awarded': silver}
        return {'success': True, 'already': True, 'message': msg}
    return {'success': False, 'message': f'活动中心签到失败: {msg}'}


def _query_today(base_url: str, headers: dict, timeout: int, today=None):
    """只读复查：查活动中心状态 → (True/False/None, 说明, silver)"""
    try:
        resp = requests.get(f'{base_url}/api/user/activities/status',
                            headers=headers, timeout=timeout)
        if resp.status_code == 404:
            return None, '该站没有活动中心接口', 0
        data = (resp.json() or {}).get('data') or {}
    except Exception as e:
        return None, f'状态查询异常: {e}', 0
    checkin = data.get('checkin') or {}
    if not checkin:
        return None, '该站无活动中心数据', 0
    today = today or data.get('today')
    records = [r for r in (checkin.get('calendar') or []) if r.get('day') == today]
    if records:
        silver = records[0].get('silver', 0)
        return True, f'今日已签到（+{silver} 白银）', silver
    return False, '活动中心显示今日未签到', 0


def verify_activity_checked_today(base_url: str, username: str, password: str, user_id,
                                  timeout: int = 60, retries: int = 2):
    """只读复查：活动中心系站点今天到底签到了没有

    用途：签到命令发出后请求超时/连接被重置时，服务端往往已经入账，
    这时不能再报「失败」，否则手机上会显示「明明签到了却说超时」。

    Returns:
        (True, 说明)  今天已签到
        (False, 说明) 今天确实还没签到
        (None, 说明)  查不出来（该站不是活动中心系 / 登录失败 / 接口不可用）
    """
    import time

    base_url = base_url.rstrip('/')
    jwt = jwt_login(base_url, username, password, timeout)
    if not jwt:
        return None, '账号密码登录失败，无法复查'
    headers = _headers(base_url, jwt, user_id)

    last_msg = ''
    for i in range(max(1, retries)):
        try:
            resp = requests.get(f'{base_url}/api/user/activities/status',
                                headers=headers, timeout=timeout)
            if resp.status_code == 404:
                return None, '该站没有活动中心接口'
            data = (resp.json() or {}).get('data') or {}
        except Exception as e:
            last_msg = f'状态查询异常: {e}'
            time.sleep(2)
            continue

        checkin = data.get('checkin') or {}
        if not checkin:
            return None, '该站无活动中心数据'
        today = data.get('today')
        records = [r for r in (checkin.get('calendar') or []) if r.get('day') == today]
        if records:
            return True, f'今日已签到（+{records[0].get("silver", 0)} 白银）'
        last_msg = '活动中心显示今日未签到'
        time.sleep(2)
    return False, last_msg


def checkin_xingya(base_url: str, jwt: str, user_id, timeout: int = 30) -> dict:
    """
    xingya 系「每日领取」签到（POST /api/user/xingya-daily-claim）
    """
    headers = _headers(base_url, jwt, user_id)
    headers['Referer'] = f'{base_url}/personal'

    try:
        resp = requests.post(f'{base_url}/api/user/xingya-daily-claim',
                             headers=headers, timeout=timeout)
        result = resp.json()
    except Exception as e:
        return {'success': False, 'message': f'领取异常: {e}'}

    data = result.get('data') or {}
    if result.get('success') or data:
        quota = data.get('quota_awarded', 0)
        if data.get('already_claimed'):
            return {'success': True, 'message': f'今日已领取（{quota} 额度）',
                    'already': True, 'quota_awarded': quota}
        return {'success': True, 'message': f'签到成功（+{quota} 额度）',
                'quota_awarded': quota}

    msg = result.get('message') or str(result)
    return {'success': False, 'message': f'领取失败: {msg}'}


def is_activity_center_site(base_url: str) -> bool:
    """探测是否为活动中心系站点（nemodesk）"""
    try:
        resp = requests.get(f'{base_url}/api/user/activities/status', timeout=15)
        return resp.status_code != 404
    except Exception:
        return False


def checkin_wanwanzhou(base_url: str, username: str, password: str,
                       timeout: int = 120) -> dict:
    """
    晚晚舟（wanwanzhou）系签到：API 前缀为 /api/v1/ 的自定义 fork

    流程：POST /api/v1/auth/login 拿 JWT → GET /api/v1/user/checkin 查状态
          → POST /api/v1/user/checkin 签到（奖励为余额，需邀请人数解锁）
    """
    base_url = base_url.rstrip('/')
    headers = {'Content-Type': 'application/json'}

    # 1. 账密登录拿 JWT
    # 注意1：站点 http:// 常 301 跳 https，requests 自动跟随会把 POST 降级为 GET
    #        （登录变 GET 404），这里手动保持 POST 重试
    # 注意2：该 fork 登录要求 email 字段（LoginRequest.Email required），
    #        同时带上 username/email 两个字段兼容不同站点
    login_body = {'username': username, 'email': username, 'password': password}
    try:
        resp = requests.post(f'{base_url}/api/v1/auth/login',
                             json=login_body,
                             timeout=timeout, allow_redirects=False)
        if resp.status_code in (301, 302):
            location = resp.headers.get('Location', '')
            if location.startswith('https://'):
                from urllib.parse import urlparse
                p = urlparse(location)
                base_url = f'{p.scheme}://{p.netloc}'
                resp = requests.post(location, json=login_body, timeout=timeout)
        data = resp.json()
    except Exception as e:
        return {'success': False, 'message': f'登录异常: {e}'}

    token = (data.get('data') or {}).get('access_token')
    if not token:
        return {'success': False,
                'message': f'登录失败: {data.get("message") or resp.status_code}'}
    user = (data.get('data') or {}).get('user') or {}
    user_id = user.get('id')
    headers['Authorization'] = f'Bearer {token}'
    headers['New-Api-User'] = str(user_id or '')

    # 2. 查签到状态
    try:
        status = requests.get(f'{base_url}/api/v1/user/checkin',
                              headers=headers, timeout=timeout).json()
    except Exception as e:
        return {'success': False, 'message': f'状态查询异常: {e}'}

    sdata = status.get('data') or {}
    if sdata.get('checked_in_today'):
        return {'success': True, 'message': '今日已签到', 'already': True}

    if not sdata.get('unlocked', True):
        need = sdata.get('invites_needed', 0)
        return {'success': False,
                'message': f'签到未解锁：还需邀请 {need} 人'}

    # 3. 签到
    try:
        r = requests.post(f'{base_url}/api/v1/user/checkin',
                          headers=headers, timeout=timeout).json()
    except Exception as e:
        return {'success': False, 'message': f'签到异常: {e}'}

    rdata = r.get('data') or {}
    if r.get('code') == 0 or rdata:
        if rdata.get('already_checked_in'):
            return {'success': True, 'message': '今日已签到', 'already': True}
        msg = rdata.get('message', '签到成功')
        return {'success': True, 'message': f'签到成功（{msg}）'}
    return {'success': False,
            'message': f'签到失败: {r.get("message") or r}'}


def run_activity_checkin(base_url: str, username: str, password: str, user_id,
                         timeout: int = 30, site_hint: str = None) -> dict:
    """
    活动中心系签到总入口：JWT 登录 → 自动识别 fork → 签到

    Args:
        site_hint: 调用方提供的站点类型线索（可选）
            - 'xingya': 旧接口返回 410 / checkin_endpoint_retired，直接走星河领取
            - 'activity_center': 旧接口提示 Use Activity Center，直接走活动中心
            - None: 未提供线索，自动探测（GET /api/user/activities/status）

    Returns:
        dict: {success, message, quota_awarded, ...}
    """
    base_url = base_url.rstrip('/')

    jwt = jwt_login(base_url, username, password, timeout)
    if not jwt:
        return {'success': False, 'message': '账号密码登录失败，无法获取 JWT'}

    # 有明确线索 → 直接分发，不再探测
    if site_hint == 'xingya':
        return checkin_xingya(base_url, jwt, user_id, timeout)
    if site_hint == 'activity_center':
        return checkin_activity_center(base_url, jwt, user_id, timeout)

    # 无线索 → 探测：先试活动中心系（nemodesk），再试 xingya 系
    if is_activity_center_site(base_url):
        return checkin_activity_center(base_url, jwt, user_id, timeout)

    result = checkin_xingya(base_url, jwt, user_id, timeout)
    if result.get('success') or 'invalid' not in result.get('message', '').lower():
        return result

    return {'success': False, 'message': '未识别的活动中心系站点，请联系维护者补充适配'}
