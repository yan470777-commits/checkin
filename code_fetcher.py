#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日签到码自动抓取模块

场景：部分中转站签到时要输入当天「签到码 / 签到口令」。
码通常发布在：
- 站点公告 / 群置顶 / Telegram 频道（用户自维护的 URL）
- 站点自己的公告接口（newapi: GET /api/notice）
- 站点首页 HTML

三种用法：
1. 站点配置中固定写死：checkin_code: "ABC123"
2. 站点配置自动抓取：checkin_code_url: "https://..."（可配 checkin_code_regex）
3. 命令行手动抓取验证：python code_fetcher.py <url> [正则]
"""

import re
import json
from typing import Optional
from urllib.parse import urljoin

# 常见签到码发布格式（按优先级尝试）
# 说明：关键词后允许跟随最多 8 个非码字符（中文标点、"是/为"、空格、引号等）
DEFAULT_PATTERNS = [
    r'签到码[】\]\)\s:：是为领取]{0,8}["“\']?([A-Za-z0-9_\-]{4,24})',
    r'签到口令[】\]\)\s:：是为领取]{0,8}["“\']?([A-Za-z0-9_\-]{4,24})',
    r'今日口令[】\]\)\s:：是为领取]{0,8}["“\']?([A-Za-z0-9_\-]{4,24})',
    r'口令[】\]\)\s:：是为领取]{0,8}["“\']?([A-Za-z0-9_\-]{4,24})',
    r'checkin[_\- ]?code["“\']?\s*[:：=]\s*["“\']?([A-Za-z0-9_\-]{4,24})',
    r'["\']?code["\']?\s*[:：=]\s*["“\']([A-Za-z0-9_\-]{4,24})["“\']',
]

# 明显不是签到码的词，命中则跳过该候选
_BLACKLIST = {'true', 'false', 'null', 'none', 'checkin', 'sign', '签到', 'code', 'error'}


def extract_code(text: str, regex: str = None) -> Optional[str]:
    """
    从文本中提取签到码

    Args:
        text: 网页 / 接口返回的文本
        regex: 自定义正则（需含一个捕获组）；不传则用内置常见格式

    Returns:
        签到码字符串，找不到返回 None
    """
    if not text:
        return None

    if regex:
        m = re.search(regex, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        return None

    for pattern in DEFAULT_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            code = m.group(1).strip()
            if code.lower() not in _BLACKLIST and len(code) >= 4:
                return code
    return None


def fetch_code_from_url(url: str, regex: str = None, session=None,
                        timeout: int = 30) -> Optional[str]:
    """
    从指定 URL 抓取页面并提取签到码

    Args:
        url: 发布签到码的页面地址（站点公告页 / gist / 频道镜像页等）
        regex: 自定义提取正则
        session: requests.Session（带上浏览器 cookie，访问需登录的公告页）
    """
    import requests
    try:
        getter = session.get if session is not None else requests.get
        resp = getter(url, timeout=timeout, headers={
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
        })
        if resp.status_code != 200:
            print(f'[签到码] 抓取失败 HTTP {resp.status_code}: {url}')
            return None
        code = extract_code(resp.text, regex)
        if code:
            print(f'[签到码] 从 {url} 抓到签到码')
        else:
            print(f'[签到码] {url} 页面中未找到签到码')
        return code
    except Exception as e:
        print(f'[签到码] 抓取异常: {e}')
        return None


def fetch_code_from_site(base_url: str, session=None, regex: str = None,
                         timeout: int = 30) -> Optional[str]:
    """
    尝试从站点自身抓取签到码：
    1. newapi 公告接口 GET /api/notice
    2. 站点首页 HTML
    """
    base_url = base_url.rstrip('/')

    # newapi 公告接口
    try:
        import requests
        getter = session.get if session is not None else requests.get
        resp = getter(f'{base_url}/api/notice', timeout=timeout)
        if resp.status_code == 200:
            code = extract_code(resp.text, regex)
            if code:
                print('[签到码] 从站点公告接口抓到签到码')
                return code
    except Exception:
        pass

    # 首页 HTML
    try:
        import requests
        getter = session.get if session is not None else requests.get
        resp = getter(base_url, timeout=timeout)
        if resp.status_code == 200:
            code = extract_code(resp.text, regex)
            if code:
                print('[签到码] 从站点首页抓到签到码')
                return code
    except Exception:
        pass

    return None


def resolve_checkin_code(checkin_code: str = None, checkin_code_url: str = None,
                         checkin_code_regex: str = None, base_url: str = None,
                         session=None) -> Optional[str]:
    """
    按优先级解析当日签到码：
    1. 配置里写死的 checkin_code（支持 ${ENV_VAR} 占位）
    2. checkin_code_url 自动抓取
    3. 站点自身接口/首页
    """
    import os

    # 1. 固定值（支持环境变量占位）
    if checkin_code:
        code = checkin_code.strip()
        m = re.fullmatch(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', code)
        if m:
            code = os.environ.get(m.group(1), '').strip()
        if code:
            return code

    # 2. 指定 URL 抓取
    if checkin_code_url:
        code = fetch_code_from_url(checkin_code_url, checkin_code_regex, session)
        if code:
            return code

    # 3. 站点自身
    if base_url:
        code = fetch_code_from_site(base_url, session, checkin_code_regex)
        if code:
            return code

    return None


def main():
    import sys
    if len(sys.argv) < 2:
        print('用法: python code_fetcher.py <URL> [自定义正则]')
        print('示例: python code_fetcher.py https://example.com/announcement')
        sys.exit(1)

    url = sys.argv[1]
    regex = sys.argv[2] if len(sys.argv) > 2 else None

    code = fetch_code_from_url(url, regex)
    if code:
        print(f'\n✅ 签到码: {code}')
        # 输出可直接粘贴的 JSON 片段
        print(f'\n配置片段: "checkin_code": "{code}"')
    else:
        print('\n❌ 未找到签到码，可尝试用第二个参数传自定义正则（含一个捕获组）')
        sys.exit(1)


if __name__ == '__main__':
    main()
