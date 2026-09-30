#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Linux 离线包下载工具（运行于有外网的 Windows / Linux 本机）
- 纯 Python 3 标准库实现，无第三方依赖
- 直接解析镜像源仓库元数据（RPM: repodata/primary.xml；DEB: Packages.gz）
- 支持依赖自动解析、多线程断点续传下载、生成本地仓库索引
用法: python server.py [--host 127.0.0.1] [--port 8080] [--no-browser]
"""

import argparse
import bz2
import configparser
import functools
import gzip
import hashlib
import io
import json
import lzma
import os
import queue
import re
import string
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape as xml_escape

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, 'cache')
DEFAULT_DL_DIR = os.path.join(BASE_DIR, 'offline-packages')
INDEX_HTML = os.path.join(BASE_DIR, 'index.html')

MAX_BODY = 16 * 1024 * 1024
RESOLVE_CAP = 3000
SEARCH_CAP = 1000
HISTORY_CAP = 800
TASK_KEEP = 30
UA = 'Mozilla/5.0 (offline-pkg-fetcher/1.0)'

# ----------------------------------------------------------------------------
# 预设：发行版 / 镜像源
# ----------------------------------------------------------------------------

MIRRORS = {
    'tuna':    {'name': '清华大学 TUNA', 'base': 'https://mirrors.tuna.tsinghua.edu.cn'},
    'aliyun':  {'name': '阿里云',        'base': 'https://mirrors.aliyun.com'},
    'ustc':    {'name': '中科大 USTC',   'base': 'https://mirrors.ustc.edu.cn'},
    'official':{'name': '官方源',        'base': ''},
}

# {m} = 镜像 base, {arch} = 架构目录名(x86_64/aarch64 或 amd64/arm64)
# 扩展源模板占位符: {pgver} PGDG 大版本, {codename} Debian 系发行代号

def _epel_extra(elver):
    if elver == '7':
        return {'id': 'epel', 'name': 'EPEL 7（已停更，归档源）', 'templates': {
            'tuna':    ['EPEL|https://mirrors.aliyun.com/epel-archive/7/{arch}'],
            'aliyun':  ['EPEL|{m}/epel-archive/7/{arch}'],
            'ustc':    ['EPEL|{m}/epel-archive/7/{arch}'],
            'official':['EPEL|https://archives.fedoraproject.org/pub/archive/epel/7/{arch}'],
        }}
    return {'id': 'epel', 'name': 'EPEL %s 扩展源（企业级附加软件）' % elver, 'templates': {
        'tuna':    ['EPEL|{m}/epel/%s/Everything/{arch}' % elver],
        'aliyun':  ['EPEL|{m}/epel/%s/Everything/{arch}' % elver],
        'ustc':    ['EPEL|{m}/epel/%s/Everything/{arch}' % elver],
        'official':['EPEL|https://dl.fedoraproject.org/pub/epel/%s/Everything/{arch}' % elver],
    }}


def _pgdg_rpm_extra(elver):
    """清华/中科大不镜像 PGDG；选这两个镜像时回退到阿里云的绝对地址"""
    aliyun_abs = 'PGDG|https://mirrors.aliyun.com/postgresql/repos/yum/{pgver}/redhat/rhel-%s-{arch}' % elver
    return {'id': 'pgdg', 'name': 'PostgreSQL 官方源 PGDG（含 14/15/16/17/18）',
            'options': ['18', '17', '16', '15', '14'], 'default_option': '16',
            'templates': {
                'tuna':    [aliyun_abs],
                'ustc':    [aliyun_abs],
                'aliyun':  ['PGDG|{m}/postgresql/repos/yum/{pgver}/redhat/rhel-%s-{arch}' % elver],
                'official':['PGDG|https://download.postgresql.org/pub/repos/yum/{pgver}/redhat/rhel-%s-{arch}' % elver],
            }}


def _pgdg_deb_extra():
    return {'id': 'pgdg', 'name': 'PostgreSQL 官方源 PGDG（apt.postgresql.org）',
            'templates': {
                'official':['deb http://apt.postgresql.org/pub/repos/apt {codename}-pgdg main'],
            }}


def _el_repos(mbase, elver, name_map):
    """name_map: {'tuna': 'rocky/9', ...} → 各镜像的 BaseOS/AppStream 行"""
    out = {}
    for k, path in name_map.items():
        if k == 'official':
            out[k] = ['BaseOS|%s/BaseOS/{arch}/os' % path,
                      'AppStream|%s/AppStream/{arch}/os' % path]
        else:
            out[k] = ['BaseOS|{m}/%s/BaseOS/{arch}/os' % path,
                      'AppStream|{m}/%s/AppStream/{arch}/os' % path]
    return out


PRESETS = {
    'rpm': {
        'label': 'RHEL 系 (yum/dnf, .rpm)',
        'arches': ['x86_64', 'aarch64'],
        'hint': '每行一个仓库 baseurl（含 repodata/ 的目录），可选前缀 "名称|" 作为显示名',
        'distros': [
            {'id': 'centos7', 'name': 'CentOS 7（已停更，存档数据冻结于 2020）', 'elver': '7', 'releasever': '7', 'repos': {
                'tuna':    ['BaseOS|{m}/centos-vault/7.9.2009/os/{arch}',
                            'Updates|{m}/centos-vault/7.9.2009/updates/{arch}',
                            'Extras|{m}/centos-vault/7.9.2009/extras/{arch}'],
                'aliyun':  ['BaseOS|{m}/centos/7/os/{arch}',
                            'Updates|{m}/centos/7/updates/{arch}',
                            'Extras|{m}/centos/7/extras/{arch}'],
                'ustc':    ['BaseOS|{m}/centos-vault/7.9.2009/os/{arch}',
                            'Updates|{m}/centos-vault/7.9.2009/updates/{arch}',
                            'Extras|{m}/centos-vault/7.9.2009/extras/{arch}'],
                'official':['BaseOS|https://vault.centos.org/7.9.2009/os/{arch}',
                            'Updates|https://vault.centos.org/7.9.2009/updates/{arch}',
                            'Extras|https://vault.centos.org/7.9.2009/extras/{arch}'],
            }, 'extras': [_epel_extra('7'), _pgdg_rpm_extra('7')]},
            {'id': 'stream9', 'name': 'CentOS Stream 9', 'elver': '9', 'releasever': '9', 'repos': _el_repos(None, '9', {
                'tuna': 'centos-stream/9-stream', 'aliyun': 'centos-stream/9-stream',
                'ustc': 'centos-stream/9-stream',
                'official': 'https://mirror.centos.org/centos/9-stream',
            }), 'extras': [_epel_extra('9'), _pgdg_rpm_extra('9')]},
            {'id': 'rocky9', 'name': 'Rocky Linux 9', 'elver': '9', 'releasever': '9', 'repos': _el_repos(None, '9', {
                'tuna': 'rocky/9', 'aliyun': 'rockylinux/9', 'ustc': 'rocky/9',
                'official': 'https://download.rockylinux.org/pub/rocky/9',
            }), 'extras': [_epel_extra('9'), _pgdg_rpm_extra('9')]},
            {'id': 'rocky10', 'name': 'Rocky Linux 10', 'elver': '10', 'releasever': '10', 'repos': _el_repos(None, '10', {
                'tuna': 'rocky/10', 'aliyun': 'rockylinux/10', 'ustc': 'rocky/10',
                'official': 'https://download.rockylinux.org/pub/rocky/10',
            }), 'extras': [_epel_extra('10'), _pgdg_rpm_extra('10')]},
            {'id': 'alma9', 'name': 'AlmaLinux 9', 'elver': '9', 'releasever': '9', 'repos': _el_repos(None, '9', {
                'tuna': 'almalinux/9', 'aliyun': 'almalinux/9', 'ustc': 'almalinux/9',
                'official': 'https://repo.almalinux.org/almalinux/9',
            }), 'extras': [_epel_extra('9'), _pgdg_rpm_extra('9')]},
            {'id': 'alma10', 'name': 'AlmaLinux 10', 'elver': '10', 'releasever': '10', 'repos': _el_repos(None, '10', {
                'tuna': 'almalinux/10', 'aliyun': 'almalinux/10', 'ustc': 'almalinux/10',
                'official': 'https://repo.almalinux.org/almalinux/10',
            }), 'extras': [_epel_extra('10'), _pgdg_rpm_extra('10')]},
            {'id': 'fedora42', 'name': 'Fedora 42', 'elver': '', 'releasever': '42', 'repos': {
                'tuna':    ['Everything|{m}/fedora/releases/42/Everything/{arch}/os',
                            'Updates|{m}/fedora/updates/42/Everything/{arch}'],
                'aliyun':  ['Everything|{m}/fedora/releases/42/Everything/{arch}/os',
                            'Updates|{m}/fedora/updates/42/Everything/{arch}'],
                'ustc':    ['Everything|{m}/fedora/releases/42/Everything/{arch}/os',
                            'Updates|{m}/fedora/updates/42/Everything/{arch}'],
                'official':['Everything|https://dl.fedoraproject.org/pub/fedora/linux/releases/42/Everything/{arch}/os',
                            'Updates|https://dl.fedoraproject.org/pub/fedora/linux/updates/42/Everything/{arch}'],
            }, 'extras': []},
        ],
    },
    'deb': {
        'label': 'Debian 系 (apt, .deb)',
        'arches': ['amd64', 'arm64'],
        'hint': '每行一条 sources.list 格式: deb <镜像地址> <发行代号> <组件...>',
        'distros': [
            {'id': 'ubuntu2204', 'name': 'Ubuntu 22.04 LTS', 'codename': 'jammy', 'repos': {
                '_arm64_base': '/ubuntu-ports',
                'tuna':    ['deb {m}{p} jammy main restricted universe multiverse',
                            'deb {m}{p} jammy-updates main restricted universe multiverse',
                            'deb {m}{p} jammy-security main restricted universe multiverse'],
                'aliyun':  ['deb {m}{p} jammy main restricted universe multiverse',
                            'deb {m}{p} jammy-updates main restricted universe multiverse',
                            'deb {m}{p} jammy-security main restricted universe multiverse'],
                'ustc':    ['deb {m}{p} jammy main restricted universe multiverse',
                            'deb {m}{p} jammy-updates main restricted universe multiverse',
                            'deb {m}{p} jammy-security main restricted universe multiverse'],
                'official':['deb http://archive.ubuntu.com/ubuntu jammy main restricted universe multiverse',
                            'deb http://archive.ubuntu.com/ubuntu jammy-updates main restricted universe multiverse',
                            'deb http://security.ubuntu.com/ubuntu jammy-security main restricted universe multiverse'],
            }, 'extras': [_pgdg_deb_extra()]},
            {'id': 'ubuntu2404', 'name': 'Ubuntu 24.04 LTS', 'codename': 'noble', 'repos': {
                '_arm64_base': '/ubuntu-ports',
                'tuna':    ['deb {m}{p} noble main restricted universe multiverse',
                            'deb {m}{p} noble-updates main restricted universe multiverse',
                            'deb {m}{p} noble-security main restricted universe multiverse'],
                'aliyun':  ['deb {m}{p} noble main restricted universe multiverse',
                            'deb {m}{p} noble-updates main restricted universe multiverse',
                            'deb {m}{p} noble-security main restricted universe multiverse'],
                'ustc':    ['deb {m}{p} noble main restricted universe multiverse',
                            'deb {m}{p} noble-updates main restricted universe multiverse',
                            'deb {m}{p} noble-security main restricted universe multiverse'],
                'official':['deb http://archive.ubuntu.com/ubuntu noble main restricted universe multiverse',
                            'deb http://archive.ubuntu.com/ubuntu noble-updates main restricted universe multiverse',
                            'deb http://security.ubuntu.com/ubuntu noble-security main restricted universe multiverse'],
            }, 'extras': [_pgdg_deb_extra()]},
            {'id': 'ubuntu2504', 'name': 'Ubuntu 25.04', 'codename': 'plucky', 'repos': {
                '_arm64_base': '/ubuntu-ports',
                'tuna':    ['deb {m}{p} plucky main restricted universe multiverse',
                            'deb {m}{p} plucky-updates main restricted universe multiverse',
                            'deb {m}{p} plucky-security main restricted universe multiverse'],
                'aliyun':  ['deb {m}{p} plucky main restricted universe multiverse',
                            'deb {m}{p} plucky-updates main restricted universe multiverse',
                            'deb {m}{p} plucky-security main restricted universe multiverse'],
                'ustc':    ['deb {m}{p} plucky main restricted universe multiverse',
                            'deb {m}{p} plucky-updates main restricted universe multiverse',
                            'deb {m}{p} plucky-security main restricted universe multiverse'],
                'official':['deb http://archive.ubuntu.com/ubuntu plucky main restricted universe multiverse',
                            'deb http://archive.ubuntu.com/ubuntu plucky-updates main restricted universe multiverse',
                            'deb http://security.ubuntu.com/ubuntu plucky-security main restricted universe multiverse'],
            }, 'extras': [_pgdg_deb_extra()]},
            {'id': 'debian12', 'name': 'Debian 12 (bookworm)', 'codename': 'bookworm', 'repos': {
                'tuna':    ['deb {m}/debian bookworm main contrib non-free-firmware',
                            'deb {m}/debian bookworm-updates main contrib non-free-firmware',
                            'deb {m}/debian-security bookworm-security main contrib non-free-firmware'],
                'aliyun':  ['deb {m}/debian bookworm main contrib non-free-firmware',
                            'deb {m}/debian bookworm-updates main contrib non-free-firmware',
                            'deb {m}/debian-security bookworm-security main contrib non-free-firmware'],
                'ustc':    ['deb {m}/debian bookworm main contrib non-free-firmware',
                            'deb {m}/debian bookworm-updates main contrib non-free-firmware',
                            'deb {m}/debian-security bookworm-security main contrib non-free-firmware'],
                'official':['deb http://deb.debian.org/debian bookworm main contrib non-free-firmware',
                            'deb http://deb.debian.org/debian bookworm-updates main contrib non-free-firmware',
                            'deb http://security.debian.org/debian-security bookworm-security main contrib non-free-firmware'],
            }, 'extras': [_pgdg_deb_extra()]},
            {'id': 'debian13', 'name': 'Debian 13 (trixie)', 'codename': 'trixie', 'repos': {
                'tuna':    ['deb {m}/debian trixie main contrib non-free-firmware',
                            'deb {m}/debian trixie-updates main contrib non-free-firmware',
                            'deb {m}/debian-security trixie-security main contrib non-free-firmware'],
                'aliyun':  ['deb {m}/debian trixie main contrib non-free-firmware',
                            'deb {m}/debian trixie-updates main contrib non-free-firmware',
                            'deb {m}/debian-security trixie-security main contrib non-free-firmware'],
                'ustc':    ['deb {m}/debian trixie main contrib non-free-firmware',
                            'deb {m}/debian trixie-updates main contrib non-free-firmware',
                            'deb {m}/debian-security trixie-security main contrib non-free-firmware'],
                'official':['deb http://deb.debian.org/debian trixie main contrib non-free-firmware',
                            'deb http://deb.debian.org/debian trixie-updates main contrib non-free-firmware',
                            'deb http://security.debian.org/debian-security trixie-security main contrib non-free-firmware'],
            }, 'extras': [_pgdg_deb_extra()]},
        ],
    },
}

# ----------------------------------------------------------------------------
# 全局状态
# ----------------------------------------------------------------------------

STATE = {
    'lock': threading.RLock(),
    'family': None,
    'arch': None,
    'repos': [],
    'packages': [],
    'pkg_by_id': {},
    'tasks': {},
}


def human_size(n):
    n = float(n or 0)
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or u == 'GB':
            return ('%.1f %s' % (n, u))
        n /= 1024


def now_str():
    return time.strftime('%H:%M:%S')


def _norm_name(s):
    """包名归一化：忽略 -_.+~ 与空白，使 postgresql14 能匹配 postgresql-14"""
    return re.sub(r'[-_.+~\s]', '', (s or '').lower())


# ----------------------------------------------------------------------------
# 任务系统（加载元数据 / 下载 通用，SSE 推送）
# ----------------------------------------------------------------------------

def new_task(ttype, title):
    tid = uuid.uuid4().hex[:12]
    task = {
        'id': tid, 'type': ttype, 'title': title,
        'status': 'running', 'created': time.time(),
        'events': queue.Queue(), 'history': [], 'seq': 0, 'result': None,
        'cancel': threading.Event(),
        'progress': {'total_files': 0, 'done_files': 0, 'failed_files': 0,
                     'skipped_files': 0, 'total_bytes': 0, 'done_bytes': 0},
    }
    with STATE['lock']:
        STATE['tasks'][tid] = task
        done_ids = [t['id'] for t in STATE['tasks'].values()
                    if t['status'] != 'running']
        if len(done_ids) > TASK_KEEP:
            done_ids.sort(key=lambda i: STATE['tasks'][i]['created'])
            for i in done_ids[:len(done_ids) - TASK_KEEP]:
                STATE['tasks'].pop(i, None)
    return task


def emit(task, etype, msg='', **kw):
    task['seq'] += 1
    ev = {'seq': task['seq'], 'type': etype, 'msg': msg, 'time': now_str()}
    ev.update(kw)
    task['history'].append(ev)
    if len(task['history']) > HISTORY_CAP:
        task['history'] = task['history'][-HISTORY_CAP:]
    task['events'].put(ev)


def finish_task(task, status, msg=''):
    task['status'] = status
    emit(task, status if status in ('done', 'error', 'cancelled') else 'done', msg)


# ----------------------------------------------------------------------------
# HTTP 工具
# ----------------------------------------------------------------------------

def http_open(url, headers=None, timeout=40):
    req = urllib.request.Request(url, headers={'User-Agent': UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_bytes(url, progress_cb=None, timeout=60):
    resp = http_open(url, timeout=timeout)
    total = int(resp.headers.get('Content-Length') or 0)
    buf = io.BytesIO()
    got = 0
    while True:
        chunk = resp.read(512 * 1024)
        if not chunk:
            break
        buf.write(chunk)
        got += len(chunk)
        if progress_cb:
            progress_cb(got, total)
    return buf.getvalue()


def decompress_stream(resp, name):
    low = name.lower()
    if low.endswith('.gz'):
        return gzip.GzipFile(fileobj=resp)
    if low.endswith('.xz') or low.endswith('.lzma'):
        return lzma.LZMAFile(resp)
    if low.endswith('.zst'):
        raise RuntimeError('该仓库元数据使用 zstd 压缩，标准库不支持，请更换镜像源（清华/阿里云源通常为 gzip）')
    return resp


def validate_url(url):
    try:
        u = urllib.parse.urlparse(url)
    except Exception:
        return False
    return u.scheme in ('http', 'https') and bool(u.netloc)


# ----------------------------------------------------------------------------
# 仓库 RPM 解析（yum reporpms，如 pgdg-redhat-repo-latest.noarch.rpm）
# ----------------------------------------------------------------------------

def _rpm_header_end(data, pos):
    if data[pos:pos + 3] != b'\x8e\xad\xe8':
        raise ValueError('不是有效的 RPM 文件（头结构异常）')
    nindex = int.from_bytes(data[pos + 8:pos + 12], 'big')
    hsize = int.from_bytes(data[pos + 12:pos + 16], 'big')
    if nindex > 10000 or hsize > 64 * 1024 * 1024:
        raise ValueError('RPM 头结构异常（索引过大）')
    return pos + 16 + nindex * 16 + hsize


def _cpio_extract(data, exts):
    out = {}
    pos = 0
    n = len(data)
    while pos + 110 <= n:
        if data[pos:pos + 6] not in (b'070701', b'070702'):
            raise ValueError('RPM payload 不是 cpio 格式')
        try:
            f = [int(data[pos + 6 + i * 8: pos + 14 + i * 8], 16) for i in range(13)]
        except ValueError:
            raise ValueError('cpio 头解析失败')
        filesize, namesize = f[6], f[11]
        name_pos = pos + 110
        if name_pos + namesize > n:
            break
        name = data[name_pos:name_pos + max(namesize - 1, 0)].decode('utf-8', 'replace')
        data_pos = (name_pos + namesize + 3) & ~3
        if name == 'TRAILER!!!':
            break
        if data_pos + filesize <= n and any(name.endswith(e) for e in exts):
            out[name] = data[data_pos:data_pos + filesize]
        pos = (data_pos + filesize + 3) & ~3
    return out


def _tar_extract_files(rpm_bytes, exts):
    # Windows 10/11 自带 bsdtar 可直接解 rpm（含 zstd payload）
    import subprocess
    with tempfile.TemporaryDirectory() as td:
        rp = os.path.join(td, 'repo.rpm')
        out_dir = os.path.join(td, 'out')
        os.makedirs(out_dir, exist_ok=True)
        with open(rp, 'wb') as f:
            f.write(rpm_bytes)
        r = subprocess.run(['tar', '-xf', rp, '-C', out_dir],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            raise ValueError('该 RPM 使用 zstd 压缩且系统 tar 解包失败（%s）；'
                             '请改用 Python 3.14+ 运行本工具，或手动把 .repo 里的 baseurl 粘贴到仓库列表'
                             % r.stderr.decode('utf-8', 'replace').strip()[:120])
        out = {}
        for root, _dirs, files in os.walk(out_dir):
            for fn in files:
                if any(fn.endswith(e) for e in exts):
                    fp = os.path.join(root, fn)
                    try:
                        out[os.path.basename(fn)] = open(fp, 'rb').read()
                    except OSError:
                        pass
        return out


def rpm_extract_files(rpm_bytes, exts=('.repo',)):
    """从仓库 RPM 中提取指定后缀的文本文件（如 yum.repos.d 的 .repo）"""
    if len(rpm_bytes) < 200 or rpm_bytes[:4] != b'\xed\xab\xee\xdb':
        raise ValueError('下载的内容不是 RPM 文件（URL 应指向 .rpm，如 pgdg-redhat-repo-latest.noarch.rpm）')
    pos = _rpm_header_end(rpm_bytes, 96)      # 签名头
    pos = (pos + 7) & ~7                     # 签名区 8 字节对齐
    pos = _rpm_header_end(rpm_bytes, pos)    # 主头
    payload = rpm_bytes[pos:]
    if payload[:4] == b'\x28\xb5\x2f\xfd':   # zstd
        try:
            from compression import zstd      # Python 3.14+
            return _cpio_extract(zstd.decompress(payload), exts)
        except ImportError:
            return _tar_extract_files(rpm_bytes, exts)
    if payload[:2] == b'\x1f\x8b':
        data = gzip.decompress(payload)
    elif payload[:5] == b'\xfd7zXZ':
        data = lzma.decompress(payload)
    elif payload[:3] == b'BZh':
        data = bz2.decompress(payload)
    else:
        data = payload
    return _cpio_extract(data, exts)


def _pick_minor_dir(parent, leaf, right, fallback):
    """$releasever_minor 未知时探测父目录：优先主版本目录（rhel-9-x86_64），
    否则取最新小版本目录（rhel-9.8-x86_64），都失败则退回主版本写法"""
    if parent:
        try:
            body = http_open(parent + '/', timeout=20).read().decode('utf-8', 'replace')
            hrefs = {h.rstrip('/') for h in re.findall(r'href="([^"?]+)"', body)}
            plain = leaf + right
            if plain in hrefs:
                return parent + '/' + plain, ''
            pat = re.compile(re.escape(leaf) + r'\.(\d+)\.(\d+)$')
            cands = []
            for h in hrefs:
                m = pat.search(h)
                if m:
                    cands.append(((int(m.group(1)), int(m.group(2))), h))
            if cands:
                return parent + '/' + max(cands)[1], ''
        except Exception:
            pass
    return fallback + right, '未探测到小版本目录，已退回主版本目录写法（若加载失败请手动调整 URL）'


def _sub_repo_vars(s, arch, releasever):
    """替换 .repo 中的 yum 变量；返回 (url, warning)"""
    if not s:
        return s, ''
    parts = (releasever or '').split('.')
    major = parts[0] if parts and parts[0] else ''
    minor = parts[1] if len(parts) > 1 else ''
    warn = ''
    # 1) 架构变量先替换（major/minor 探测需要完整的目录名）
    for k in ('basearch', 'arch'):
        s = s.replace('$' + k, arch or '').replace('${%s}' % k, arch or '')
    # 2) $releasever_major / $releasever_minor（必须在普通 $releasever 之前，
    #    否则 $releasever 会破坏 $releasever_major）
    MARK = '\x00'
    if '$releasever_major' in s or '${releasever_major}' in s:
        s = s.replace('${releasever_major}', major).replace('$releasever_major', major)
        s = s.replace('${releasever_minor}', MARK).replace('$releasever_minor', MARK)
        if MARK in s:
            if minor:
                s = s.replace(MARK, minor)
            else:
                left, _, right = s.partition(MARK)
                base = left.rstrip('.')
                parent = base.rsplit('/', 1)[0] if '/' in base else ''
                leaf = base.rsplit('/', 1)[-1]
                s, warn = _pick_minor_dir(parent, leaf, right, base)
    # 3) 普通 $releasever
    s = s.replace('${releasever}', releasever or '').replace('$releasever', releasever or '')
    return s, warn


def _check_repo_url(url):
    """校验仓库 baseurl 的 repodata 是否可访问，返回错误信息（空串=正常）"""
    try:
        r = http_open(url.rstrip('/') + '/repodata/repomd.xml', timeout=20)
        r.read(64)
        r.close()
        return ''
    except Exception as e:
        return 'repodata 无法访问（%s）' % str(e)[:80]


def parse_repo_rpm(rpm_bytes, arch, releasever):
    """解析仓库 RPM → [{name, url}]，含 mirrorlist/metalink 解析与可用性校验"""
    files = rpm_extract_files(rpm_bytes, ('.repo',))
    if not files:
        raise ValueError('RPM 内未找到 .repo 文件')
    repos, skipped, warnings = [], [], []
    for fname, content in files.items():
        cp = configparser.ConfigParser(strict=False, interpolation=None,
                                        delimiters=('=',))
        try:
            cp.read_string(content.decode('utf-8', 'replace'))
        except configparser.Error as e:
            warnings.append('%s 解析失败: %s' % (fname, e))
            continue
        for sec in cp.sections():
            get = lambda k: (cp.get(sec, k, fallback='') or '').strip()
            if get('enabled') == '0':
                skipped.append(sec)
                continue
            url, warn = '', ''
            baseurl = get('baseurl')
            if baseurl:
                url = baseurl.splitlines()[0].strip()
            if not url:
                ml, _ = _sub_repo_vars(get('mirrorlist'), arch, releasever)
                if ml and validate_url(ml):
                    try:
                        for ln in http_open(ml, timeout=20).read().decode('utf-8', 'replace').splitlines():
                            ln = ln.strip()
                            if ln.startswith(('http://', 'https://')):
                                url = ln
                                break
                    except Exception:
                        pass
            if not url:
                ml, _ = _sub_repo_vars(get('metalink'), arch, releasever)
                if ml and validate_url(ml):
                    try:
                        body = http_open(ml, timeout=20).read().decode('utf-8', 'replace')
                        m = re.search(r'<url[^>]*>(https?://[^<]+)</url>', body)
                        if m:
                            url = m.group(1)
                    except Exception:
                        pass
            if not url:
                warnings.append('仓库 [%s] 无 baseurl/mirrorlist，已跳过' % sec)
                continue
            url, warn = _sub_repo_vars(url, arch, releasever)
            url = url.strip()
            if warn:
                warnings.append('仓库 [%s] %s' % (sec, warn))
            if not validate_url(url):
                warnings.append('仓库 [%s] 地址无效: %s' % (sec, url[:100]))
                continue
            if '$' in url:
                warnings.append('仓库 [%s] 地址中仍有未识别变量: %s' % (sec, url[:100]))
                continue
            err = _check_repo_url(url)
            if err:
                warnings.append('仓库 [%s] %s：%s' % (sec, err, url[:100]))
            name = re.sub(r'[^A-Za-z0-9._-]', '-', sec)[:60] or 'repo'
            repos.append({'name': name, 'url': url, 'file': fname})
    return repos, skipped, warnings


# ----------------------------------------------------------------------------
# 版本比较（尽力而为：rpm 与 deb 通用比较器）
# ----------------------------------------------------------------------------

def _ver_parts(s):
    return re.findall(r'\d+|[a-zA-Z]+|~|[^a-zA-Z\d~]+', s or '')


def _rpm_vercmp(a, b):
    pa, pb = _ver_parts(a), _ver_parts(b)
    for i in range(max(len(pa), len(pb))):
        sa = pa[i] if i < len(pa) else ''
        sb = pb[i] if i < len(pb) else ''
        if sa == sb:
            continue
        if sa == '~':
            return -1
        if sb == '~':
            return 1
        if not sa:
            return -1
        if not sb:
            return 1
        da, db = sa.isdigit(), sb.isdigit()
        if da and not db:
            return 1
        if db and not da:
            return -1
        if da and db:
            na, nb = int(sa), int(sb)
            if na != nb:
                return 1 if na > nb else -1
        else:
            if sa != sb:
                return 1 if sa > sb else -1
    return 0


def evr_split_deb(v):
    epoch = 0
    if ':' in v:
        e, _, v = v.partition(':')
        if e.isdigit():
            epoch = int(e)
    up, _, rev = v.rpartition('-')
    if not up:
        up, rev = v, ''
    return epoch, up, rev


def evr_cmp(a, b):
    """a/b: (epoch, ver, rel) 统一比较；deb 把 version 拆成 epoch/upstream/revision"""
    ea, eb = int(a[0] or 0), int(b[0] or 0)
    if ea != eb:
        return 1 if ea > eb else -1
    c = _rpm_vercmp(a[1], b[1])
    if c:
        return c
    return _rpm_vercmp(a[2] or '', b[2] or '')


def deb_ver_cmp(a, b):
    ea, ua, ra = evr_split_deb(a)
    eb, ub, rb = evr_split_deb(b)
    return evr_cmp((ea, ua, ra), (eb, ub, rb))


_RPM_FLAG_OK = {
    'GT': lambda c: c > 0, 'GE': lambda c: c >= 0, 'EQ': lambda c: c == 0,
    'LE': lambda c: c <= 0, 'LT': lambda c: c < 0,
}

_DEB_OP_OK = {
    '>': lambda c: c > 0, '>>': lambda c: c > 0, '>=': lambda c: c >= 0,
    '=': lambda c: c == 0, '<=': lambda c: c <= 0, '<': lambda c: c < 0, '<<': lambda c: c < 0,
}


def rpm_req_satisfied(cand_evr, req):
    flags = req.get('flags')
    if not flags:
        return True
    c = evr_cmp(cand_evr, (req.get('epoch') or 0, req.get('ver') or '', req.get('rel') or ''))
    fl = set(flags.replace(' ', '').split('|')) if '|' in flags else {flags}
    for f in fl:
        f = f.strip()
        if f in _RPM_FLAG_OK and _RPM_FLAG_OK[f](c):
            return True
    return False


def deb_req_satisfied(cand_ver, op, ver):
    if not op or not ver:
        return True
    c = deb_ver_cmp(cand_ver, ver)
    fn = _DEB_OP_OK.get(op)
    return bool(fn and fn(c))


# ----------------------------------------------------------------------------
# RPM 仓库解析
# ----------------------------------------------------------------------------

NS_C = '{http://linux.duke.edu/metadata/common}'
NS_R = '{http://linux.duke.edu/metadata/rpm}'


def _find(el, tag):
    for ch in el:
        if ch.tag == tag:
            return ch
    return None


def _parse_rpm_entries(parent):
    out = []
    if parent is None:
        return out
    for e in parent:
        if not e.tag.endswith('entry'):
            continue
        name = e.get('name') or ''
        if not name:
            continue
        out.append({
            'name': name,
            'flags': e.get('flags') or '',
            'epoch': e.get('epoch') or '0',
            'ver': e.get('ver') or '',
            'rel': e.get('rel') or '',
        })
    return out


def parse_rpm_repo(repo, task, arch):
    base = repo['url'].rstrip('/') + '/'
    emit(task, 'log', '[%s] 获取 repomd.xml ...' % repo['name'])
    repomd = fetch_bytes(base + 'repodata/repomd.xml')
    root = ET.fromstring(repomd)
    primary_href = None
    for data in root:
        if data.tag.endswith('data') and data.get('type') == 'primary':
            loc = _find(data, '{http://linux.duke.edu/metadata/repo}location')
            if loc is None:
                for ch in data:
                    if ch.tag.endswith('location'):
                        loc = ch
                        break
            if loc is not None:
                primary_href = loc.get('href')
                break
    if not primary_href:
        raise RuntimeError('repomd.xml 中未找到 primary 元数据')

    url = urllib.parse.urljoin(base, primary_href)
    emit(task, 'log', '[%s] 下载并解析 primary 元数据: %s' % (repo['name'], primary_href))
    resp = http_open(url)
    stream = decompress_stream(resp, primary_href)
    pkgs = []
    context = ET.iterparse(stream, events=('end',))
    for _, el in context:
        if el.tag == NS_C + 'package':
            p = _parse_rpm_package(el, repo['id'], base)
            el.clear()
            if p and (p['arch'] == arch or p['arch'] == 'noarch'):
                pkgs.append(p)
    emit(task, 'log', '[%s] 解析完成，共 %d 个包' % (repo['name'], len(pkgs)))
    return pkgs


def _parse_rpm_package(el, repo_id, baseurl):
    name_el = _find(el, NS_C + 'name')
    arch_el = _find(el, NS_C + 'arch')
    ver_el = _find(el, NS_C + 'version')
    if name_el is None or arch_el is None or ver_el is None:
        return None
    ck = _find(el, NS_C + 'checksum')
    loc = _find(el, NS_C + 'location')
    size_el = _find(el, NS_C + 'size')
    summary_el = _find(el, NS_C + 'summary')
    desc_el = _find(el, NS_C + 'description')
    fmt = _find(el, NS_C + 'format')
    provides, requires = [], []
    if fmt is not None:
        provides = _parse_rpm_entries(_find(fmt, NS_R + 'provides'))
        requires = _parse_rpm_entries(_find(fmt, NS_R + 'requires'))
    requires = [r for r in requires
                if not r['name'].startswith('rpmlib(') and not r['name'].startswith('rtld(')]
    epoch = ver_el.get('epoch') or '0'
    ver = ver_el.get('ver') or ''
    rel = ver_el.get('rel') or ''
    evr = ('%s:%s-%s' % (epoch, ver, rel)) if epoch != '0' else ('%s-%s' % (ver, rel))
    href = loc.get('href') if loc is not None else ''
    name = name_el.text or ''
    return {
        'id': '%s|%s|%s|%s' % (repo_id, name, evr, arch_el.text or ''),
        'repo': repo_id, 'name': name, 'arch': arch_el.text or '',
        'epoch': epoch, 'ver': ver, 'rel': rel, 'evr': evr,
        'sha256': (ck.text or '').strip() if ck is not None else '',
        'size': int(size_el.get('package') or 0) if size_el is not None else 0,
        'summary': (summary_el.text or '').strip() if summary_el is not None else '',
        'description': (desc_el.text or '').strip() if desc_el is not None else '',
        'href': href,
        'url': urllib.parse.urljoin(baseurl, href),
        'basename': os.path.basename(href),
        'requires': requires, 'provides': provides,
        'family': 'rpm',
    }


# ----------------------------------------------------------------------------
# DEB 仓库解析
# ----------------------------------------------------------------------------

def _parse_deb_paragraphs(text, repo_id, base, arch):
    pkgs = []
    for para in re.split(r'\n\s*\n', text):
        if not para.strip():
            continue
        fields = {}
        key = None
        for line in para.split('\n'):
            if line[:1] in (' ', '\t') and key:
                if key == 'Description' and '\n' not in fields[key]:
                    fields[key] += '\n' + line.strip()
                continue
            k, _, v = line.partition(':')
            key = k.strip()
            fields[key] = v.strip()
        name = fields.get('Package')
        ver = fields.get('Version')
        parch = fields.get('Architecture')
        fname = fields.get('Filename')
        if not (name and ver and parch and fname):
            continue
        if parch not in (arch, 'all'):
            continue
        depends = parse_deb_depends(fields.get('Depends', ''))
        provides = []
        for prov in fields.get('Provides', '').split(','):
            prov = prov.strip()
            if prov:
                provides.append({'name': re.split(r'[\s(]', prov)[0]})
        pkgs.append({
            'id': '%s|%s|%s|%s' % (repo_id, name, ver, parch),
            'repo': repo_id, 'name': name, 'arch': parch,
            'epoch': str(evr_split_deb(ver)[0]),
            'ver': evr_split_deb(ver)[1], 'rel': evr_split_deb(ver)[2],
            'evr': ver, 'deb_version': ver,
            'sha256': fields.get('SHA256', ''),
            'size': int(fields.get('Size') or 0),
            'summary': (fields.get('Description') or '').split('\n')[0][:200],
            'description': fields.get('Description', ''),
            'href': fname,
            'url': base.rstrip('/') + '/' + fname,
            'basename': os.path.basename(fname),
            'requires': depends, 'provides': provides,
            'family': 'deb',
        })
    return pkgs


def parse_deb_depends(s):
    groups = []
    for part in (s or '').split(','):
        part = part.strip()
        if not part:
            continue
        alts = []
        for alt in part.split('|'):
            m = re.match(r'\s*([^\s(]+)\s*(?:\(\s*(>=|<=|=|>>|<<|>|<)\s*([^)]*?)\s*\))?', alt)
            if m:
                alts.append({'name': m.group(1), 'op': m.group(2) or '',
                             'ver': (m.group(3) or '').strip()})
        if alts:
            groups.append(alts)
    return groups


def parse_deb_repo(repo, task, arch):
    base = repo['url'].rstrip('/')
    dist = repo['dist']
    pkgs = []
    for comp in repo['comps']:
        rel = 'dists/%s/%s/binary-%s/Packages' % (dist, comp, arch)
        data = None
        used = ''
        for ext in ('.gz', '.xz', ''):
            url = '%s/%s%s' % (base, rel, ext)
            try:
                emit(task, 'log', '[%s] 下载 %s ...' % (repo['name'], rel + ext))
                raw = fetch_bytes(url)
                if ext == '.gz':
                    data = gzip.decompress(raw)
                elif ext == '.xz':
                    data = lzma.decompress(raw)
                else:
                    data = raw
                used = rel + ext
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue
                raise
        if data is None:
            emit(task, 'log', '[%s] 警告: 未找到 %s（跳过该组件）' % (repo['name'], rel))
            continue
        sub = _parse_deb_paragraphs(data.decode('utf-8', 'replace'), repo['id'], base, arch)
        emit(task, 'log', '[%s] %s 解析完成: %d 个包' % (repo['name'], used, len(sub)))
        pkgs.extend(sub)
    return pkgs


# ----------------------------------------------------------------------------
# 元数据加载（含磁盘缓存）
# ----------------------------------------------------------------------------

def _cache_key(repo):
    raw = json.dumps(repo, sort_keys=True).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:24]


def _cache_stats():
    total, files = 0, 0
    if os.path.isdir(CACHE_DIR):
        for name in os.listdir(CACHE_DIR):
            fp = os.path.join(CACHE_DIR, name)
            try:
                if os.path.isfile(fp):
                    total += os.path.getsize(fp)
                    files += 1
            except OSError:
                pass
    return total, files


def _cache_clear():
    freed, removed = 0, 0
    if os.path.isdir(CACHE_DIR):
        for name in os.listdir(CACHE_DIR):
            fp = os.path.join(CACHE_DIR, name)
            try:
                if os.path.isfile(fp):
                    freed += os.path.getsize(fp)
                    os.remove(fp)
                    removed += 1
            except OSError:
                pass
    return freed, removed


def load_repos_task(task, family, arch, repos, force=False):
    os.makedirs(CACHE_DIR, exist_ok=True)
    all_pkgs = []
    loaded_repos = []
    failed = []
    try:
        for i, repo in enumerate(repos):
            repo['id'] = 'r%d' % i
            try:
                ck_src = {k: repo[k] for k in ('family', 'url', 'dist', 'comps') if k in repo}
                ck_src['arch'] = arch
                ck = _cache_key(ck_src)
                cf = os.path.join(CACHE_DIR, ck + '.json')
                pkgs = None
                if not force and os.path.isfile(cf):
                    try:
                        with open(cf, 'r', encoding='utf-8') as f:
                            cached = json.load(f)
                        if cached.get('arch') == arch:
                            pkgs = cached['packages']
                            for p in pkgs:
                                p['repo'] = repo['id']
                            cat = cached.get('cached_at')
                            cat_str = ('，缓存于 ' + time.strftime('%m-%d %H:%M', time.localtime(cat))) if cat else ''
                            emit(task, 'log', '[%s] 使用本地缓存（%d 个包%s；如需最新数据请勾选"强制刷新"）'
                                 % (repo['name'], len(pkgs), cat_str))
                    except Exception:
                        pkgs = None
                if pkgs is None:
                    if family == 'rpm':
                        pkgs = parse_rpm_repo(repo, task, arch)
                    else:
                        pkgs = parse_deb_repo(repo, task, arch)
                    try:
                        with open(cf, 'w', encoding='utf-8') as f:
                            json.dump({'arch': arch, 'cached_at': time.time(), 'packages': pkgs}, f)
                    except Exception as e:
                        emit(task, 'log', '缓存写入失败（忽略）: %s' % e)
                all_pkgs.extend(pkgs)
                loaded_repos.append({'id': repo['id'], 'name': repo['name'],
                                     'url': repo['url'], 'count': len(pkgs),
                                     'dist': repo.get('dist', ''), 'comps': repo.get('comps', [])})
            except Exception as e:
                failed.append((repo['name'], str(e)))
                emit(task, 'log', '[%s] 加载失败，已跳过（不影响其余仓库）：%s' % (repo['name'], e))
            emit(task, 'progress', '', total_files=len(repos), done_files=i + 1)
        if not loaded_repos:
            raise RuntimeError('；'.join('[%s] %s' % (n, e) for n, e in failed) or '没有可用仓库')
        pkg_by_id = {p['id']: p for p in all_pkgs}
        with STATE['lock']:
            STATE['family'] = family
            STATE['arch'] = arch
            STATE['repos'] = loaded_repos
            STATE['packages'] = all_pkgs
            STATE['pkg_by_id'] = pkg_by_id
        msg = ('元数据加载完成：成功 %d/%d 个仓库，共 %d 个包（架构 %s）'
               % (len(loaded_repos), len(repos), len(all_pkgs), arch))
        if failed:
            msg += '；失败已跳过：%s' % '、'.join(n for n, _e in failed)
        if family == 'rpm' and loaded_repos:
            emit(task, 'log', '后台预热 filelists 元数据（用于文件级依赖解析，首次解析更快） ...')
            threading.Thread(target=_prewarm_filelists,
                             args=([r['url'] for r in loaded_repos],), daemon=True).start()
        finish_task(task, 'done', msg)
    except Exception as e:
        finish_task(task, 'error', '元数据加载失败: %s' % e)


def _prewarm_filelists(repo_bases):
    for base in repo_bases:
        try:
            url = _filelists_url(base)
            if url:
                src = _filelists_get_stream(url)
                src.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# 依赖解析
# ----------------------------------------------------------------------------

# filelists 扫描缓存：{repo_base_url: {'url': filelists_url, 'found': {path: name}, 'missed': set()}}
_FILELISTS_CACHE = {}
_FILELISTS_LOCK = threading.Lock()


def _filelists_url(repo_base):
    """从 repomd.xml 解析 filelists 元数据地址（优先 gzip）"""
    with _FILELISTS_LOCK:
        cache = _FILELISTS_CACHE.setdefault(repo_base, {'url': None, 'found': {}, 'missed': set()})
    if cache['url'] is not None:
        return cache['url']
    try:
        repomd = fetch_bytes(repo_base.rstrip('/') + '/repodata/repomd.xml', timeout=40)
        root = ET.fromstring(repomd)
        cands = {}
        for data in root:
            if not data.tag.endswith('data'):
                continue
            dtype = data.get('type') or ''
            if dtype.split('_', 1)[0] != 'filelists':
                continue
            loc = _find(data, NS_C + 'location')
            if loc is None:
                for ch in data:
                    if ch.tag.endswith('location'):
                        loc = ch
                        break
            if loc is not None and loc.get('href'):
                cands[dtype] = loc.get('href')
        href = cands.get('filelists')
        if href is None:
            for k in ('filelists_zck',):
                if k in cands:
                    href = cands[k]
        cache['url'] = urllib.parse.urljoin(repo_base.rstrip('/') + '/', href) if href else ''
    except Exception:
        cache['url'] = ''
    return cache['url']


def _filelists_cache_file(url):
    ck = hashlib.sha256(url.encode('utf-8')).hexdigest()[:24]
    return os.path.join(CACHE_DIR, ck + '.filelists')


def _filelists_get_stream(url, task=None, repo_name=''):
    """filelists 元数据本地磁盘缓存：首次下载，之后直接读本地文件。
    临时文件 + 原子重命名，并发下载也不会产生损坏文件。"""
    fp = _filelists_cache_file(url)
    if not os.path.isfile(fp):
        os.makedirs(CACHE_DIR, exist_ok=True)
        if task:
            emit(task, 'log', '[%s] 首次解析文件依赖，下载 filelists 元数据（此后走本地缓存） ...' % repo_name)
        resp = http_open(url, timeout=300)
        tmp = '%s.%d.part' % (fp, threading.get_ident())
        with open(tmp, 'wb') as out:
            while True:
                if task and task['cancel'].is_set():
                    out.close()
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    raise RuntimeError('已取消')
                chunk = resp.read(512 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        os.replace(tmp, fp)
    return open(fp, 'rb')


def scan_filelists_paths(wanted, task=None):
    """在已加载的 RPM 仓库 filelists 元数据中查找文件路径的提供者包名。
    返回 {path: 包名}；未命中的路径不在结果中。结果按仓库缓存，重复调用很快。"""
    found = {}
    if not wanted:
        return found
    with STATE['lock']:
        repos = [{'id': r['id'], 'name': r.get('name') or r['id'], 'base': r['url']}
                 for r in STATE['repos']]
    repos = [r for r in repos if r['base'].startswith(('http://', 'https://'))]
    remaining = set(wanted)
    for repo in repos:
        if not remaining:
            break
        if task and task['cancel'].is_set():
            break
        with _FILELISTS_LOCK:
            cache = _FILELISTS_CACHE.setdefault(repo['base'], {'url': None, 'found': {}, 'missed': set()})
        todo = {p for p in remaining if p not in cache['found'] and p not in cache['missed']}
        for p in remaining & set(cache['found']):
            found[p] = cache['found'][p]
            remaining.discard(p)
        if not todo:
            continue
        url = _filelists_url(repo['base'])
        if not url:
            cache['missed'].update(todo)
            continue
        if task:
            emit(task, 'log', '[%s] 扫描 filelists 解析 %d 个文件依赖 ...' % (repo['name'], len(todo)))
        try:
            src = _filelists_get_stream(url, task, repo['name'])
            try:
                stream = decompress_stream(src, url)
                for _ev, pkg_el in ET.iterparse(stream, events=('end',)):
                    if not pkg_el.tag.endswith('}package'):
                        continue
                    if task and task['cancel'].is_set():
                        break
                    pname = pkg_el.get('name') or ''
                    if pname:
                        for f in pkg_el:
                            if not f.tag.endswith('}file'):
                                continue
                            path = (f.text or '').strip()
                            if path in todo and path not in cache['found']:
                                cache['found'][path] = pname
                                found[path] = pname
                                remaining.discard(path)
                    pkg_el.clear()
                    if not remaining:
                        break
            finally:
                try:
                    src.close()
                except Exception:
                    pass
        except RuntimeError:
            break
        except Exception as e:
            if task:
                emit(task, 'log', '[%s] filelists 扫描失败（跳过）: %s' % (repo['name'], str(e)[:100]))
        for p in todo - set(cache['found']):
            cache['missed'].add(p)
    return found


def build_providers(packages):
    providers = {}
    for p in packages:
        providers.setdefault(p['name'], []).append((p, (p['epoch'], p['ver'], p['rel'])))
        for pr in p['provides']:
            pe = None
            if pr.get('ver'):
                pe = (pr.get('epoch') or '0', pr.get('ver') or '', pr.get('rel') or '')
            providers.setdefault(pr['name'], []).append((p, pe))
    return providers


def _pick_best(cands, selected_ids, family):
    for p, _ in cands:
        if p['id'] in selected_ids:
            return p

    def score_key(c):
        p, pe = c
        dbg = 1 if ('debuginfo' in p['name'] or 'debugsource' in p['name']
                    or p['name'].endswith('-dbg')) else 0
        ver_key = functools.cmp_to_key(evr_cmp)((p['epoch'], p['ver'], p['rel']))
        return (ver_key, -dbg)

    return max(cands, key=score_key)[0]


def _resolve_round(pkg_ids, pkg_by_id, providers, family):
    """单轮 BFS 依赖解析。路径类依赖（含 /）未满足时记入 wanted 而非直接判缺口。
    返回 (order, gaps, wanted)；wanted = {路径: 请求该路径的包}"""
    selected = {}
    order = []
    gaps = []
    wanted = {}
    q = [pid for pid in pkg_ids if pid in pkg_by_id]
    queued = set(q)

    while q:
        if len(selected) >= RESOLVE_CAP:
            gaps.append({'by': '-', 'req': '依赖数量超过上限 %d，已截断' % RESOLVE_CAP})
            break
        pid = q.pop(0)
        if pid in selected:
            continue
        p = pkg_by_id.get(pid)
        if not p:
            continue
        selected[pid] = p
        order.append(p)

        if family == 'rpm':
            req_groups = [[dict(r)] for r in p['requires']]
        else:
            req_groups = p['requires']

        for group in req_groups:
            satisfied = False
            gap_desc = None
            by = '%s-%s' % (p['name'], p['evr'])
            for alt in group:
                rname = alt['name']
                cands = providers.get(rname)
                if not cands:
                    if '/' in rname:
                        # 文件路径依赖：先经 filelists 查找，暂不计入缺口
                        wanted.setdefault(rname, by)
                        satisfied = True
                        break
                    if family == 'rpm' and alt.get('flags'):
                        gap_desc = '%s (%s %s:%s-%s)' % (
                            rname, alt['flags'], alt.get('epoch') or 0,
                            alt.get('ver') or '', alt.get('rel') or '')
                    elif family == 'deb' and alt.get('op'):
                        gap_desc = '%s (%s %s)' % (rname, alt['op'], alt.get('ver') or '')
                    else:
                        gap_desc = rname
                    continue
                if family == 'rpm':
                    ok_cands = [(cp, pe) for cp, pe in cands
                                if rpm_req_satisfied(pe or (cp['epoch'], cp['ver'], cp['rel']), alt)]
                else:
                    ok_cands = [(cp, pe) for cp, pe in cands
                                if deb_req_satisfied(cp['deb_version'], alt.get('op', ''), alt.get('ver', ''))]
                if not ok_cands:
                    if '/' in rname:
                        wanted.setdefault(rname, by)
                        satisfied = True
                        break
                    if family == 'rpm':
                        gap_desc = '%s (需要 %s %s:%s-%s，仓库中版本不满足)' % (
                            rname, alt['flags'], alt.get('epoch') or 0,
                            alt.get('ver') or '', alt.get('rel') or '')
                    else:
                        gap_desc = '%s (需要 %s %s，仓库中版本不满足)' % (
                            rname, alt.get('op'), alt.get('ver'))
                    continue
                best = _pick_best(ok_cands, selected, family)
                if best['id'] not in selected and best['id'] not in queued:
                    q.append(best['id'])
                    queued.add(best['id'])
                satisfied = True
                break
            if not satisfied:
                if not any(g['req'] == (gap_desc or '?') for g in gaps):
                    gaps.append({'by': by, 'req': gap_desc or str(group)})
    return order, gaps, wanted


def resolve_packages(pkg_ids, task=None):
    with STATE['lock']:
        packages = list(STATE['packages'])
        pkg_by_id = dict(STATE['pkg_by_id'])
        family = STATE['family']
    providers = build_providers(packages)
    order, gaps, wanted = [], [], {}
    for _round in range(3):
        order, gaps, wanted = _resolve_round(pkg_ids, pkg_by_id, providers, family)
        if not wanted or family != 'rpm':
            return order, gaps
        if task and task['cancel'].is_set():
            return order, gaps
        # 有未满足的文件路径依赖 → 扫描各仓库 filelists 找提供者，再解析一轮
        found = scan_filelists_paths(list(wanted.keys()), task)
        if task and task['cancel'].is_set():
            return order, gaps
        injected = 0
        for path, pname in found.items():
            cands = providers.get(pname)
            if cands:
                providers[path] = cands
                injected += 1
        if not injected:
            break
        if task:
            emit(task, 'log', 'filelists 命中 %d/%d 个文件依赖，进行第 %d 轮解析 ...'
                 % (injected, len(wanted), _round + 2))
    # 扫描后仍未找到提供者的路径依赖 → 真实缺口
    for path, by in wanted.items():
        gaps.append({'by': by,
                     'req': '%s（文件提供者未在已加载仓库中找到；此类多为系统基础文件，'
                            '目标机通常已内置，可忽略）' % path})
    return order, gaps


def run_resolve_task(task, ids):
    try:
        order, gaps = resolve_packages(ids, task)
        items = [{'id': p['id'], 'name': p['name'], 'evr': p['evr'], 'arch': p['arch'],
                  'repo': p['repo'], 'size': p['size'], 'basename': p['basename']}
                 for p in order]
        task['result'] = {'count': len(items),
                          'total_size': sum(i['size'] for i in items),
                          'items': items, 'gaps': gaps}
        if task['cancel'].is_set():
            finish_task(task, 'cancelled', '解析已取消')
        else:
            finish_task(task, 'done', '解析完成: %d 个包' % len(items))
    except Exception as e:
        traceback.print_exc()
        finish_task(task, 'error', '解析失败: %s' % e)


# ----------------------------------------------------------------------------
# 下载执行
# ----------------------------------------------------------------------------

def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def download_one(task, item, dest_dir):
    name = item['basename']
    dest = os.path.join(dest_dir, name)
    url = item['url']
    expected = item.get('size') or 0

    if os.path.isfile(dest):
        have = os.path.getsize(dest)
        if expected and have == expected:
            emit(task, 'file', file=name, status='skipped',
                 msg='已存在，跳过: %s' % name)
            return 'skipped'
        start = have
    else:
        start = 0

    attempts = 3
    for attempt in range(1, attempts + 1):
        if task['cancel'].is_set():
            return 'cancelled'
        try:
            headers = {}
            mode = 'wb'
            if start and expected and start < expected:
                headers['Range'] = 'bytes=%d-' % start
                mode = 'ab'
            resp = http_open(url, headers=headers, timeout=60)
            if start and resp.status != 206:
                mode = 'wb'
                start = 0
            got = start
            last = [0.0]
            with open(dest, mode) as f:
                while True:
                    if task['cancel'].is_set():
                        return 'cancelled'
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if time.time() - last[0] > 0.4:
                        last[0] = time.time()
                        emit(task, 'file', '', file=name, status='downloading',
                             got=got, total=expected)
            if expected and got != expected:
                raise IOError('大小不符: 期望 %d 实际 %d' % (expected, got))
            if item.get('sha256'):
                if _sha256_file(dest) != item['sha256']:
                    raise IOError('SHA256 校验失败')
            emit(task, 'file', file=name, status='done',
                 msg='完成: %s (%s)' % (name, human_size(got)), got=got, total=expected)
            return 'done'
        except Exception as e:
            if '大小不符' in str(e) or 'SHA256' in str(e):
                # 文件内容损坏：删除后从头重下，避免断点续传叠加坏数据
                try:
                    if os.path.isfile(dest):
                        os.remove(dest)
                except OSError:
                    pass
            start = os.path.getsize(dest) if os.path.isfile(dest) else 0
            if attempt < attempts:
                emit(task, 'log', '下载失败，第 %d 次重试 %s: %s' % (attempt, name, e))
                time.sleep(2)
            else:
                emit(task, 'file', file=name, status='failed',
                     msg='失败: %s - %s' % (name, e))
                return 'failed'
    return 'failed'


def run_download_task(task, items, dest_dir, concurrency):
    prog = task['progress']
    prog['total_files'] = len(items)
    prog['total_bytes'] = sum(i.get('size') or 0 for i in items)
    work = queue.Queue()
    for it in items:
        work.put(it)

    def worker():
        while True:
            try:
                item = work.get_nowait()
            except queue.Empty:
                return
            if task['cancel'].is_set():
                work.put(item)
                return
            try:
                r = download_one(task, item, dest_dir)
            except Exception as e:
                # 单个文件处理异常绝不能让 worker 静默退出，否则统计对不上
                traceback.print_exc()
                emit(task, 'file', file=item.get('basename', '?'), status='failed',
                     msg='异常: %s - %s' % (item.get('basename', '?'), e))
                r = 'failed'
            with STATE['lock']:
                if r == 'done':
                    prog['done_files'] += 1
                    prog['done_bytes'] += item.get('size') or 0
                elif r == 'skipped':
                    prog['skipped_files'] += 1
                    prog['done_files'] += 1
                    prog['done_bytes'] += item.get('size') or 0
                elif r == 'failed':
                    prog['failed_files'] += 1
            work.task_done()

    threads = []
    for _ in range(min(concurrency, max(1, len(items)))):
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    if task['cancel'].is_set():
        finish_task(task, 'cancelled', '任务已取消（已下载的文件保留在目录中）')
        return

    emit(task, 'log', '开始生成本地仓库索引 ...')
    try:
        # 只把实际存在于目录中的文件写入索引，避免引用下载失败的包
        ok_items = [it for it in items
                    if os.path.isfile(os.path.join(dest_dir, it['basename']))]
        family = ok_items[0]['family'] if ok_items else 'rpm'
        if family == 'rpm':
            gen_rpm_repodata(dest_dir, ok_items)
            emit(task, 'log', '已生成 repodata/（yum/dnf 本地仓库索引）')
        else:
            gen_deb_packages_index(dest_dir, ok_items)
            emit(task, 'log', '已生成 Packages.gz（apt 本地源索引）')
    except Exception as e:
        emit(task, 'log', '仓库索引生成失败（不影响已下载的包文件）: %s' % e)
        traceback.print_exc()

    failed = prog['failed_files']
    if failed:
        finish_task(task, 'done',
                    '下载结束：成功 %d，跳过 %d，失败 %d（失败项可重试下载）'
                    % (prog['done_files'] - prog['skipped_files'], prog['skipped_files'], failed))
    else:
        finish_task(task, 'done',
                    '全部完成：下载 %d 个包，跳过已存在 %d 个，共 %s'
                    % (prog['done_files'] - prog['skipped_files'],
                       prog['skipped_files'], human_size(prog['done_bytes'])))


# ----------------------------------------------------------------------------
# 本地仓库索引生成
# ----------------------------------------------------------------------------

def gen_rpm_repodata(dest_dir, items):
    ts = int(time.time())
    primary = io.StringIO()
    primary.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    primary.write('<metadata xmlns="http://linux.duke.edu/metadata/common" '
                  'xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="%d">\n' % len(items))
    for it in items:
        fpath = os.path.join(dest_dir, it['basename'])
        fsize = os.path.getsize(fpath) if os.path.isfile(fpath) else (it.get('size') or 0)
        try:
            cksum = _sha256_file(fpath)
        except Exception:
            cksum = it.get('sha256') or ''
        primary.write('<package type="rpm">\n')
        primary.write('  <name>%s</name>\n' % xml_escape(it['name']))
        primary.write('  <arch>%s</arch>\n' % xml_escape(it['arch']))
        primary.write('  <version epoch="%s" ver="%s" rel="%s"/>\n'
                      % (xml_escape(str(it['epoch'])), xml_escape(it['ver']), xml_escape(it['rel'])))
        primary.write('  <checksum type="sha256" pkgid="YES">%s</checksum>\n' % cksum)
        primary.write('  <summary>%s</summary>\n' % xml_escape(it.get('summary') or ''))
        primary.write('  <description>%s</description>\n' % xml_escape(it.get('description') or ''))
        primary.write('  <packager></packager>\n  <url></url>\n')
        primary.write('  <time file="%d" build="%d"/>\n' % (ts, ts))
        primary.write('  <size package="%d" installed="0" archive="0"/>\n' % fsize)
        primary.write('  <location href="%s"/>\n' % xml_escape(it['basename']))
        primary.write('  <format>\n    <rpm:license>unknown</rpm:license>\n')
        for group, tag in ((it.get('provides') or [], 'provides'), (it.get('requires') or [], 'requires')):
            if not group:
                continue
            primary.write('    <rpm:%s>\n' % tag)
            for e in group:
                attrs = ' name="%s"' % xml_escape(e['name'])
                if e.get('flags'):
                    attrs += ' flags="%s"' % xml_escape(e['flags'])
                    if e.get('ver'):
                        attrs += ' epoch="%s" ver="%s"' % (xml_escape(str(e.get('epoch') or '0')), xml_escape(e['ver']))
                        if e.get('rel'):
                            attrs += ' rel="%s"' % xml_escape(e['rel'])
                primary.write('      <rpm:entry%s/>\n' % attrs)
            primary.write('    </rpm:%s>\n' % tag)
        primary.write('  </format>\n</package>\n')
    primary.write('</metadata>\n')

    repodata = os.path.join(dest_dir, 'repodata')
    os.makedirs(repodata, exist_ok=True)

    def write_gz(fname, text):
        raw = text.encode('utf-8')
        gz_path = os.path.join(repodata, fname)
        with gzip.open(gz_path, 'wb', compresslevel=6) as f:
            f.write(raw)
        return fname, raw, _sha256_file(gz_path), os.path.getsize(gz_path), hashlib.sha256(raw).hexdigest(), len(raw)

    entries = [write_gz('primary.xml.gz', primary.getvalue())]
    for fname, tag in (('filelists.xml.gz', 'filelists'), ('other.xml.gz', 'other')):
        empty = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<%s xmlns="http://linux.duke.edu/metadata/%s" packages="%d"/>\n'
                 % (tag, tag, len(items)))
        entries.append(write_gz(fname, empty))

    repomd = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<repomd xmlns="http://linux.duke.edu/metadata/repo">']
    for fname, raw, gzsum, gzsize, opensum, opensize in entries:
        dtype = fname.split('.xml')[0]
        repomd.append('  <data type="%s">' % dtype)
        repomd.append('    <location href="repodata/%s"/>' % fname)
        repomd.append('    <checksum type="sha256">%s</checksum>' % gzsum)
        repomd.append('    <timestamp>%d</timestamp>' % ts)
        repomd.append('    <size>%d</size>' % gzsize)
        repomd.append('    <open-checksum type="sha256">%s</open-checksum>' % opensum)
        repomd.append('    <open-size>%d</open-size>' % opensize)
        repomd.append('  </data>')
    repomd.append('</repomd>\n')
    with open(os.path.join(repodata, 'repomd.xml'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(repomd))


def gen_deb_packages_index(dest_dir, items):
    lines = []
    for it in items:
        fpath = os.path.join(dest_dir, it['basename'])
        try:
            cksum = _sha256_file(fpath)
            fsize = os.path.getsize(fpath)
        except Exception:
            cksum = it.get('sha256') or ''
            fsize = it.get('size') or 0
        lines.append('Package: %s' % it['name'])
        lines.append('Version: %s' % it['evr'])
        lines.append('Architecture: %s' % it['arch'])
        if it.get('requires'):
            deps = []
            for group in it['requires']:
                alts = []
                for a in group:
                    s = a['name']
                    if a.get('op') and a.get('ver'):
                        s += ' (%s %s)' % (a['op'], a['ver'])
                    alts.append(s)
                deps.append(' | '.join(alts))
            lines.append('Depends: %s' % ', '.join(deps))
        lines.append('Filename: ./%s' % it['basename'])
        lines.append('Size: %d' % fsize)
        if cksum:
            lines.append('SHA256: %s' % cksum)
        if it.get('summary'):
            lines.append('Description: %s' % it['summary'])
        lines.append('')
    text = '\n'.join(lines)
    with open(os.path.join(dest_dir, 'Packages'), 'w', encoding='utf-8') as f:
        f.write(text)
    with gzip.open(os.path.join(dest_dir, 'Packages.gz'), 'wb') as f:
        f.write(text.encode('utf-8'))


# ----------------------------------------------------------------------------
# 输入解析与校验
# ----------------------------------------------------------------------------

def parse_repo_lines(family, text):
    repos = []
    for ln, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if family == 'deb' or line.startswith('deb '):
            tokens = line.split()
            if len(tokens) < 4 or tokens[0] != 'deb':
                raise ValueError('第 %d 行格式错误，deb 源应为: deb <地址> <代号> <组件...>' % ln)
            url, dist, comps = tokens[1], tokens[2], tokens[3:]
            if not validate_url(url):
                raise ValueError('第 %d 行地址无效: %s' % (ln, url))
            repos.append({'family': 'deb', 'name': '%s %s' % (dist, ' '.join(comps)),
                          'url': url, 'dist': dist, 'comps': comps})
        else:
            name = ''
            url = line
            if '|' in line and not line.startswith('http'):
                name, _, url = line.partition('|')
            elif '|' in line:
                left, _, right = line.partition('|')
                if right.startswith('http'):
                    name, url = left.strip(), right.strip()
            url = url.strip()
            if not validate_url(url):
                raise ValueError('第 %d 行仓库地址无效: %s' % (ln, url))
            repos.append({'family': 'rpm', 'name': (name or 'repo-%d' % ln).strip(), 'url': url})
    if not repos:
        raise ValueError('请至少配置一个仓库源')
    fams = {r['family'] for r in repos}
    if len(fams) > 1:
        raise ValueError('RPM 与 DEB 源不能混用，请分次下载')
    return repos


WIN_FORBIDDEN = ('c:\\windows', 'c:\\program files', 'c:\\program files (x86)',
                 'c:\\programdata', 'c:\\users\\default', 'c:\\')
POSIX_FORBIDDEN = ('/', '/etc', '/usr', '/bin', '/sbin', '/var', '/boot', '/lib', '/lib64', '/root')


def validate_download_dir(d):
    if not d or len(d) > 400 or '\x00' in d:
        return None, '目录路径无效'
    rp = os.path.realpath(d)
    norm = os.path.normpath(rp)
    low = norm.replace('/', '\\').lower()
    low = low if len(low) > 3 else low.rstrip('\\') + '\\'
    if os.name == 'nt':
        if low in WIN_FORBIDDEN or any(low.startswith(f + '\\') for f in WIN_FORBIDDEN[:-1]):
            return None, '禁止写入系统目录: %s' % norm
    else:
        p = norm.rstrip('/') or '/'
        if p in POSIX_FORBIDDEN or any(p.startswith(f + '/') for f in POSIX_FORBIDDEN[1:]):
            return None, '禁止写入系统目录: %s' % norm
    if not os.path.isdir(rp):
        return None, '目录不存在，请先在系统中创建: %s' % norm
    if not os.access(rp, os.W_OK):
        return None, '目录不可写: %s' % norm
    return rp, None


# ----------------------------------------------------------------------------
# HTTP 服务
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'OfflinePkgFetcher/1.0'

    def log_message(self, fmt, *args):
        sys.stderr.write('[%s] %s\n' % (now_str(), fmt % args))

    # -- helpers ----------------------------------------------------------
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, ctype='text/plain; charset=utf-8', code=200):
        body = text.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length <= 0 or length > MAX_BODY:
            raise ValueError('请求体为空或过大')
        return json.loads(self.rfile.read(length).decode('utf-8'))

    def _err(self, msg, code=400):
        self._send_json({'ok': False, 'error': msg}, code)

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            if path == '/' or path == '/index.html':
                with open(INDEX_HTML, 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == '/api/presets':
                self._send_json({'ok': True, 'mirrors': MIRRORS, 'presets': PRESETS})
            elif path == '/api/status':
                cache_bytes, cache_files = _cache_stats()
                with STATE['lock']:
                    self._send_json({
                        'ok': True, 'family': STATE['family'], 'arch': STATE['arch'],
                        'repos': STATE['repos'], 'package_count': len(STATE['packages']),
                        'default_dir': DEFAULT_DL_DIR,
                        'cache_bytes': cache_bytes, 'cache_files': cache_files,
                    })
            elif path == '/api/tasks':
                with STATE['lock']:
                    tasks = sorted(STATE['tasks'].values(), key=lambda t: -t['created'])
                    self._send_json({'ok': True, 'tasks': [{
                        'id': t['id'], 'type': t['type'], 'title': t['title'],
                        'status': t['status'], 'created': t['created'],
                        'progress': t['progress'],
                        'result': t['result'] if t['type'] == 'resolve' else None}
                        for t in tasks]})
            elif path.startswith('/api/tasks/') and path.endswith('/events'):
                tid = path.split('/')[3]
                self._sse(tid)
            elif path == '/api/ls':
                self._ls(qs.get('path', [''])[0])
            else:
                self._err('not found', 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self._err(str(e), 500)
            except Exception:
                pass

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == '/api/repos/load':
                self._api_load()
            elif path == '/api/search':
                self._api_search()
            elif path == '/api/resolve':
                self._api_resolve()
            elif path == '/api/download':
                self._api_download()
            elif path == '/api/cache/clear':
                self._api_cache_clear()
            elif path == '/api/repo/import':
                self._api_repo_import()
            elif path.startswith('/api/tasks/') and path.endswith('/cancel'):
                tid = path.split('/')[3]
                with STATE['lock']:
                    t = STATE['tasks'].get(tid)
                if not t:
                    return self._err('任务不存在', 404)
                t['cancel'].set()
                self._send_json({'ok': True})
            else:
                self._err('not found', 404)
        except ValueError as e:
            self._err(str(e))
        except BrokenPipeError:
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self._err(str(e), 500)
            except Exception:
                pass

    # -- API --------------------------------------------------------------
    def _api_load(self):
        data = self._read_json()
        family = data.get('family')
        arch = data.get('arch')
        if family not in ('rpm', 'deb'):
            return self._err('family 必须是 rpm 或 deb')
        valid_arch = PRESETS[family]['arches']
        if arch not in valid_arch:
            return self._err('arch 必须是: %s' % ', '.join(valid_arch))
        repos = parse_repo_lines(family, data.get('repos_text') or '')
        if len(repos) > 12:
            return self._err('仓库数量过多（最多 12 个）')
        with STATE['lock']:
            busy = any(t['status'] == 'running' and t['type'] == 'load'
                       for t in STATE['tasks'].values())
        if busy:
            return self._err('已有元数据加载任务进行中')
        task = new_task('load', '加载元数据')
        th = threading.Thread(target=load_repos_task,
                              args=(task, family, arch, repos, bool(data.get('force'))),
                              daemon=True)
        th.start()
        self._send_json({'ok': True, 'task_id': task['id']})

    def _api_cache_clear(self):
        with STATE['lock']:
            busy = any(t['status'] == 'running' for t in STATE['tasks'].values())
        if busy:
            return self._err('有任务正在运行，请等待完成或先取消')
        freed, removed = _cache_clear()
        with STATE['lock']:
            STATE['packages'] = []
            STATE['repos'] = []
            STATE['family'] = None
            STATE['arch'] = None
        cache_bytes, cache_files = _cache_stats()
        self._send_json({'ok': True, 'freed': freed, 'removed': removed,
                         'cache_bytes': cache_bytes, 'cache_files': cache_files})

    def _api_repo_import(self):
        data = self._read_json()
        url = (data.get('url') or '').strip()
        arch = (data.get('arch') or 'x86_64').strip()
        releasever = str(data.get('releasever') or '').strip()
        if not validate_url(url):
            return self._err('请输入有效的 http(s) 仓库 RPM 链接（以 .rpm 结尾）')
        if not url.lower().split('?')[0].endswith('.rpm'):
            return self._err('链接应以 .rpm 结尾（yum 仓库包，如 pgdg-redhat-repo-latest.noarch.rpm）')
        try:
            rpm = fetch_bytes(url, timeout=60)
        except Exception as e:
            return self._err('仓库 RPM 下载失败: %s' % e)
        if len(rpm) > 64 * 1024 * 1024:
            return self._err('文件过大（>64MB），不像仓库 RPM')
        try:
            repos, skipped, warnings = parse_repo_rpm(rpm, arch, releasever)
        except ValueError as e:
            return self._err(str(e))
        except Exception as e:
            return self._err('RPM 解析失败: %s' % e)
        if not repos:
            return self._err('未解析出可用仓库：%s' % '；'.join(warnings or ['没有启用的仓库']))
        self._send_json({'ok': True, 'repos': repos, 'skipped': skipped,
                         'warnings': warnings})

    def _api_search(self):
        data = self._read_json()
        q = (data.get('q') or '').strip()
        if not q:
            return self._err('请输入包名关键字')
        limit = min(int(data.get('limit') or 200), SEARCH_CAP)
        repo_f = data.get('repo') or ''
        arch_f = data.get('arch') or ''
        with STATE['lock']:
            packages = STATE['packages']
        if not packages:
            return self._err('尚未加载元数据，请先在上方加载仓库')
        try:
            rx = re.compile(q, re.I) if data.get('regex') else None
        except re.error as e:
            return self._err('正则表达式无效: %s' % e)
        ql = q.lower()
        qn = _norm_name(q)
        exact, prefix, other = [], [], []
        for p in packages:
            if repo_f and p['repo'] != repo_f:
                continue
            if arch_f and p['arch'] != arch_f:
                continue
            nm = p['name']
            nl = nm.lower()
            if rx:
                matched = bool(rx.search(nm))
            else:
                matched = (ql in nl) or (qn and qn in _norm_name(nm))
            if not matched:
                continue
            item = {'id': p['id'], 'name': nm, 'evr': p['evr'], 'arch': p['arch'],
                    'repo': p['repo'], 'size': p['size'], 'summary': p['summary']}
            if nl == ql:
                exact.append(item)
            elif nl.startswith(ql) or _norm_name(nm).startswith(qn):
                prefix.append(item)
            else:
                other.append(item)
        via_summary = False
        if not (exact or prefix or other) and not rx:
            # 包名无匹配时退回按描述搜索
            for p in packages:
                if repo_f and p['repo'] != repo_f:
                    continue
                if arch_f and p['arch'] != arch_f:
                    continue
                if ql in (p['summary'] or '').lower():
                    other.append({'id': p['id'], 'name': p['name'], 'evr': p['evr'],
                                  'arch': p['arch'], 'repo': p['repo'], 'size': p['size'],
                                  'summary': p['summary']})
            via_summary = bool(other)
        results = exact + sorted(prefix, key=lambda x: x['name']) + sorted(other, key=lambda x: x['name'])
        self._send_json({'ok': True, 'results': results[:limit],
                         'total': len(exact) + len(prefix) + len(other),
                         'via_summary': via_summary})

    def _api_resolve(self):
        data = self._read_json()
        ids = data.get('ids') or []
        if not ids or len(ids) > 500:
            return self._err('请选择 1-500 个包')
        task = new_task('resolve', '解析依赖（%d 个包）' % len(ids))
        th = threading.Thread(target=run_resolve_task, args=(task, ids), daemon=True)
        th.start()
        self._send_json({'ok': True, 'task_id': task['id']})

    def _api_download(self):
        data = self._read_json()
        ids = data.get('ids') or []
        d = (data.get('dir') or '').strip()
        conc = int(data.get('concurrency') or 6)
        conc = max(1, min(conc, 16))
        rp, err = validate_download_dir(d)
        if err:
            return self._err(err)
        with STATE['lock']:
            pkg_by_id = STATE['pkg_by_id']
            items = []
            for pid in ids[:RESOLVE_CAP]:
                p = pkg_by_id.get(pid)
                if p:
                    items.append({'basename': p['basename'], 'url': p['url'],
                                  'size': p['size'], 'sha256': p['sha256'],
                                  'family': p['family'], 'name': p['name'],
                                  'arch': p['arch'], 'epoch': p['epoch'],
                                  'ver': p['ver'], 'rel': p['rel'], 'evr': p['evr'],
                                  'repo': p.get('repo', ''),
                                  'summary': p['summary'], 'description': p.get('description', ''),
                                  'provides': p.get('provides') or [],
                                  'requires': p.get('requires') or []})
            busy = any(t['status'] == 'running' and t['type'] == 'download'
                       for t in STATE['tasks'].values())
        if not items:
            return self._err('没有可下载的包（元数据可能已变更，请重新解析依赖）')
        if busy:
            return self._err('已有下载任务进行中，请先完成或取消')
        # 文件名冲突处理：同名不同大小则加前缀
        seen = {}
        for it in items:
            bn = it['basename']
            if bn in seen and seen[bn] != it['size']:
                it['basename'] = '%s_%s' % (it['repo'], bn)
            seen[it['basename']] = it['size']
        task = new_task('download', '下载 %d 个包' % len(items))
        emit(task, 'log', '目标目录: %s  并发: %d  包数: %d' % (rp, conc, len(items)))
        th = threading.Thread(target=run_download_task,
                              args=(task, items, rp, conc), daemon=True)
        th.start()
        self._send_json({'ok': True, 'task_id': task['id']})

    # -- 目录浏览 ----------------------------------------------------------
    def _ls(self, path):
        path = (path or '').strip()
        if not path:
            if os.name == 'nt':
                drives = ['%s:\\' % c for c in string.ascii_uppercase
                          if os.path.isdir('%s:\\' % c)]
                return self._send_json({'ok': True, 'path': '', 'parent': '',
                                        'dirs': drives})
            path = '/'
        rp = os.path.realpath(path)
        if not os.path.isdir(rp):
            return self._err('目录不存在: %s' % rp)
        parent = os.path.dirname(rp.rstrip('\\/')) or rp
        dirs = []
        try:
            for name in sorted(os.listdir(rp)):
                full = os.path.join(rp, name)
                if os.path.isdir(full):
                    dirs.append(full)
        except PermissionError:
            return self._err('无权限访问: %s' % rp)
        self._send_json({'ok': True, 'path': rp, 'parent': parent, 'dirs': dirs[:500]})

    # -- SSE ---------------------------------------------------------------
    def _sse(self, tid):
        with STATE['lock']:
            task = STATE['tasks'].get(tid)
        if not task:
            return self._err('任务不存在', 404)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        self.close_connection = False

        def send(ev):
            self.wfile.write(('data: %s\n\n' % json.dumps(ev, ensure_ascii=False)).encode('utf-8'))
            self.wfile.flush()

        try:
            for ev in task['history']:
                send(ev)
            if task['status'] != 'running':
                return
            while True:
                try:
                    ev = task['events'].get(timeout=15)
                    send(ev)
                    if ev['type'] in ('done', 'error', 'cancelled'):
                        return
                except queue.Empty:
                    self.wfile.write(b': hb\n\n')
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            self.close_connection = True


# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description='Linux 离线包下载工具')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--no-browser', action='store_true')
    args = ap.parse_args()

    os.makedirs(DEFAULT_DL_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = 'http://%s:%d/' % (args.host, args.port)
    print('=' * 60)
    print('  Linux 离线包下载工具已启动:  %s' % url)
    print('  默认下载目录: %s' % DEFAULT_DL_DIR)
    print('  按 Ctrl+C 停止服务')
    print('=' * 60)
    if not args.no_browser:
        try:
            import webbrowser
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')


if __name__ == '__main__':
    main()
