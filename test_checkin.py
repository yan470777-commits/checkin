#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NewAPI 签到测试脚本
用于快速测试单个站点的签到功能
"""

import sys
from checkin import NewAPICheckin


def test_checkin(base_url: str, session_cookie: str, user_id: str = None, verbose: bool = False):
    """测试签到功能"""
    print('=' * 50)
    print('NewAPI 签到测试')
    print('=' * 50)
    print(f'站点: {base_url}')
    print(f'Session 长度: {len(session_cookie)} 字符')
    if user_id:
        print(f'用户ID: {user_id} (手动指定)')
    if verbose:
        print(f'Session 开头: {session_cookie[:50]}...')
    print()

    # 创建客户端
    client = NewAPICheckin(base_url, session_cookie, user_id)

    if client.user_id:
        print(f'✅ 用户ID: {client.user_id}')
    else:
        print('⚠️  未配置用户ID，将尝试从 API 获取')
    print()

    # 测试获取用户信息
    print('[1/3] 测试获取用户信息...')
    user_info = client.get_user_info(verbose=verbose)
    if user_info:
        print(f'✅ 成功')
        print(f'  用户名: {user_info.get("username")}')
        print(f'  用户ID: {user_info.get("id")}')
    else:
        print('❌ 失败 - Session 可能已过期')
        print('\n💡 问题排查：')
        print('  1. 检查 Session Cookie 是否正确完整')
        print('  2. 重新登录网站获取新的 Session')
        print('  3. 使用 --verbose 参数查看详细错误信息')
        print('     python test_checkin.py <URL> <SESSION> --verbose')
        return False

    # 测试签到
    print('\n[2/3] 测试签到...')
    result = client.checkin()
    if result['success']:
        print(f'✅ {result["message"]}')
        if result['checkin_date']:
            print(f'  签到日期: {result["checkin_date"]}')
        if result['quota_awarded']:
            quota = result['quota_awarded']
            if quota >= 1000000:
                quota_str = f'{quota / 1000000:.2f}M'
            elif quota >= 1000:
                quota_str = f'{quota / 1000:.2f}K'
            else:
                quota_str = str(quota)
            print(f'  获得额度: +{quota_str} ({quota:,} tokens)')
    else:
        print(f'❌ {result["message"]}')
        return False

    # 测试获取签到历史
    print('\n[3/3] 测试获取签到历史...')
    history = client.get_checkin_history()
    if history:
        print('✅ 成功')
        if history.get('stats'):
            stats = history['stats']
            print(f'  本月签到: {stats.get("checkin_count", 0)} 天')
            total = stats.get('total_quota', 0)
            if total >= 1000000:
                total_str = f'{total / 1000000:.2f}M'
            elif total >= 1000:
                total_str = f'{total / 1000:.2f}K'
            else:
                total_str = str(total)
            print(f'  累计额度: {total_str} ({total:,} tokens)')
            print(f'  今日已签: {"是" if stats.get("checked_in_today") else "否"}')
    else:
        print('⚠️  获取失败（不影响签到）')

    print('\n' + '=' * 50)
    print('测试完成！所有功能正常 ✅')
    print('=' * 50)
    return True


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print('使用方法：')
        print('  python test_checkin.py <BASE_URL> <SESSION_COOKIE> [选项]')
        print('')
        print('参数说明：')
        print('  BASE_URL       - 站点地址，如 https://api.example.com')
        print('  SESSION_COOKIE - 从浏览器获取的 session 值')
        print('')
        print('可选参数：')
        print('  --user-id <ID> - 指定用户ID（推荐）')
        print('  --verbose, -v  - 显示详细调试信息')
        print('')
        print('示例：')
        print('  # 基本用法')
        print('  python test_checkin.py https://api.example.com MTc2NzQxMzYzM...')
        print('')
        print('  # 指定用户ID（推荐）')
        print('  python test_checkin.py https://api.example.com MTc2NzQxMzYzM... --user-id 123')
        print('')
        print('  # 启用详细调试')
        print('  python test_checkin.py https://api.example.com MTc2NzQxMzYzM... --user-id 123 --verbose')
        sys.exit(1)

    base_url = sys.argv[1]
    session_cookie = sys.argv[2]

    # 解析可选参数
    user_id = None
    verbose = False

    for i in range(3, len(sys.argv)):
        if sys.argv[i] in ['--verbose', '-v']:
            verbose = True
        elif sys.argv[i] == '--user-id' and i + 1 < len(sys.argv):
            user_id = sys.argv[i + 1]

    if verbose:
        print('[调试模式已启用]\n')

    success = test_checkin(base_url, session_cookie, user_id, verbose)
    sys.exit(0 if success else 1)
