import os
import re
import requests
import subprocess
from urllib.parse import urlparse
from ipaddress import ip_address, IPv4Address, IPv6Address
import concurrent.futures
import time
import threading
from collections import OrderedDict

# 配置参数
CONFIG_DIR = 'config'
SUBSCRIBE_FILE = os.path.join(CONFIG_DIR, 'subscribe.txt')
DEMO_FILE = os.path.join(CONFIG_DIR, 'demo.txt')
LOCAL_FILE = os.path.join(CONFIG_DIR, 'local.txt')
BLACKLIST_FILE = os.path.join(CONFIG_DIR, 'blacklist.txt')

OUTPUT_DIR = 'output'
IPV4_DIR = os.path.join(OUTPUT_DIR, 'ipv4')
IPV6_DIR = os.path.join(OUTPUT_DIR, 'ipv6')
SPEED_LOG = os.path.join(OUTPUT_DIR, 'sort.log')

SPEED_TEST_DURATION = 5
MAX_WORKERS = 20

# 全局变量
failed_domains = set()
log_lock = threading.Lock()
domain_lock = threading.Lock()
counter_lock = threading.Lock()

os.makedirs(IPV4_DIR, exist_ok=True)
os.makedirs(IPV6_DIR, exist_ok=True)


# --------------------------
# 工具函数
# --------------------------
def write_log(message):
    """线程安全的日志写入"""
    with log_lock:
        with open(SPEED_LOG, 'a', encoding='utf-8') as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")


def get_domain(url):
    """提取域名"""
    try:
        netloc = urlparse(url).netloc
        return netloc.split(':')[0] if ':' in netloc else netloc
    except:
        return None


def update_blacklist(domain):
    """更新黑名单"""
    if domain:
        with domain_lock:
            failed_domains.add(domain)


def get_ip_type(url):
    """安全获取IP类型"""
    try:
        host = urlparse(url).hostname
        if not host:
            return 'ipv4'

        # 尝试解析IP地址类型
        ip = ip_address(host)
        return 'ipv6' if isinstance(ip, IPv6Address) else 'ipv4'
    except ValueError:
        return 'ipv4'
    except Exception as e:
        print(f"⚠️ IP类型检测异常: {str(e)} ← {url}")
        return 'ipv4'


# --------------------------
# 核心逻辑
# --------------------------
def parse_demo_file():
    """解析频道模板文件"""
    print("\n🔍 解析频道模板文件...")
    alias_map = {}
    group_map = {}
    group_order = []
    channel_order = OrderedDict()
    current_group = None

    try:
        with open(DEMO_FILE, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue

                if line.endswith(',#genre#'):
                    current_group = line.split(',', 1)[0]
                    group_order.append(current_group)
                    channel_order[current_group] = []
                    print(f"  发现分组 [{current_group}]")
                elif current_group and line:
                    parts = [p.strip() for p in line.split('|')]
                    standard_name = parts[0]
                    channel_order[current_group].append(standard_name)

                    for alias in parts:
                        alias_map[alias] = standard_name
                    group_map[standard_name] = current_group

        print(f"✅ 发现 {len(group_order)} 个分组，{len(alias_map)} 个别名")
        return alias_map, group_map, group_order, channel_order

    except Exception as e:
        print(f"❌ 模板解析失败: {str(e)}")
        return {}, {}, [], OrderedDict()


def fetch_sources():
    """获取订阅源数据"""
    print("\n🔍 获取订阅源...")
    sources = []

    try:
        with open(SUBSCRIBE_FILE, 'r') as f:
            urls = [line.strip() for line in f if line.strip()]

        print(f"  发现 {len(urls)} 个订阅地址")
        for idx, url in enumerate(urls, 1):
            try:
                print(f"\n🌐 正在获取源 ({idx}/{len(urls)})：{url}")
                response = requests.get(url, timeout=15)
                content = response.text

                if '#EXTM3U' in content or url.endswith('.m3u'):
                    parsed = parse_m3u(content)
                    print(f"  解析到 {len(parsed)} 个M3U源")
                    sources.extend(parsed)
                else:
                    parsed = parse_txt(content)
                    print(f"  解析到 {len(parsed)} 个TXT源")
                    sources.extend(parsed)

            except Exception as e:
                print(f"❌ 下载失败: {str(e)}")

    except FileNotFoundError:
        print("⚠️ 订阅文件不存在")

    return sources


def parse_m3u(content):
    """解析M3U格式内容"""
    channels = []
    current = {}
    for line in content.split('\n'):
        line = line.strip()
        if line.startswith('#EXTINF'):
            match = re.search(r'tvg-name="([^"]*)"', line)
            current = {'name': match.group(1) if match else '未知频道', 'urls': []}
        elif line and not line.startswith('#'):
            if current:
                current['urls'].append(line)
                channels.append(current)
                current = {}
    return [{'name': c['name'], 'url': u} for c in channels for u in c['urls']]


def parse_txt(content):
    """解析TXT格式内容"""
    channels = []
    for line in content.split('\n'):
        line = line.strip()
        if ',' in line:
            try:
                name, urls = line.split(',', 1)
                for url in urls.split('#'):
                    clean_url = url.split('$')[0].strip()
                    if clean_url:
                        channels.append({'name': name.strip(), 'url': clean_url})
            except Exception as e:
                print(f"❌ 解析失败: {str(e)} ← {line}")
    return channels


def parse_local():
    """解析本地源文件"""
    print("\n🔍 解析本地源...")
    sources = []
    try:
        with open(LOCAL_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if ',' in line:
                    try:
                        name, urls = line.split(',', 1)
                        for url in urls.split('#'):
                            parts = url.split('$', 1)
                            source = {
                                'name': name.strip(),
                                'url': parts[0].strip(),
                                'whitelist': len(parts) > 1
                            }
                            sources.append(source)
                    except Exception as e:
                        print(f"❌ 解析失败: {str(e)} ← {line}")
        print(f"✅ 找到 {len(sources)} 个本地源")
    except FileNotFoundError:
        print("⚠️ 本地源文件不存在")
    return sources


def read_blacklist():
    """读取黑名单列表"""
    try:
        with open(BLACKLIST_FILE, 'r') as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []


def filter_sources(sources, blacklist):
    """过滤黑名单源"""
    print("\n🔍 过滤黑名单...")
    filtered = []
    blacklist_lower = [kw.lower() for kw in blacklist]

    for s in sources:
        # URL格式校验
        if not urlparse(s['url']).scheme:
            print(f"🚫 无效URL格式: {s['url']}")
            continue

        if s.get('whitelist', False):
            filtered.append(s)
            continue

        if any(kw in s['url'].lower() for kw in blacklist_lower):
            print(f"🚫 拦截黑名单: {s['url']}")
            continue

        filtered.append(s)

    print(f"✅ 保留 {len(filtered)}/{len(sources)} 个源")
    return filtered


def test_rtmp(url):
    """RTMP推流检测"""
    try:
        result = subprocess.run(
            ['ffmpeg', '-i', url, '-t', '1', '-v', 'error', '-f', 'null', '-'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10
        )
        if result.returncode == 0:
            write_log(f"RTMP检测成功: {url}")
            return 100
        write_log(f"RTMP检测失败: {url} | {result.stderr.decode()[:100]}")
        return 0
    except Exception as e:
        write_log(f"RTMP检测异常: {url} | {str(e)}")
        return 0


def test_speed(url):
    """增强版测速函数"""
    try:
        start_time = time.time()

        # RTMP协议处理
        if url.startswith(('rtmp://', 'rtmps://')):
            return test_rtmp(url)

        # HTTP协议处理
        if not url.startswith(('http://', 'https://')):
            write_log(f"⚠️ 跳过非常规协议: {url}")
            return 0

        with requests.Session() as session:
            response = session.get(url,
                                   stream=True,
                                   timeout=(3.05, 5),
                                   allow_redirects=True,
                                   verify=False,
                                   headers={'User-Agent': 'Mozilla/5.0'})

            total_bytes = 0
            data_start = time.time()
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    total_bytes += len(chunk)
                if (time.time() - data_start) >= SPEED_TEST_DURATION:
                    break

            duration = max(time.time() - data_start, 0.001)
            speed = (total_bytes / 1024) / duration
            log_msg = (f"✅ 测速成功: {url}\n"
                       f"   速度: {speed:.2f}KB/s | 数据量: {total_bytes / 1024:.1f}KB | "
                       f"总耗时: {time.time() - start_time:.2f}s")
            write_log(log_msg)
            return speed

    except Exception as e:
        domain = get_domain(url)
        update_blacklist(domain)
        log_msg = (f"❌ 测速失败: {url}\n"
                   f"   错误: {str(e)} | 域名: {domain}")
        write_log(log_msg)
        return 0


def process_sources(sources):
    """处理所有源并进行测速"""
    total = len(sources)
    print(f"\n🔍 开始检测 {total} 个源")
    processed = []
    processed_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(
            lambda s: (s['name'], s['url'], test_speed(s['url']), get_ip_type(s['url'])), s) for s in sources}

        for future in concurrent.futures.as_completed(futures):
            try:
                name, url, speed, ip_type = future.result()
                with counter_lock:
                    processed_count += 1
                    progress = f"[{processed_count}/{total}]"

                speed_str = f"{speed:>7.2f}KB/s".rjust(12)
                print(f"{progress} 📊 频道: {name[:15]:<5}|速度:{speed_str} |{url} ,类型: {ip_type.upper()}  ")
                processed.append((name, url, speed, ip_type))
            except Exception as e:
                print(f"⚠️ 处理异常: {str(e)}")

                # # 保存黑名单更新
                # if failed_domains:
                #     existing = set()
                #     if os.path.exists(BLACKLIST_FILE):
                #         with open(BLACKLIST_FILE, 'r') as f:
                #             existing = set(line.strip() for line in f)
            
                #     new_domains = failed_domains - existing
                #     if new_domains:
                #         with open(BLACKLIST_FILE, 'a') as f:
                #             for domain in new_domains:
                #                 f.write(f"{domain}\n")
                #         print(f"🆕 新增 {len(new_domains)} 个域名到黑名单")

    print("\n✅ 全部源检测完成")
    return processed


def organize_channels(processed, alias_map, group_map):
    """整理频道数据"""
    print("\n📚 整理频道数据...")
    organized = {'ipv4': OrderedDict(), 'ipv6': OrderedDict()}

    for name, url, speed, ip_type in processed:
        if ip_type not in ('ipv4', 'ipv6'):
            print(f"⚠️ 异常IP类型: {ip_type}，使用ipv4代替 ← {url}")
            ip_type = 'ipv4'

        std_name = alias_map.get(name, name)
        group = group_map.get(std_name, '其他')

        if group not in organized[ip_type]:
            organized[ip_type][group] = OrderedDict()
        if std_name not in organized[ip_type][group]:
            organized[ip_type][group][std_name] = []

        organized[ip_type][group][std_name].append((url, speed))

    return organized


def finalize_output(organized, group_order, channel_order):
    """生成输出文件"""
    print("\n📂 生成结果文件...")
    for ip_type in ['ipv4', 'ipv6']:
        txt_lines = []
        m3u_lines = [
            '#EXTM3U x-tvg-url="https://gh.catmak.name/https://raw.githubusercontent.com/Guovin/iptv-api/refs/heads/master/output/epg/epg.gz"',  # 添加EPG地址
        ]

        # 按模板顺序处理分组
        for group in group_order:
            if group not in organized[ip_type]:
                continue

            txt_lines.append(f"{group},#genre#")
            #m3u_lines.append(f'#EXTINF:-1 group-title="{group}",{group}\n#genre#')

            # 处理模板频道
            for channel in channel_order[group]:
                if channel not in organized[ip_type][group]:
                    continue

                urls = sorted(organized[ip_type][group][channel], key=lambda x: x[1], reverse=True)
                selected = [u[0] for u in urls[:20]]

                if selected:
                   # txt_lines.append(f"{channel},{'#'.join(selected)}")
                    for url in selected:
                        txt_lines.append(f"{channel},{url}")
                        m3u_lines.append(f'#EXTINF:-1 tvg-name="{channel}"tvg-logo="https://gh.catmak.name/https://raw.githubusercontent.com/fanmingming/live/main/tv/{channel}.png" group-title="{group}",{channel}\n{url}')

            # 处理额外频道
            extra = sorted(
                [c for c in organized[ip_type][group] if c not in channel_order[group]],
                key=lambda x: x.lower()
            )
            for channel in extra:
                urls = sorted(organized[ip_type][group][channel], key=lambda x: x[1], reverse=True)
                selected = [u[0] for u in urls[:10]]
                if selected:
                    txt_lines.append(f"{channel},{'#'.join(selected)}")
                    for url in selected:
                        m3u_lines.append(f'#EXTINF:-1 tvg-name="{channel}" group-title="{group}",{channel}\n{url}')

        # 处理其他分组
        if '其他' in organized[ip_type]:
            txt_lines.append("其他,#genre#")
            m3u_lines.append('#EXTINF:-1 group-title="其他",其他\n#genre#')
            for channel in sorted(organized[ip_type]['其他'].keys(), key=lambda x: x.lower()):
                urls = sorted(organized[ip_type]['其他'][channel], key=lambda x: x[1], reverse=True)
                selected = [u[0] for u in urls[:10]]
                if selected:
                    txt_lines.append(f"{channel},{'#'.join(selected)}")
                    for url in selected:
                        m3u_lines.append(f'#EXTINF:-1 tvg-name="{channel}" group-title="其他",{channel}\n{url}')

        # 写入文件
        dir_path = IPV4_DIR if ip_type == 'ipv4' else IPV6_DIR
        with open(os.path.join(dir_path, 'result.txt'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(txt_lines))
        with open(os.path.join(dir_path, 'result.m3u'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(m3u_lines))

        print(f"  已生成 {ip_type.upper()} 文件 → {dir_path}")


if __name__ == '__main__':
    print("\n" + "=" * 50)
    print("🎬 IPTV直播源处理脚本（增强版）")
    print("=" * 50)

    # 初始化日志文件
    with open(SPEED_LOG, 'w') as f:
        f.write(f"测速日志 {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    # 初始化数据
    alias_map, group_map, group_order, channel_order = parse_demo_file()
    sources = fetch_sources() + parse_local()
    blacklist = read_blacklist()

    # 处理流程
    filtered = filter_sources(sources, blacklist)
    processed = process_sources(filtered)
    organized = organize_channels(processed, alias_map, group_map)
    finalize_output(organized, group_order, channel_order)

    print("\n" + "=" * 50)
    print("🎉 处理完成！结果文件已保存至 output 目录")
    print("=" * 50)
