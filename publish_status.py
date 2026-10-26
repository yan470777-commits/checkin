#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把签到 + 巡检结果整理成手机端可直接读取的数据

云端（GitHub Actions）跑完 checkin.py 后执行本脚本，生成三份文件：

    docs/data/latest.json              最新一次状态（手机页面 / 你的 App 读这个）
    docs/data/history/YYYY-MM-DD.json  当天快照
    docs/data/history.json             最近 60 天汇总（趋势列表）

除写这三个 JSON 外全是只读 GET 请求，绝不会触发签到。

用法：
    python publish_status.py
    python publish_status.py --checkin-json logs/checkin_result.json
    python publish_status.py --out-dir docs/data
"""

import argparse
import json
import os
import sys

from status_report import fmt_quota, load_accounts, now_beijing, query_one

STATE_ICON = {'done': '[已签]', 'miss': '[未签]', 'unknown': '[未知]',
              'fail': '[失败]', 'skipped': '[手动]'}


def state_of(today, error):
    """把 today(True/False/None) + error 归一成页面用的状态码"""
    if error:
        return 'fail'
    if today is True:
        return 'done'
    if today is False:
        return 'miss'
    return 'unknown'


def load_checkin(path):
    """读取 checkin.py --json-out 的结果，按站点名建索引"""
    if not path or not os.path.isfile(path):
        return {}, {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return {}, {}

    index = {}
    for item in payload.get('results') or []:
        name = str(item.get('name') or '')
        if not name:
            continue
        index[name] = {
            'success': bool(item.get('success')),
            'message': item.get('message') or '',
            'quota_awarded': item.get('quota_awarded'),
            'advice': item.get('advice') or '',
        }
    return index, payload.get('summary') or {}


def lookup_checkin(index, name):
    """按站点名取当天签到结果，名字不完全一致时做一次包含匹配"""
    if not index:
        return None
    if name in index:
        return index[name]
    for key, value in index.items():
        if key and (key in name or name in key):
            return value
    return None


def build_sites(accounts, checkin_index, month, today):
    """逐站查询今日状态（只读），并合并当天真实签到结果"""
    sites = []
    for account in accounts:
        name = account.get('name') or account.get('url') or '未命名站点'

        if account.get('disabled') or account.get('manual'):
            sites.append({
                'name': name,
                'site': '',
                'user': '',
                'state': 'skipped',
                'month_days': None,
                'total_days': None,
                'total_quota': None,
                'total_quota_text': '',
                'note': '手动签到站（配置了 disabled），脚本不参与，需要你在手机上自己点',
                'error': '',
                'checkin': None,
            })
            continue

        info = query_one(account, month, today)
        sites.append({
            'name': info['name'],
            'site': info['masked_url'],
            'user': info['user'],
            'state': state_of(info['today'], info['error']),
            'month_days': info['month_count'],
            'total_days': info['total_checkins'],
            'total_quota': info['total_quota'],
            'total_quota_text': fmt_quota(info['total_quota']) if info['total_quota'] else '',
            'note': info['note'] or '',
            'error': info['error'] or '',
            'checkin': lookup_checkin(checkin_index, info['name']),
        })
    return sites


def summarize(sites):
    return {
        'total': len(sites),
        'done': sum(1 for s in sites if s['state'] == 'done'),
        'miss': sum(1 for s in sites if s['state'] == 'miss'),
        'unknown': sum(1 for s in sites if s['state'] == 'unknown'),
        'fail': sum(1 for s in sites if s['state'] == 'fail'),
        'skipped': sum(1 for s in sites if s['state'] == 'skipped'),
    }


def write_json(path, payload):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def update_history(out_dir, entry):
    """维护最近 60 天的汇总列表（同日覆盖）"""
    path = os.path.join(out_dir, 'history.json')
    history = []
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                history = [h for h in loaded if isinstance(h, dict)]
        except (OSError, ValueError):
            history = []

    history = [h for h in history if h.get('date') != entry['date']]
    history.append(entry)
    history.sort(key=lambda h: str(h.get('date') or ''))
    write_json(path, history[-60:])


def print_sites(sites, month):
    """控制台报表（云端日志里可读，不用 emoji 以免终端编码问题）"""
    print('=' * 60)
    print('签到状态汇总（生成手机端数据用 · 只读）')
    print(f'更新时间: {now_beijing().strftime("%Y-%m-%d %H:%M:%S")}    统计月份: {month}')
    print('=' * 60)
    for i, site in enumerate(sites, 1):
        print(f'[{i}/{len(sites)}] {site["name"]}  {STATE_ICON.get(site["state"], "")}')
        if site['site']:
            print(f'  站点: {site["site"]}')
        stats = []
        if site['month_days'] is not None:
            stats.append(f'本月 {site["month_days"]} 天')
        if site['total_days'] is not None:
            stats.append(f'累计 {site["total_days"]} 天')
        if site['total_quota_text']:
            stats.append(f'{site["total_quota_text"]} 额度')
        if stats:
            print('  统计: ' + '，'.join(stats))
        if site['note']:
            print(f'  说明: {site["note"]}')
        if site['error']:
            print(f'  错误: {site["error"]}')
        ck = site['checkin']
        if ck:
            award = f'（+{ck["quota_awarded"]}）' if ck.get('quota_awarded') else ''
            print(f'  签到: {"成功" if ck["success"] else "失败"}{award} {ck["message"]}')
        print()


def main():
    parser = argparse.ArgumentParser(description='生成手机端可读的签到状态数据（只读）')
    parser.add_argument('--checkin-json', default=os.environ.get('CHECKIN_JSON_OUT', ''),
                        help='checkin.py --json-out 生成的结果文件，用于展示当天真实签到结果')
    parser.add_argument('--out-dir',
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'docs', 'data'),
                        help='输出目录，默认 ./docs/data')
    parser.add_argument('--month', default=None, help='统计月份 YYYY-MM，默认当前月（北京时间）')
    args = parser.parse_args()

    now = now_beijing()
    today = now.strftime('%Y-%m-%d')
    month = args.month or now.strftime('%Y-%m')
    stamp = now.strftime('%Y-%m-%d %H:%M:%S')

    accounts, source = load_accounts()
    if not accounts:
        write_json(os.path.join(args.out_dir, 'latest.json'), {
            'updated_at': stamp,
            'date': today,
            'error': '未找到账号配置（NEWAPI_ACCOUNTS / .env / CONFIG_URL 都为空）',
            'summary': summarize([]),
            'sites': [],
        })
        print('[错误] 未找到账号配置，无法生成状态数据')
        sys.exit(2)

    checkin_index, checkin_summary = load_checkin(args.checkin_json)
    sites = build_sites(accounts, checkin_index, month, today)
    summary = summarize(sites)

    payload = {
        'updated_at': stamp,
        'date': today,
        'month': month,
        'source': source,
        'summary': summary,
        'checkin_run': checkin_summary,
        'sites': sites,
    }
    write_json(os.path.join(args.out_dir, 'latest.json'), payload)
    write_json(os.path.join(args.out_dir, 'history', f'{today}.json'), payload)
    update_history(args.out_dir, {
        'date': today,
        'updated_at': stamp,
        'summary': summary,
        'checkin_run': checkin_summary,
    })

    # 文本报表（云端日志里可读）
    print_sites(sites, month)

    print(f'汇总: 共 {summary["total"]} 站 · 已签 {summary["done"]} / 未签 {summary["miss"]} / '
          f'未知 {summary["unknown"]} / 失败 {summary["fail"]} / 手动 {summary["skipped"]}')
    if checkin_summary:
        print(f'[当天签到] 成功 {checkin_summary.get("success", 0)} / '
              f'失败 {checkin_summary.get("failed", 0)} / 跳过 {checkin_summary.get("skipped", 0)}')
    elif not checkin_index:
        print('[提示] 没读到 checkin 结果文件，本次只展示「今日是否已签」查询结果')
    print(f'[输出] {os.path.abspath(args.out_dir)}')


if __name__ == '__main__':
    main()
