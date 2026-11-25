#!/usr/bin/env python3
"""
腾讯云 COS 云存储（可选）：把站点里的图片/视频等媒体资源上传到 COS，
并把 Markdown 里的资源引用改写为 COS / CDN 的绝对 URL。

默认情况下 md2website 仍把图片/视频复制到站点目录本地（docs/ 下），
只有显式开启 COS 时才上传并改写引用，源目录始终只读。

用法:
    python3 cos.py template [--out <路径>] [--force]       # 打印/写出配置模板
    python3 cos.py set KEY=VALUE [KEY=VALUE ...]           # 写入全局配置 ~/.md2website/cos.yml
    python3 cos.py show [--config <文件>] [--project <站点目录>]
    python3 cos.py check [--config <文件>] [--project <站点目录>]
    python3 cos.py upload <docs目录> [--config <文件>] [--dry-run]
    python3 cos.py rewrite <docs目录> [--config <文件>] [--dry-run]
    python3 cos.py sync <站点目录> [--config <文件>] [--dry-run]

配置字段（SECRET_ID/SECRET_KEY/REGION/BUCKET 必填）:
    SECRET_ID    腾讯云 SecretId
    SECRET_KEY   腾讯云 SecretKey
    REGION       地域，如 ap-guangzhou
    BUCKET       存储桶名称（含 APPID，如 mybucket-1250000000）
    URL_PREFIX   对外访问前缀（CDN 域名或 COS 域名；不填则用 COS 默认域名）
    PATH_PREFIX  桶内对象前缀，如 school/shuxue/（多站点共用桶时用于隔离）

上传依赖腾讯云官方 SDK，缺失时按提示执行:
    pip install cos-python-sdk-v5
"""
import argparse
import os
import re
import sys

IMG_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp', '.ico'}
VIDEO_EXTS = {'.mp4', '.webm', '.mov', '.m4v', '.mkv'}
SKIP_DIRS = {'.vitepress', 'public', 'node_modules', 'dist', 'build', '.git', '.cache', '__pycache__'}

COS_DIR = os.path.join(os.path.expanduser('~'), '.md2website')
COS_GLOBAL = os.path.join(COS_DIR, 'cos.yml')
LOCAL_CFG = '.cos-local.yml'

# 字段定义：(键, 说明, 是否必填)
FIELDS = [
    ('SECRET_ID', '腾讯云 SecretId', True),
    ('SECRET_KEY', '腾讯云 SecretKey', True),
    ('REGION', '地域，如 ap-guangzhou', True),
    ('BUCKET', '存储桶名称（含 APPID，如 mybucket-1250000000）', True),
    ('URL_PREFIX', '对外访问前缀（CDN 域名或 COS 域名；不填则用 COS 默认域名）', False),
    ('PATH_PREFIX', '桶内对象前缀，如 school/shuxue/（多站点共用桶时隔离）', False),
]
REQUIRED = [k for k, _, req in FIELDS if req]
DESCRIBE = {k: d for k, d, _ in FIELDS}


def posix(p):
    return p.replace(os.sep, '/')


def strip_inline_comment(v):
    """去掉行尾注释：仅当 # 前有空白时生效，避免密钥里的 # 被误截断。"""
    if v[:1] in ('"', "'"):
        end = v.find(v[0], 1)
        if end != -1:
            return v[:end + 1]
    for i, ch in enumerate(v):
        if ch == '#' and i > 0 and v[i - 1].isspace():
            return v[:i].rstrip()
    return v


def parse_cfg(path):
    """解析 key: value 或 KEY=value 单行配置；# 为注释。不执行任何 shell 代码。"""
    data = {}
    if not os.path.isfile(path):
        return data
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            sep = None
            if '=' in line:
                sep = '='
            elif ':' in line:
                sep = ':'
            else:
                continue
            k, v = line.split(sep, 1)
            k = k.strip().upper()
            v = strip_inline_comment(v.strip())
            if len(v) >= 2 and v[0] == v[-1] and v[0] in '\"\'':
                v = v[1:-1]
            data[k] = v
    return data


def write_cfg(path, values):
    lines = [
        '# ============================================================',
        '# 腾讯云 COS 云存储配置（md2website 可选，权限 600，切勿提交到仓库）',
        '# 填好后：python3 cos.py check --config <本文件>',
        '# ============================================================',
        '',
    ]
    for key, desc, _ in FIELDS:
        lines.append(f'# {desc}')
        val = values.get(key, '')
        lines.append(f'{key}: {val}')
        lines.append('')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def find_config(config_arg, project_dir=None):
    """返回 (path, source)。config_arg 优先，其次站点本地，最后全局默认。"""
    if config_arg:
        p = os.path.abspath(os.path.expanduser(config_arg))
        return p, ('--config' if os.path.isfile(p) else '--config(缺失)')
    if project_dir:
        local = os.path.join(os.path.abspath(os.path.expanduser(project_dir)), LOCAL_CFG)
        if os.path.isfile(local):
            return local, f'站点本地 {LOCAL_CFG}'
    return COS_GLOBAL, '全局 ~/.md2website/cos.yml'


def missing_of(data):
    return [k for k in REQUIRED if not (data.get(k) or '').strip()]


def default_domain(data):
    bucket = (data.get('BUCKET') or '').strip()
    region = (data.get('REGION') or '').strip()
    if bucket and region:
        return f'https://{bucket}.cos.{region}.myqcloud.com'
    return ''


def base_url(data):
    """返回对外访问的 URL 前缀（不含末尾 /，已含 PATH_PREFIX）。"""
    prefix = (data.get('URL_PREFIX') or '').strip() or default_domain(data)
    base = prefix.rstrip('/')
    pp = (data.get('PATH_PREFIX') or '').strip().strip('/')
    if pp:
        base = base + '/' + pp
    return base


def object_key(data, site_rel):
    pp = (data.get('PATH_PREFIX') or '').strip().strip('/')
    if pp:
        return f'{pp}/{site_rel}'
    return site_rel


def iter_assets(docs_root):
    """遍历 docs 下的图片/视频，返回 [(绝对路径, 相对 docs 根的 POSIX 路径)]。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(docs_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.')]
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext in IMG_EXTS or ext in VIDEO_EXTS:
                abs_path = os.path.join(dirpath, fn)
                site_rel = posix(os.path.relpath(abs_path, docs_root))
                out.append((abs_path, site_rel))
    return out


# ---- 引用改写 ----

IMG_REF = re.compile(r'!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?([^)]*)\)')
MD_LINK = re.compile(r'(?<!!)\[([^\]]*)\]\(\s*<?([^)\s>]+)>?([^)]*)\)')
HTML_TAG_SRC = re.compile(
    r'<(img|video|source)\b([^>]*?)\bsrc\s*=\s*["\']([^"\']+)["\']([^>]*)>', re.I)
FENCE = re.compile(r'^\s*(```|~~~)')


def _is_external(ref):
    if re.match(r'^(https?:)?//', ref) or ref.startswith('//'):
        return True
    return ref.startswith(('data:', 'mailto:', 'tel:', '#')) or not ref


def _rewrite_ref(ref, base, md_dir, docs_root, known_assets):
    """返回 (新ref 或 None 表示不改, 是否已改写)。"""
    stripped = ref.split('?')[0].split('#')[0]
    ext = os.path.splitext(stripped)[1].lower()
    if ext not in IMG_EXTS and ext not in VIDEO_EXTS:
        return None, False
    if _is_external(stripped):
        return None, False
    if stripped.startswith(base):
        return None, False
    abs_target = os.path.normpath(os.path.join(md_dir, stripped))
    site_rel = posix(os.path.relpath(abs_target, docs_root))
    if site_rel == '..' or site_rel.startswith('../'):
        return None, False
    # 只改写本地真实存在的资源，避免给死链生成 COS 地址
    if site_rel not in known_assets:
        return None, False
    tail = ref[len(stripped):]
    return f'{base}/{site_rel}{tail}', True


def rewrite_file(path, base, docs_root, known_assets, dry):
    text = open(path, encoding='utf-8').read()
    lines = text.splitlines(keepends=True)
    changed = 0
    out_lines = []
    in_fence = False
    for line in lines:
        if FENCE.match(line):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if in_fence:
            out_lines.append(line)
            continue

        # 1) Markdown 图片 ![alt](ref)
        def repl_img(m):
            nonlocal changed
            ref = m.group(2)
            new, ok = _rewrite_ref(ref, base, os.path.dirname(path), docs_root, known_assets)
            if ok:
                changed += 1
                return f'![{m.group(1)}]({new}{m.group(3)})'
            return m.group(0)

        # 2) Markdown 链接 [text](ref)（仅图片/视频扩展名）
        def repl_link(m):
            nonlocal changed
            ref = m.group(2)
            new, ok = _rewrite_ref(ref, base, os.path.dirname(path), docs_root, known_assets)
            if ok:
                changed += 1
                return f'[{m.group(1)}]({new}{m.group(3)})'
            return m.group(0)

        # 3) HTML <img>/<video>/<source> src
        def repl_html(m):
            nonlocal changed
            ref = m.group(3)
            new, ok = _rewrite_ref(ref, base, os.path.dirname(path), docs_root, known_assets)
            if ok:
                changed += 1
                return f'<{m.group(1)}{m.group(2)}src="{new}"{m.group(4)}>'
            return m.group(0)

        line = HTML_TAG_SRC.sub(repl_html, line)
        line = IMG_REF.sub(repl_img, line)
        line = MD_LINK.sub(repl_link, line)
        out_lines.append(line)

    if changed and not dry:
        with open(path, 'w', encoding='utf-8') as f:
            f.writelines(out_lines)
    return changed


def iter_md(docs_root):
    for dirpath, dirnames, filenames in os.walk(docs_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.')]
        for fn in sorted(filenames):
            if fn.lower().endswith(('.md', '.markdown')):
                yield os.path.join(dirpath, fn)


def rewrite_docs(docs_root, base, dry):
    known = {site_rel for _, site_rel in iter_assets(docs_root)}
    changed_files = 0
    total = 0
    for path in iter_md(docs_root):
        n = rewrite_file(path, base, docs_root, known, dry)
        if n:
            changed_files += 1
            total += n
    return total, changed_files, len(known)


def upload_docs(docs_root, data, dry):
    assets = iter_assets(docs_root)
    if dry:
        return len(assets), 0, []

    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError:
        print('未安装腾讯云 COS SDK，请先执行: pip install cos-python-sdk-v5',
              file=sys.stderr)
        return 0, len(assets), []

    cfg = CosConfig(Region=data['REGION'].strip(),
                    SecretId=data['SECRET_ID'].strip(),
                    SecretKey=data['SECRET_KEY'].strip())
    client = CosS3Client(cfg)
    bucket = data['BUCKET'].strip()
    uploaded = 0
    failed = 0
    errors = []
    for abs_path, site_rel in assets:
        key = object_key(data, site_rel)
        try:
            client.upload_file(Bucket=bucket, Key=key, LocalFilePath=abs_path,
                               EnableMD5=False)
            uploaded += 1
        except Exception as e:
            failed += 1
            errors.append((site_rel, str(e)))
    return uploaded, failed, errors


# ---- 子命令 ----

def cmd_template(args):
    text = '\n'.join(
        f'# {desc}\n{k}: '
        for k, desc, _ in FIELDS
    )
    if not args.out:
        header = '\n'.join([
            '# 腾讯云 COS 云存储配置模板（权限 600，切勿提交到仓库）',
            '# 填好后保存到站点目录的 .cos-local.yml，或全局 ~/.md2website/cos.yml',
            '# 之后执行: python3 cos.py check --config <本文件>',
            '',
        ])
        sys.stdout.write(header + text + '\n')
        return 0
    out = os.path.abspath(os.path.expanduser(args.out))
    if os.path.exists(out) and not args.force:
        print(f'目标已存在，未覆盖: {out}\n确认覆盖请加 --force', file=sys.stderr)
        return 1
    if not os.path.isdir(os.path.dirname(out)):
        print(f'目录不存在: {os.path.dirname(out)}', file=sys.stderr)
        return 1
    with open(out, 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    print(f'已写入: {out}')
    print('请填入 SECRET_ID/SECRET_KEY/REGION/BUCKET 后，用 check 复核')
    return 0


def cmd_set(args):
    data = parse_cfg(COS_GLOBAL)
    for pair in args.pairs:
        if '=' not in pair:
            print(f'格式错误（应为 KEY=VALUE）: {pair}', file=sys.stderr)
            return 1
        k, v = pair.split('=', 1)
        k = k.strip().upper()
        if k not in DESCRIBE:
            print(f'未知字段: {k}\n可用字段: {", ".join(DESCRIBE)}', file=sys.stderr)
            return 1
        data[k] = v.strip()
    path = write_cfg(COS_GLOBAL, data)
    print(f'已写入: {path}')
    miss = missing_of(data)
    if miss:
        print('仍缺必填项: ' + ', '.join(miss))
        return 2
    return 0


def _mask(v):
    v = v or ''
    if len(v) <= 8:
        return '***'
    return v[:4] + '***' + v[-4:]


def cmd_show(args):
    path, source = find_config(args.config, args.project)
    data = parse_cfg(path)
    print(f'配置来源: {source}')
    print(f'文件路径: {path}')
    if not data:
        print('（尚未创建，可用 template 生成模板，或用 set 写入全局配置）')
        return 1
    for k, desc, _ in FIELDS:
        val = data.get(k, '')
        if k == 'SECRET_KEY' and val:
            val = _mask(val)
        print(f'  {k:<14} {val or "<未设置>"}   # {desc}')
    miss = missing_of(data)
    if miss:
        print(f'\n缺必填项: {", ".join(miss)}')
    else:
        print(f'\n对外 URL 前缀: {base_url(data)}/')
    return 0


def cmd_check(args):
    path, source = find_config(args.config, args.project)
    data = parse_cfg(path)
    miss = missing_of(data)
    if not data:
        print('MISSING')
        print('  （无配置）可用 template 生成模板，或用 set 写入全局配置')
        return 2
    if miss:
        print('INCOMPLETE')
        for k in miss:
            print(f'  {k}: {DESCRIBE[k]}')
        return 2
    print('OK')
    print(f'  bucket: {data["BUCKET"].strip()}   region: {data["REGION"].strip()}')
    print(f'  URL 前缀: {base_url(data)}/')
    return 0


def _load_for(args, project_dir):
    path, source = find_config(args.config, project_dir)
    data = parse_cfg(path)
    return data, path, source


def cmd_upload(args):
    docs = os.path.abspath(args.docs)
    if not os.path.isdir(docs):
        print(f'目录不存在: {docs}', file=sys.stderr)
        return 1
    data, path, source = _load_for(args, os.path.dirname(docs))
    miss = missing_of(data)
    if miss:
        print(f'COS 配置不完整（{source}），缺: {", ".join(miss)}', file=sys.stderr)
        print('可用 template 生成模板，或用 set 写入全局配置', file=sys.stderr)
        return 2
    n_assets = len(iter_assets(docs))
    print(f'配置: {source}')
    print(f'目标: {data["BUCKET"].strip()} ({data["REGION"].strip()})  前缀: {base_url(data)}/')
    print(f'待上传资源: {n_assets} 个（图片/视频）')
    if args.dry_run:
        for abs_path, site_rel in iter_assets(docs):
            print(f'  [预览] {site_rel} -> {object_key(data, site_rel)}')
        print('去掉 --dry-run 实际上传。')
        return 0
    uploaded, failed, errors = upload_docs(docs, data, dry=False)
    print(f'\n上传完成：成功 {uploaded}，失败 {failed}')
    for rel, err in errors:
        print(f'  ! {rel}: {err}')
    return 1 if failed else 0


def cmd_rewrite(args):
    docs = os.path.abspath(args.docs)
    if not os.path.isdir(docs):
        print(f'目录不存在: {docs}', file=sys.stderr)
        return 1
    data, path, source = _load_for(args, os.path.dirname(docs))
    miss = missing_of(data)
    if miss:
        print(f'COS 配置不完整（{source}），缺: {", ".join(miss)}', file=sys.stderr)
        return 2
    base = base_url(data)
    print(f'配置: {source}')
    print(f'改写目标: {base}/')
    total, changed_files, n_assets = rewrite_docs(docs, base, dry=args.dry_run)
    tag = '待改写' if args.dry_run else '已改写'
    print(f'共 {tag} {total} 处引用（涉及 {changed_files} 个文件，资源 {n_assets} 个）')
    if args.dry_run:
        print('去掉 --dry-run 实际写入。')
    return 0


def cmd_sync(args):
    site = os.path.abspath(args.project)
    if not os.path.isdir(site):
        print(f'站点目录不存在: {site}', file=sys.stderr)
        return 1
    docs = os.path.join(site, 'docs')
    if not os.path.isdir(docs):
        print(f'未找到 docs 目录: {docs}', file=sys.stderr)
        return 1
    data, path, source = _load_for(args, site)
    miss = missing_of(data)
    if miss:
        print(f'COS 配置不完整（{source}），缺: {", ".join(miss)}', file=sys.stderr)
        print('可用 template 生成模板，或用 set 写入全局配置', file=sys.stderr)
        return 2

    n_assets = len(iter_assets(docs))
    print(f'配置: {source}')
    print(f'目标: {data["BUCKET"].strip()} ({data["REGION"].strip()})')
    print(f'URL 前缀: {base_url(data)}/')
    print(f'资源数: {n_assets} 个（图片/视频）')

    if args.dry_run:
        print('\n[预览] 将上传的资源：')
        for abs_path, site_rel in iter_assets(docs):
            print(f'  {site_rel} -> {object_key(data, site_rel)}')
        return 0

    uploaded, failed, errors = upload_docs(docs, data, dry=False)
    print(f'\n上传：成功 {uploaded}，失败 {failed}')
    for rel, err in errors:
        print(f'  ! {rel}: {err}')

    total, changed_files, _ = rewrite_docs(docs, base_url(data), dry=False)
    print(f'改写：{total} 处引用（{changed_files} 个文件）')

    if failed:
        print('提示：部分资源上传失败，其引用未改写或仍指向本地文件。')
        return 1
    print('\n完成。图片/视频已上传到 COS，站点 Markdown 引用已指向云存储 URL。')
    print('注意：请确保存储桶为公有读，或已配置 CDN 加速域名（URL_PREFIX 填 CDN 域名）。')
    return 0


def main():
    ap = argparse.ArgumentParser(description='腾讯云 COS 云存储（可选）：上传图片/视频并改写引用')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('template', help='生成配置模板（默认打印，--out 写文件）')
    p.add_argument('--out', help='写入指定文件')
    p.add_argument('--force', action='store_true', help='--out 目标已存在时覆盖')

    p = sub.add_parser('set', help='写入全局配置 ~/.md2website/cos.yml')
    p.add_argument('pairs', nargs='+', help='KEY=VALUE，如 SECRET_ID=...')

    p = sub.add_parser('show', help='查看当前配置（密钥脱敏）')
    p.add_argument('--config', help='指定配置文件')
    p.add_argument('--project', help='站点目录（读取其 .cos-local.yml）')

    p = sub.add_parser('check', help='检查配置完整性')
    p.add_argument('--config', help='指定配置文件')
    p.add_argument('--project', help='站点目录（读取其 .cos-local.yml）')

    p = sub.add_parser('upload', help='上传 docs 下的图片/视频到 COS')
    p.add_argument('docs', help='docs 目录路径')
    p.add_argument('--config', help='指定配置文件')
    p.add_argument('--dry-run', action='store_true', help='只列出，不上传')

    p = sub.add_parser('rewrite', help='把 Markdown 里的资源引用改写为 COS/CDN URL')
    p.add_argument('docs', help='docs 目录路径')
    p.add_argument('--config', help='指定配置文件')
    p.add_argument('--dry-run', action='store_true', help='只预览，不写入')

    p = sub.add_parser('sync', help='上传 + 改写引用（对站点目录一键执行）')
    p.add_argument('project', help='站点目录路径')
    p.add_argument('--config', help='指定配置文件')
    p.add_argument('--dry-run', action='store_true', help='只预览，不执行')

    args = ap.parse_args()
    return {
        'template': cmd_template,
        'set': cmd_set,
        'show': cmd_show,
        'check': cmd_check,
        'upload': cmd_upload,
        'rewrite': cmd_rewrite,
        'sync': cmd_sync,
    }[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
