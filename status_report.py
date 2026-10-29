#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
签到状态巡检（只读）

只发 GET 请求查询各站点「今天签没签到」，不会真实签到，
不会改写 .env / 云端配置，也不会触发任何通知。适合用来快速回答「今天都签到了吗」。

用法：
    python status_report.py                    # 巡检今天（北京时间）
    python status_report.py --month 2026-10    # 巡检指定月份
    python status_report.py --json             # 输出 JSON（便于脚本/通知复用）

配置来源与 checkin.py 完全一致（按优先级）：
    1. CONFIG_URL（云端 WebDAV 配置）
    2. NEWAPI_ACCOUNTS 环境变量
    3. 脚本同目录 .env 里的 NEWAPI_ACCOUNTS

退出码：
    0 = 全部已签到
    1 = 存在未签到 / 状态未知 / 查询失败
    2 = 未找到可用配置或缺少依赖

⚠️ 只对「标准 /api/user/checkin 统计接口」的站点有效。
   使用活动中心（nemodesk / xingya 系）、账密登录（wanwanzhou 系）的站点
   会显示「未知」并给出说明，需要看这些站的实际面板或它自己的签到记录。
"""

import argparse
import json
import os
import sys
from datetime import datetime

try:
    import requests
except ImportError:
    print('[错误] 缺少依赖 requests，请先执行: pip install -r requirements.txt')
    sys.exit(2)

try:
    import pytz
    BEIJING_TZ = pytz.timezone('Asia/Shanghai')
except ImportError:
    # 未装 pytz 时退化为固定 UTC+8，保证巡检脚本仍可用
    from datetime import timedelta, timezone
    BEIJING_TZ = timezone(timedelta(hours=8))

# 复用主脚本的配置加载与脱敏逻辑，避免两套实现走偏
from checkin import NewAPICheckin, load_config_from_cloud, load_env_file, parse_accounts


def _setup_console():
    """统一输出编码，避免 Windows 重定向/GBK 控制台下 emoji 触发 UnicodeEncodeError"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors='replace')                      # 控制台保持原编码，仅降级
            else:
                stream.reconfigure(encoding='utf-8', errors='replace')     # 管道/重定向统一 UTF-8
        except (AttributeError, ValueError, OSError):
            pass


def _symbols() -> dict:
    """能显示 emoji 就用 emoji，GBK 等窄编码环境自动退回纯文本标记"""
    emoji = {
        'done': '✅ 已签到', 'miss': '⭕ 未签到', 'unknown': '❓ 未知',
        'done_s': '✅ 已签', 'miss_s': '⭕ 未签', 'unknown_s': '❓ 未知', 'fail_s': '❌ 失败',
        'fail': '❌', 'tip': '💡', 'warn': '⚠️',
    }
    try:
        ''.join(emoji.values()).encode(sys.stdout.encoding or 'utf-8')
        return emoji
    except (UnicodeEncodeError, LookupError):
        return {
            'done': '[已签]', 'miss': '[未签]', 'unknown': '[未知]',
            'done_s': '[已签]', 'miss_s': '[未签]', 'unknown_s': '[未知]', 'fail_s': '[失败]',
            'fail': '[失败]', 'tip': '[提示]', 'warn': '[注意]',
        }


def now_beijing() -> datetime:
    """当前北京时间（与 checkin.py 的时区口径一致）"""
    return datetime.now(BEIJING_TZ)


def fmt_quota(quota) -> str:
    """额度格式化，与 checkin.py 的 M / K 显示保持一致"""
    if not quota:
        return '0'
    if quota >= 1000000:
        return f'{quota / 1000000:.2f}M'
    if quota >= 1000:
        return f'{quota / 1000:.2f}K'
    return str(quota)


def _server_msg(resp) -> str:
    """提取服务端错误说明（new-api 用 message，FastAPI 用 detail），便于定位失败原因"""
    try:
        body = resp.json()
    except ValueError:
        return ''
    if not isinstance(body, dict):
        return ''
    for key in ('message', 'detail', 'error'):
        val = body.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, dict) and isinstance(val.get('message'), str):
            return val['message']
    return ''


def _log_checkin_today(s: requests.Session, base_url: str, today: str, timeout: int):
    """用「日志」判断今日是否已签到（只读，不触发签到）

    有些站把标准签到停用、改走独立领取接口（如 xingya 的 /api/user/xingya-daily-claim），
    此时 /api/user/checkin 里的 checked_in_today 仍是旧系统口径，会把今天说成未签。
    这类站在 /api/log/self 里会留一条「Check-in credit granted / 签到 / 领取」日志，
    因此用最新一条签到日志的日期兜底判断。

    Returns:
        True 已签 / False 今天还没签 / None 无法判断（没有签到日志或接口不可用）
    """
    try:
        resp = s.get(f'{base_url}/api/log/self', params={'p': 1, 'page_size': 20},
                     timeout=timeout)
        if resp.status_code != 200:
            return None
        payload = (resp.json() or {}).get('data') or {}
    except (requests.exceptions.RequestException, ValueError):
        return None

    items = payload.get('items') or payload.get('records') or []
    if not isinstance(items, list):
        return None

    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get('content') or '').lower()
        if 'check' not in text and '签到' not in text and '领取' not in text:
            continue
        ts = item.get('created_at')
        if not isinstance(ts, (int, float)) or ts <= 0:
            continue
        try:
            day = datetime.fromtimestamp(ts, BEIJING_TZ).strftime('%Y-%m-%d')
        except (OverflowError, OSError, ValueError):
            continue
        if day == today:
            return True
        if day < today:
            # 日志按时间倒序，最新一条签到日志都不是今天 → 今天没签
            return False

    return None


def _activity_checkin_today(base_url: str, account: dict, timeout: int):
    """活动中心系站点（nemodesk）：按活动中心日历判断今日是否已签到（只读）

    这类站的奖励是「白银」silver，不写入 /api/log/self，日志判定一律是「没签」，
    必须用 /api/user/activities/status 的 calendar 判定。

    Returns:
        True 已签 / False 今天还没签 / None 无法判断（非活动中心系 / 登录失败）
    """
    if not (account.get('login_username') and account.get('login_password')):
        return None
    try:
        from activity_checkin import verify_activity_checked_today
    except ImportError:
        return None
    done, _why = verify_activity_checked_today(
        base_url, account['login_username'], account['login_password'],
        account.get('user_id'), timeout=min(timeout, 60), retries=1)
    return done


def load_accounts():
    """按 checkin.py 的优先级加载账号配置，返回 (账号列表, 来源说明)"""
    preexisting = os.environ.get('NEWAPI_ACCOUNTS', '')
    load_env_file()

    accounts_str = ''
    source = ''

    config_url = os.environ.get('CONFIG_URL', '')
    if config_url:
        accounts_str = load_config_from_cloud(config_url, os.environ.get('CONFIG_AUTH', '')) or ''
        source = '云端配置 (CONFIG_URL)'

    if not accounts_str:
        accounts_str = os.environ.get('NEWAPI_ACCOUNTS', '')
        if accounts_str:
            # 环境变量优先；若只有 .env 提供，则标注为本地 .env
            source = '环境变量 NEWAPI_ACCOUNTS' if preexisting else '本地 .env'

    if not accounts_str:
        return [], ''

    return parse_accounts(accounts_str), source


def _build_session(account: dict) -> requests.Session:
    """构造只读会话，认证头与 checkin.py 保持一致"""
    s = requests.Session()
    s.headers.update({
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Cache-Control': 'no-store',
        'Pragma': 'no-cache',
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
    })
    if account.get('access_token'):
        s.headers['Authorization'] = account['access_token']
    if account.get('session'):
        s.cookies.set('session', account['session'])
    if account.get('cf_clearance'):
        s.cookies.set('cf_clearance', account['cf_clearance'])
    if account.get('user_id'):
        s.headers['new-api-user'] = str(account['user_id'])
    return s


def _login_with_password(s: requests.Session, base_url: str, account: dict,
                         timeout: int) -> bool:
    """账密登录拿 session（只登录，绝不触发签到）

    new-api 标准接口 POST /api/user/login，成功后会带 Set-Cookie: session
    """
    if not (account.get('login_username') and account.get('login_password')):
        return False
    try:
        resp = s.post(f'{base_url}/api/user/login',
                      json={'username': account['login_username'],
                            'password': account['login_password']},
                      timeout=timeout)
        if resp.status_code != 200:
            return False
        body = resp.json() or {}
    except (requests.exceptions.RequestException, ValueError):
        return False

    data = body.get('data') or {}
    if not (body.get('success') or data.get('id')):
        return False
    session = data.get('session') or s.cookies.get('session')
    if session:
        s.cookies.set('session', session)
    if data.get('id'):
        s.headers['new-api-user'] = str(data['id'])
    return True


def _query_sub_site(account: dict, info: dict, timeout: int) -> bool:
    """独立签到子站（如 arityflow 的 qd.xxx.top）：账密登录 + GET /api/checkin/status（只读）

    Returns:
        True 已填好 info，False 表示该站没有可用子站或不支持（调用方继续主站逻辑）
    """
    base = (account.get('checkin_url') or '').rstrip('/')
    if not base or not account.get('login_username') or not account.get('login_password'):
        return False
    try:
        from arityflow_checkin import sub_site_login, fetch_status
    except ImportError:
        return False

    try:
        token, user = sub_site_login(base, account['login_username'],
                                     account['login_password'], timeout)
        status = fetch_status(base, token, timeout)
    except Exception as e:  # 子站异常不阻断主站查询
        info['note'] = f'签到子站查询失败: {e}'
        return False

    checked = status.get('checked_today')
    if isinstance(checked, bool):
        info['today'] = checked

    tip = f'签到走独立子站 {NewAPICheckin._mask_url(base)}'
    if status.get('streak') is not None:
        tip += f'，连签 {status.get("streak")} 天'
    if status.get('total_days') is not None:
        tip += f' / 累计 {status.get("total_days")} 天'
    info['note'] = tip

    name = str((user or {}).get('username') or '')
    if name:
        info['user'] = name[:3] + '***' if len(name) > 3 else '***'
    return True


def query_one(account: dict, month: str, today: str) -> dict:
    """
    只读查询单个站点今日签到状态

    Returns:
        dict，其中 today 为 True/False/None；None 表示无法判断
        （接口不支持 / 认证失败 / CF 拦截等，原因放在 error 或 note 里）
    """
    base_url = (account.get('url') or '').rstrip('/')
    info = {
        'name': account.get('name') or base_url,
        'url': base_url,
        'masked_url': NewAPICheckin._mask_url(base_url),
        'user': '',
        'today': None,
        'month_count': None,
        'total_checkins': None,
        'total_quota': None,
        'error': None,
        'note': '',
    }

    timeout = int(account.get('timeout') or 60)
    s = _build_session(account)

    # 0) 独立签到子站（如 arityflow）：主站 /api/user/checkin 对这类站返回无权限，优先查子站
    if account.get('checkin_url') and _query_sub_site(account, info, timeout):
        return info

    # 1) 认证与用户信息：GET /api/user/self
    try:
        resp = s.get(f'{base_url}/api/user/self', timeout=timeout)
    except requests.exceptions.Timeout:
        info['error'] = '请求超时'
        return info
    except requests.exceptions.RequestException as e:
        info['error'] = f'网络请求失败: {e}'
        return info

    if resp.status_code == 401:
        # 只配了账号密码的站（如 youkies）：补做一次登录拿 session（只登录，不签到）
        if (account.get('login_username') and account.get('login_password')
                and not account.get('access_token') and not account.get('session')):
            if _login_with_password(s, base_url, account, timeout):
                try:
                    resp = s.get(f'{base_url}/api/user/self', timeout=timeout)
                except requests.exceptions.RequestException as e:
                    info['error'] = f'登录后获取用户信息失败: {e}'
                    return info

        if resp.status_code == 401:
            detail = _server_msg(resp)
            if account.get('login_username') and not account.get('access_token') and not account.get('session'):
                info['error'] = '认证失败（该站走账密登录，自动登录也没成功）'
            elif 'new-api-user' in detail.lower():
                info['error'] = '认证失败（该站要求令牌搭配数字用户ID，请在配置里补 user_id）'
            else:
                info['error'] = '认证失败（access_token / session 可能已失效）'
            if detail:
                info['note'] = f'服务端返回: {detail}'
            return info

    if resp.status_code != 200:
        detail = _server_msg(resp)
        info['error'] = f'获取用户信息失败 (HTTP {resp.status_code})'
        if detail:
            info['note'] = f'服务端返回: {detail}'
        return info

    try:
        user_data = (resp.json() or {}).get('data') or {}
    except ValueError:
        info['error'] = '响应不是 JSON（可能被 Cloudflare 拦截，本地运行 checkin.py 可走系统代理）'
        return info

    username = user_data.get('username') or ''
    if username:
        # 与 checkin.py 一致：只显示前 3 个字符
        info['user'] = username[:3] + '***' if len(username) > 3 else '***'
    if user_data.get('id'):
        s.headers['new-api-user'] = str(user_data['id'])

    # 2) 签到统计：GET /api/user/checkin?month=YYYY-MM
    try:
        resp = s.get(f'{base_url}/api/user/checkin', params={'month': month}, timeout=timeout)
    except requests.exceptions.Timeout:
        info['error'] = '查询签到统计超时'
        return info
    except requests.exceptions.RequestException as e:
        info['error'] = f'查询签到统计失败: {e}'
        return info

    if resp.status_code == 404:
        state = _log_checkin_today(s, base_url, today, timeout)
        if state is not None:
            info['today'] = state
            info['note'] = '该站无标准签到统计接口，按签到日志判定'
        else:
            info['note'] = '该站没有标准签到统计接口（活动中心 / 自定义机制），无法自动判断'
        return info

    try:
        data = resp.json() or {}
    except ValueError:
        info['error'] = f'签到统计响应不是 JSON (HTTP {resp.status_code})'
        return info

    if not data.get('success'):
        server_msg = data.get('message') or f'签到统计接口返回失败 (HTTP {resp.status_code})'
        # 先查活动中心日历（nemodesk 系奖励是 silver，不落标准签到日志，
        # 用日志判会误报「没签」）；活动中心不可用再退回日志判定
        state = _activity_checkin_today(base_url, account, timeout)
        if state is not None:
            info['today'] = state
            info['note'] = f'标准签到未启用（{server_msg}），按活动中心日历判定'
            return info
        state = _log_checkin_today(s, base_url, today, timeout)
        if state is not None:
            info['today'] = state
            info['note'] = f'标准签到未启用（{server_msg}），按签到日志判定'
        else:
            info['note'] = server_msg
        return info

    payload = data.get('data') or {}
    stats = payload.get('stats') or payload
    records = stats.get('records') or payload.get('records') or []

    info['month_count'] = stats.get('checkin_count')
    info['total_checkins'] = stats.get('total_checkins')
    info['total_quota'] = stats.get('total_quota')

    checked_today = stats.get('checked_in_today')
    if isinstance(checked_today, bool):
        info['today'] = checked_today
    else:
        # 没有 checked_in_today 字段时，用 records 里的日期兜底
        dates = {str(r.get('checkin_date', ''))[:10] for r in records if isinstance(r, dict)}
        if dates:
            info['today'] = today in dates

    # 该站标准签到已停用（enabled=false，改走独立领取接口如 xingya-daily-claim）：
    # 上面的 checked_in_today 是旧系统口径，改用签到日志判定
    if payload.get('enabled') is False:
        state = _log_checkin_today(s, base_url, today, timeout)
        if state is True:
            info['today'] = True
            info['note'] = '标准签到已停用，按「每日领取」签到日志判定'
        elif state is False:
            info['today'] = False
            info['note'] = '标准签到已停用，签到日志里今天还没有领取记录'
        else:
            info['today'] = None
            info['note'] = '标准签到已停用（该站可能无需签到），日志里没有签到记录，无法判断'

    if info['today'] is None:
        info['note'] = info['note'] or '接口未返回今日状态'

    return info


def print_report(results: list, month: str, source: str):
    """文本报表（分隔线与 emoji 风格与 checkin.py 保持一致）"""
    print('=' * 60)
    print('签到状态巡检（只读 · 不会签到）')
    print(f'检查时间: {now_beijing().strftime("%Y-%m-%d %H:%M:%S")} (北京时间)')
    print(f'统计月份: {month}    配置来源: {source}')
    print('=' * 60)

    sym = _symbols()

    for i, info in enumerate(results, 1):
        print(f'[{i}/{len(results)}] {info["name"]}')
        print(f'  站点: {info["masked_url"]}')
        if info['user']:
            print(f'  用户: {info["user"]}')

        if info['today'] is True:
            print(f'  今日: {sym["done"]}')
        elif info['today'] is False:
            print(f'  今日: {sym["miss"]}')
        else:
            print(f'  今日: {sym["unknown"]}')

        if info['month_count'] is not None or info['total_quota'] is not None:
            print(f'  统计: 本月 {info["month_count"] or 0} 天，'
                  f'累计 {info["total_checkins"] or 0} 天 / '
                  f'{fmt_quota(info["total_quota"])} 额度')

        if info['error']:
            print(f'  {sym["fail"]} {info["error"]}')
        if info['note']:
            print(f'  {sym["tip"]} {info["note"]}')
        print()

    done = sum(1 for r in results if r['today'] is True)
    undone = sum(1 for r in results if r['today'] is False)
    unknown = sum(1 for r in results if r['today'] is None and not r['error'])
    failed = sum(1 for r in results if r['error'])

    print('=' * 60)
    print(f'汇总: 共 {len(results)} 个站点 · {sym["done_s"]} {done} / {sym["miss_s"]} {undone} / '
          f'{sym["unknown_s"]} {unknown} / {sym["fail_s"]} {failed}')
    print('=' * 60)

    if undone:
        names = '、'.join(r['name'] for r in results if r['today'] is False)
        print(f'{sym["warn"]} 今天还没签: {names}')
        print('   补签: python checkin.py（会真实签到，未签的站直接签掉）')
    if failed:
        names = '、'.join(r['name'] for r in results if r['error'])
        print(f'{sym["warn"]} 查询失败: {names}')
        print('   多为令牌失效或 CF 拦截，详见上面各站说明')


def main():
    _setup_console()
    parser = argparse.ArgumentParser(description='NewAPI 签到状态巡检（只读）')
    parser.add_argument('--month', default=None,
                        help='统计月份，格式 YYYY-MM，默认当前月（北京时间）')
    parser.add_argument('--json', action='store_true', help='以 JSON 输出结果')
    args = parser.parse_args()

    now = now_beijing()
    month = args.month or now.strftime('%Y-%m')
    today = now.strftime('%Y-%m-%d')

    accounts, source = load_accounts()
    if not accounts:
        print('=' * 60)
        print('[错误] 未找到账号配置，无法巡检')
        print('=' * 60)
        print('请任选一种方式提供配置：')
        print('  1. 把 .env（含 NEWAPI_ACCOUNTS=...）放到本脚本同目录')
        print('  2. 设置环境变量 NEWAPI_ACCOUNTS（JSON 数组）')
        print('  3. 设置 CONFIG_URL（云端 WebDAV 配置）')
        sys.exit(2)

    results = []
    for account in accounts:
        if account.get('disabled'):
            name = account.get('name') or account.get('url')
            if not args.json:
                print(f'跳过（手动签到站）: {name}')
            continue
        if not args.json:
            print(f'正在查询: {account.get("name") or account.get("url")} ...')
        results.append(query_one(account, month, today))

    if args.json:
        print(json.dumps({
            'checked_at': now.strftime('%Y-%m-%d %H:%M:%S'),
            'month': month,
            'today': today,
            'source': source,
            'results': results,
        }, ensure_ascii=False, indent=2))
    else:
        print()
        print_report(results, month, source)

    sys.exit(0 if all(r['today'] is True for r in results) else 1)


if __name__ == '__main__':
    main()
