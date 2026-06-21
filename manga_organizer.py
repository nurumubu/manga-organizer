#!/usr/bin/env python3
"""
만화 권 정리 + 이미지 편집 앱
실행: python manga_organizer.py
브라우저에서 http://localhost:7777 열기
pip install Pillow  (이미지 편집 기능 사용 시 필요)
"""

import os, sys, json, shutil, base64, mimetypes, io, zipfile, threading, time
try:
    import img2pdf
    HAS_IMG2PDF = True
except ImportError:
    HAS_IMG2PDF = False
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qs, unquote

_server_ref = None          # HTTPServer 인스턴스 참조
_heartbeat_timer = None    # 브라우저 연결 감시 타이머
_heartbeat_timeout = 3     # 초 — 이 시간 동안 heartbeat 없으면 종료

def _shutdown():
    """프로세스 즉시 강제 종료"""
    def _do():
        time.sleep(0.3)
        print("\n👋 만화 정리기 종료됨")
        import os
        os._exit(0)
    threading.Thread(target=_do, daemon=True).start()

def _reset_heartbeat():
    global _heartbeat_timer
    if _heartbeat_timer:
        _heartbeat_timer.cancel()
    _heartbeat_timer = threading.Timer(_heartbeat_timeout, _on_heartbeat_lost)
    _heartbeat_timer.daemon = True
    _heartbeat_timer.start()

def _on_heartbeat_lost():
    if _heartbeat_timer is None:
        return
    print("\n🔌 브라우저 탭이 닫혔습니다. 서버를 종료합니다...")
    _shutdown()

# tkinter 폴더 선택 (선택적)
try:
    import tkinter as tk
    from tkinter import filedialog
    HAS_TK = True
except ImportError:
    HAS_TK = False

LAST_PATH_FILE = Path(__file__).parent / '.manga_last_path'

def save_last_path(path):
    try: LAST_PATH_FILE.write_text(path, encoding='utf-8')
    except Exception: pass

def load_last_path():
    try: return LAST_PATH_FILE.read_text(encoding='utf-8').strip()
    except Exception: return ''

HOST = "localhost"
PORT = 7777
IMG_EXTS = {'.jpg', '.jpeg', '.jped', '.png', '.webp', '.gif', '.bmp',
           '.tiff', '.tif', '.avif', '.heic', '.heif', '.jfif', '.jp2'}

def is_image(path):
    """확장자 무관하게 이미지 파일인지 판별. 알려진 확장자 우선, 나머지는 mimetypes로 확인."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in IMG_EXTS:
        return True
    mime = mimetypes.guess_type(str(p))[0] or ''
    return mime.startswith('image/')

try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

def natural_key(path):
    """자연어 정렬 키 — '10화'가 '2화'보다, '88.5화'가 '88화'와 '89화' 사이에 오도록"""
    import re
    parts = []
    for c in re.split(r'(\d+\.\d+|\d+)', path.name):
        if re.match(r'^\d+\.\d+$', c):
            parts.append(float(c))
        elif c.isdigit():
            parts.append(int(c))
        else:
            parts.append(c.lower())
    return parts

def scan_folder(root):
    result = []
    root = Path(root)
    if not root.exists():
        return {'error': f'경로를 찾을 수 없습니다: {root}'}
    dirs = sorted([e for e in root.iterdir() if e.is_dir()], key=natural_key)
    if not dirs:
        return {'error': f'하위 폴더가 없습니다.\n현재 경로: {root}'}

    def get_images(folder):
        return sorted([f for f in folder.iterdir()
                       if f.is_file() and is_image(f) and BACKUP_DIR_NAME not in f.parts],
                      key=natural_key)

    # ── 구조 자동 감지 ──
    # 루트 직하위 폴더 안에 이미지가 바로 있으면 → 2단계 (루트=작품, 하위폴더=화)
    # 없으면 → 3단계 (루트, 작품, 화)
    two_level = any(get_images(d) for d in dirs)

    if two_level:
        # 2단계: 입력 경로 자체가 작품 폴더, 하위 폴더가 화
        chapters = []
        for ch_dir in dirs:
            images = get_images(ch_dir)
            if images:
                chapters.append({'name': ch_dir.name, 'path': str(ch_dir),
                                 'images': [str(f) for f in images], 'count': len(images)})
        if chapters:
            result.append({'title': root.name, 'path': str(root), 'chapters': chapters})
    else:
        # 3단계: 하위 폴더가 작품, 그 안 폴더가 화
        for title_dir in dirs:
            chapters = []
            ch_dirs = sorted([e for e in title_dir.iterdir() if e.is_dir()], key=natural_key)
            for ch_dir in ch_dirs:
                images = get_images(ch_dir)
                if images:
                    chapters.append({'name': ch_dir.name, 'path': str(ch_dir),
                                     'images': [str(f) for f in images], 'count': len(images)})
            if chapters:
                result.append({'title': title_dir.name, 'path': str(title_dir), 'chapters': chapters})

    if not result:
        return {'error': f'인식된 만화가 없습니다.\n화 폴더 안에 이미지 파일이 있는지 확인하세요.\n현재 경로: {root}'}
    return result


def create_volume_folder(src_chapters, dest_path):
    dest = Path(dest_path)
    dest.mkdir(parents=True, exist_ok=True)
    page_num = 1
    for ch_path in src_chapters:
        ch = Path(ch_path)
        images = sorted([f for f in ch.iterdir() if f.suffix.lower() in IMG_EXTS])
        for img in images:
            shutil.copy2(img, dest / f"{page_num:04d}{img.suffix}")
            page_num += 1
    return page_num - 1

BACKUP_DIR_NAME = '_원본백업'

def backup_image(img_path):
    """이미지를 같은 폴더의 _원본백업 디렉토리에 백업. 이미 있으면 스킵."""
    p = Path(img_path)
    backup_dir = p.parent / BACKUP_DIR_NAME
    backup_dir.mkdir(exist_ok=True)
    dest = backup_dir / p.name
    if not dest.exists():
        shutil.copy2(p, dest)
    return str(dest)

def restore_image(img_path):
    """_원본백업에서 원본 복원"""
    p = Path(img_path)
    backup = p.parent / BACKUP_DIR_NAME / p.name
    if not backup.exists():
        raise FileNotFoundError(f"백업 없음: {backup}")
    shutil.copy2(backup, p)

def get_backup_status(folder_path):
    """폴더 내 백업 현황 반환"""
    p = Path(folder_path)
    backup_dir = p / BACKUP_DIR_NAME
    if not backup_dir.exists():
        return {'has_backup': False, 'count': 0}
    backed = [f for f in backup_dir.iterdir() if f.suffix.lower() in IMG_EXTS]
    return {'has_backup': len(backed) > 0, 'count': len(backed)}

def create_pdf(image_paths, dest_pdf_path, jpg_quality=85):
    """이미지 목록으로 PDF 생성"""
    dest = Path(dest_pdf_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if HAS_IMG2PDF:
        # img2pdf: 무손실 (PNG/JPG 그대로 임베드, 빠름)
        imgs = []
        for p in image_paths:
            fp = Path(p)
            if is_image(fp):
                imgs.append(str(fp))
        with open(dest, 'wb') as f:
            f.write(img2pdf.convert(imgs))
    elif HAS_PIL:
        # PIL fallback: JPG로 변환 후 저장
        images = []
        for p in image_paths:
            fp = Path(p)
            if fp.suffix.lower() not in IMG_EXTS:
                continue
            img = Image.open(fp).convert('RGB')
            images.append(img)
        if not images:
            raise ValueError("이미지 없음")
        images[0].save(dest, save_all=True, append_images=images[1:], quality=jpg_quality)
    else:
        raise RuntimeError("PDF 생성에는 img2pdf 또는 Pillow가 필요합니다.")
    return str(dest)

def folder_to_pdf(folder_path, dest_pdf_path, jpg_quality=85):
    """폴더 내 이미지를 PDF로 변환"""
    p = Path(folder_path)
    images = sorted([str(f) for f in p.rglob('*') if f.suffix.lower() in IMG_EXTS and '_원본백업' not in str(f)])
    if not images:
        raise ValueError("이미지 없음")
    return create_pdf(images, dest_pdf_path, jpg_quality)

def collect_chapter_images(chapter_paths):
    """여러 화 경로에서 이미지 파일 목록을 순서대로 수집"""
    all_imgs = []
    for ch_path in chapter_paths:
        ch = Path(ch_path)
        all_imgs += sorted([f for f in ch.iterdir() if f.is_file() and is_image(f)])
    return all_imgs

def compress_image(src_path, dest_path, jpg_quality=85):
    """단일 이미지를 JPG로 변환/압축. dest_path 확장자는 .jpg 여야 함."""
    if not HAS_PIL:
        raise RuntimeError("이미지 압축에는 Pillow가 필요합니다. pip install Pillow")
    img = Image.open(src_path).convert('RGB')
    img.save(dest_path, format='JPEG', quality=jpg_quality, optimize=True)

def create_archive(image_paths, dest_path, compress_to_jpg=False, jpg_quality=85):
    """이미지 목록으로 CBZ 또는 ZIP 생성 (확장자로 구분).
    CBZ/ZIP 모두 ZIP_STORED(무손실 컨테이너)로 저장.
    compress_to_jpg=True 시 PNG->JPG 변환은 손실."""
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, 'w', compression=zipfile.ZIP_STORED) as zf:
        for i, p in enumerate(image_paths):
            fp = Path(p)
            if fp.suffix.lower() not in IMG_EXTS:
                continue
            if compress_to_jpg and fp.suffix.lower() not in {'.jpg', '.jpeg'}:
                if not HAS_PIL:
                    raise RuntimeError("PNG->JPG 변환에는 Pillow가 필요합니다. pip install Pillow")
                buf = io.BytesIO()
                img = Image.open(fp).convert('RGB')
                img.save(buf, format='JPEG', quality=jpg_quality, optimize=True)
                zf.writestr(f"{i+1:04d}.jpg", buf.getvalue())
            else:
                zf.write(fp, f"{i+1:04d}{fp.suffix.lower()}")
    return str(dest)

def create_cbz(image_paths, dest_cbz_path, compress_to_jpg=False, jpg_quality=85):
    return create_archive(image_paths, dest_cbz_path, compress_to_jpg, jpg_quality)

# 썸네일 메모리 캐시 {img_path: (mtime, bytes)}
_thumb_cache = {}
import threading as _threading
_thumb_cache_lock = _threading.Lock()

def create_thumbnail(img_path, size=160):
    """이미지를 size px 썸네일로 변환. Pillow 필요. 캐시 사용."""
    p = Path(img_path)
    mtime = p.stat().st_mtime
    with _thumb_cache_lock:
        if img_path in _thumb_cache and _thumb_cache[img_path][0] == mtime:
            return _thumb_cache[img_path][1]
    img = Image.open(p)
    img.thumbnail((size, size * 2), Image.LANCZOS)
    buf = io.BytesIO()
    fmt = 'JPEG' if p.suffix.lower() in {'.jpg','.jpeg','.jped'} else 'PNG'
    if fmt == 'JPEG' and img.mode in ('RGBA', 'P'):
        img = img.convert('RGB')
    img.save(buf, format=fmt, quality=70)
    data = buf.getvalue()
    with _thumb_cache_lock:
        _thumb_cache[img_path] = (mtime, data)
    return data

def split_image(img_path, direction='ltr', split_x=0.5, output_dir=None):
    """양면 이미지 분리. split_x: 분할선 비율(0~1), 파일명: 원본+a/b"""
    if not HAS_PIL:
        raise RuntimeError("Pillow가 필요합니다.")
    p = Path(img_path)
    img = Image.open(p)
    w, h = img.size
    mid = max(1, min(w-1, int(w * split_x)))
    left  = img.crop((0, 0, mid, h))
    right = img.crop((mid, 0, w, h))
    out_dir = Path(output_dir) if output_dir else p.parent
    first_path  = out_dir / f"{p.stem}a{p.suffix}"
    second_path = out_dir / f"{p.stem}b{p.suffix}"
    if direction == 'rtl':
        right.save(first_path); left.save(second_path)
    else:
        left.save(first_path); right.save(second_path)
    return [str(first_path), str(second_path)]


def apply_edit(img_path, edit_type, x, y, w, h, color='white', do_backup=True):
    """단일 이미지에 크롭 또는 마스킹 적용"""
    if not HAS_PIL:
        raise RuntimeError("Pillow가 설치되지 않았습니다. pip install Pillow 실행 후 재시작하세요.")
    p = Path(img_path)
    if do_backup:
        backup_image(img_path)
    img = Image.open(p)
    iw, ih = img.size
    px = int(x * iw)
    py = int(y * ih)
    pw = int(w * iw)
    ph = int(h * ih)

    if edit_type == 'crop':
        img = img.crop((px, py, px + pw, py + ph))
    elif edit_type == 'mask':
        draw = ImageDraw.Draw(img)
        fill = (255,255,255) if color == 'white' else (0,0,0)
        draw.rectangle([px, py, px+pw, py+ph], fill=fill)

    fmt = img.format or ('JPEG' if p.suffix.lower() in {'.jpg','.jpeg'} else 'PNG')
    if fmt == 'JPEG' and img.mode == 'RGBA':
        img = img.convert('RGB')
    img.save(p, format=fmt, quality=95)

HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>만화 정리기</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#1a1a1f;color:#e0e0e0;height:100vh;display:flex;flex-direction:column;overflow:hidden}
#topbar{display:flex;align-items:center;gap:10px;padding:10px 16px;background:#111115;border-bottom:1px solid #2a2a35;flex-shrink:0}
#topbar h1{font-size:15px;font-weight:600;color:#fff;white-space:nowrap}
#path-wrap{flex:1;position:relative;display:flex;gap:6px}
#path-input{flex:1;background:#2a2a35;border:1px solid #3a3a45;color:#e0e0e0;padding:6px 10px;border-radius:6px;font-size:13px;outline:none}
#path-input:focus{border-color:#7c6fd4}
#path-dropdown{position:absolute;top:100%;left:0;right:0;background:#1f1f2a;border:1px solid #3a3a45;border-radius:6px;margin-top:3px;z-index:500;display:none;box-shadow:0 4px 16px rgba(0,0,0,0.5);max-height:220px;overflow-y:auto}
#path-dropdown.open{display:block}
.pd-item{padding:8px 12px;font-size:12px;color:#c5bff5;cursor:pointer;display:flex;align-items:center;gap:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pd-item:hover{background:#2d2b4e}
.pd-del{margin-left:auto;color:#555;font-size:11px;padding:2px 6px;flex-shrink:0}
.pd-del:hover{color:#f5bfbf}
.pd-empty{padding:10px 12px;font-size:12px;color:#555;text-align:center}
button{background:#2d2b4e;color:#c5bff5;border:1px solid #4a4580;padding:6px 14px;border-radius:6px;font-size:13px;cursor:pointer;white-space:nowrap;transition:background 0.15s}
button:hover{background:#3d3a60}
button:disabled{opacity:0.4;cursor:not-allowed}
button.danger{background:#4e2b2b;color:#f5bfbf;border-color:#804545}
button.danger:hover{background:#603a3a}
button.primary{background:#4a3d9e;color:#fff;border-color:#6a5abf}
button.primary:hover{background:#5a4dae}
button.success{background:#2b4e35;color:#bff5c8;border-color:#457850}
button.success:hover{background:#3a6045}
button.warn{background:#4e3e2b;color:#f5dbbf;border-color:#806045}
button.warn:hover{background:#604a3a}
#main{display:flex;flex:1;overflow:hidden}
#sidebar{width:260px;flex-shrink:0;background:#111115;border-right:1px solid #2a2a35;display:flex;flex-direction:column;overflow:hidden}
#sidebar-header{padding:8px 12px;border-bottom:1px solid #2a2a35;font-size:12px;color:#888;display:flex;justify-content:space-between;align-items:center;gap:6px}
#tree{flex:1;overflow-y:auto;padding:4px 0}
.title-item{cursor:pointer;padding:6px 12px;font-size:13px;font-weight:500;color:#c5bff5;display:flex;align-items:center;gap:6px;user-select:none}
.title-item:hover{background:#1f1f2a}
.title-arrow{font-size:10px;color:#666;transition:transform 0.15s;display:inline-block;width:12px}
.title-arrow.open{transform:rotate(90deg)}
.chapter-list{display:none}
.chapter-list.open{display:block}
.chapter-item{padding:5px 12px 5px 28px;font-size:12px;color:#aaa;cursor:pointer;display:flex;align-items:center;gap:8px;user-select:none}
.chapter-item:hover{background:#1f1f2a;color:#ddd}
.chapter-item.selected{background:#2d2b4e;color:#c5bff5;border-left:3px solid #7c6fd4;padding-left:25px}
.chapter-item.checking{background:#1e2d1e;color:#7fbf87}
.chapter-item.in-vol{background:#1e3d22;color:#6fd47f;border-left:3px solid #4caf50;padding-left:25px;font-weight:500}
.chapter-item.saved{background:#1a2035;color:#7baaf7;border-left:3px solid #4a7af4;padding-left:25px;font-weight:500}
.ch-count{font-size:10px;color:#666;margin-left:auto}
#content{flex:1;display:flex;flex-direction:column;overflow:hidden}
#toolbar{padding:8px 14px;background:#111115;border-bottom:1px solid #2a2a35;display:flex;align-items:center;gap:8px;flex-wrap:wrap;flex-shrink:0}
#status{font-size:12px;color:#888;flex:1;min-width:100px}
#preview-area{flex:1;overflow:auto;padding:12px}
#preview-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px}
.img-card{background:#1f1f2a;border-radius:8px;overflow:hidden;border:1px solid #2a2a35;cursor:pointer;position:relative}
.img-card:hover{border-color:#4a4580}
.img-card.focused{border-color:#a89ee8;border-width:2px;box-shadow:0 0 0 4px #7c6fd488;background:#3a3560}
.img-card.focused::before{content:'';position:absolute;top:0;left:0;right:0;height:4px;background:linear-gradient(90deg,#7c6fd4,#a89ee8);z-index:1}
.img-card.focused .img-label{color:#fff;font-weight:600;background:#3a3560}
.img-card.excluded{opacity:0.2;filter:grayscale(1);pointer-events:none}
.img-card img{width:100%;aspect-ratio:3/4;object-fit:cover;display:block}
.img-card .img-label{font-size:10px;color:#888;padding:3px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.img-card .edit-badge{position:absolute;top:4px;right:4px;background:#4a3d9e;color:#fff;font-size:9px;padding:2px 5px;border-radius:4px}
#vol-panel{width:250px;flex-shrink:0;background:#111115;border-left:1px solid #2a2a35;display:flex;flex-direction:column;overflow:hidden}
#vol-header{padding:8px 12px;border-bottom:1px solid #2a2a35;font-size:12px;color:#888}
#vol-list{flex:1;overflow-y:auto;padding:8px}
.vol-card{background:#1f1f2a;border:1px solid #2a2a35;border-radius:8px;margin-bottom:8px;padding:10px}
.vol-title{font-size:13px;font-weight:500;color:#c5bff5;margin-bottom:6px;display:flex;justify-content:space-between}
.vol-ch{font-size:11px;color:#888;margin-bottom:4px;max-height:70px;overflow-y:auto}
.vol-ch-item{padding:2px 0;border-bottom:1px solid #222}
.vol-actions{display:flex;gap:6px;margin-top:6px}
.vol-actions button{font-size:11px;padding:4px 8px;flex:1}
#vol-form{padding:10px 12px;border-top:1px solid #2a2a35}
#vol-form input{width:100%;background:#2a2a35;border:1px solid #3a3a45;color:#e0e0e0;padding:6px 10px;border-radius:6px;font-size:13px;outline:none;margin-bottom:6px}
#vol-form input:focus{border-color:#7c6fd4}

/* 편집 모달 */
#edit-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:200;flex-direction:column;align-items:center;justify-content:center;gap:12px}
#edit-modal.open{display:flex}
#edit-topbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:center}
#edit-topbar label{font-size:13px;color:#aaa}
#edit-btnbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:center}
#edit-btnbar label{font-size:13px;color:#aaa}
#canvas-wrap{position:relative;cursor:crosshair;user-select:none;line-height:0}
#edit-canvas{display:block;max-width:90vw;max-height:70vh;object-fit:contain}
#sel-overlay{position:absolute;border:2px dashed #f5c518;background:rgba(245,197,24,0.15);pointer-events:none;display:none}
#edit-info{font-size:12px;color:#aaa;text-align:center;min-height:18px}

::-webkit-scrollbar{width:6px;height:6px}
#scan-overlay{display:none;position:fixed;inset:0;background:rgba(10,10,15,0.92);z-index:1000;flex-direction:column;align-items:center;justify-content:center;gap:16px}
#scan-overlay.open{display:flex}
#scan-spinner{width:48px;height:48px;border:4px solid #2a2a35;border-top-color:#7c6fd4;border-radius:50%;animation:spin 0.8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
#scan-title{font-size:18px;font-weight:600;color:#fff}
#scan-log{font-size:13px;color:#7c6fd4;max-height:200px;overflow:hidden;text-align:center;width:500px}
#scan-log .scan-line{padding:2px 0;color:#aaa;animation:fadein 0.2s}
#scan-log .scan-line.new{color:#c5bff5}
@keyframes fadein{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
#scan-done{font-size:15px;color:#7fbf87;font-weight:500}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#3a3a45;border-radius:3px}
#preview-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.88);z-index:400;align-items:center;justify-content:center}
#preview-modal.open{display:flex}
#preview-box{background:#1a1a1f;border:1px solid #3a3a45;border-radius:12px;width:500px;max-width:95vw;max-height:85vh;display:flex;flex-direction:column;overflow:hidden}
#preview-box h2{font-size:14px;font-weight:500;color:#fff;padding:14px 18px;border-bottom:1px solid #2a2a35;margin:0}
#preview-vol-list{flex:1;overflow-y:auto;padding:8px 0}
.preview-vol-title{font-size:12px;color:#7c6fd4;font-weight:600;padding:6px 16px 4px}
.preview-ch-item{display:flex;align-items:center;gap:10px;padding:6px 16px;font-size:12px;color:#ccc;cursor:grab;user-select:none}
.preview-ch-item:hover{background:#1f1f2a}
.preview-ch-item.drag-over{border-top:2px solid #7c6fd4}
.preview-ch-handle{color:#555;font-size:14px;flex-shrink:0}
.preview-ch-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#preview-footer{padding:12px 18px;border-top:1px solid #2a2a35;display:flex;gap:8px;justify-content:flex-end}
#split-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:400;align-items:center;justify-content:center}
#split-modal.open{display:flex}
#split-box{background:#1a1a1f;border:1px solid #3a3a45;border-radius:12px;width:92vw;max-width:1200px;max-height:92vh;display:flex;flex-direction:column;overflow:hidden}
#split-box h2{font-size:14px;font-weight:500;color:#fff;padding:10px 18px;border-bottom:1px solid #2a2a35;margin:0;display:flex;align-items:center;gap:10px}
.split-controls{padding:8px 18px;border-bottom:1px solid #2a2a35;display:flex;align-items:center;gap:14px;flex-wrap:wrap;font-size:12px;color:#aaa}
.split-controls label{display:flex;align-items:center;gap:5px;cursor:pointer}
#split-global-x{accent-color:#7c6fd4;width:100px}
#split-grid{flex:1;overflow-y:auto;padding:10px;display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.split-card{background:#1f1f2a;border:1px solid #2a2a35;border-radius:6px;overflow:hidden}
.split-img-wrap{position:relative;cursor:col-resize;user-select:none;line-height:0}
.split-img-wrap img{width:100%;display:block;pointer-events:none}
.split-line{position:absolute;top:0;bottom:0;width:3px;background:rgba(245,197,24,0.9);transform:translateX(-1px)}
.split-card.adjusted .split-line{background:rgba(124,111,212,0.9)}
.split-card-name{font-size:10px;color:#888;padding:3px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.split-card.adjusted .split-card-name{color:#a89ee8}
#split-footer{padding:10px 18px;border-top:1px solid #2a2a35;display:flex;justify-content:space-between;align-items:center}
#split-footer-info{font-size:11px;color:#666}
#rename-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.88);z-index:300;align-items:center;justify-content:center}
#rename-modal.open{display:flex}
#rename-box{background:#1a1a1f;border:1px solid #3a3a45;border-radius:12px;width:700px;max-width:95vw;max-height:85vh;display:flex;flex-direction:column;overflow:hidden}
#rename-header{padding:14px 18px;border-bottom:1px solid #2a2a35;display:flex;align-items:center;gap:10px}
#rename-header h2{font-size:14px;font-weight:500;color:#fff;flex:1;margin:0}
#rename-controls{padding:12px 18px;border-bottom:1px solid #2a2a35;display:flex;flex-direction:column;gap:8px}
.rename-row{display:flex;align-items:center;gap:8px;font-size:13px}
.rename-row label{color:#aaa;width:80px;flex-shrink:0}
.rename-row input{flex:1;background:#2a2a35;border:1px solid #3a3a45;color:#e0e0e0;padding:5px 10px;border-radius:6px;font-size:13px;outline:none}
.rename-row input:focus{border-color:#7c6fd4}
.rename-row select{background:#2a2a35;border:1px solid #3a3a45;color:#e0e0e0;padding:5px 8px;border-radius:6px;font-size:13px;outline:none}
#rename-preview{flex:1;overflow-y:auto;padding:10px 18px}
.rename-item{display:flex;align-items:center;gap:10px;padding:5px 0;border-bottom:1px solid #1f1f28;font-size:12px}
.rename-old{color:#888;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rename-arrow{color:#555;flex-shrink:0}
.rename-new{color:#c5bff5;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
.rename-footer{padding:12px 18px;border-top:1px solid #2a2a35;display:flex;gap:8px;justify-content:flex-end}
</style>
</head>
<body>

<div id="topbar">
  <h1>📚 만화 정리기</h1>
  <div id="path-wrap">
    <input id="path-input" type="text" placeholder="만화 폴더 경로 (예: D:\만화작업중\작품명)" autocomplete="off"
      onfocus="openDropdown()" onkeydown="if(event.key==='Enter')loadFolder();if(event.key==='Escape')closeDropdown()" />
    <div id="path-dropdown"></div>
    <button onclick="browseFolder()" title="폴더 탐색기 열기">📂</button>
  </div>
  <button onclick="loadFolder()" id="btn-open" title="">열기</button>
  <button class="danger" onclick="shutdownApp()" style="margin-left:4px" title="서버 종료">⏹ 종료</button>
</div>

<div id="main">
  <div id="sidebar">
    <div id="sidebar-header">
      <span id="tree-info">폴더를 입력하세요</span>
      <button style="font-size:11px;padding:3px 8px" onclick="expandAll()">전체 펼치기</button>
    </div>
    <div id="tree"></div>
  </div>

  <div id="content">
    <div id="toolbar">
      <span id="status">화를 선택하면 이미지가 표시됩니다</span>
      <button onclick="selectAllVisible()">전체 선택</button>
      <button onclick="clearSelection()">선택 해제</button>
      <button onclick="openRename()">✏ 파일명 변경</button>
      <button onclick="openSplitModal(false)" title="현재 화 전체 양면 분리">✂ 양면 분리</button>
      <button class="primary" onclick="addToVolume()">→ 권에 추가</button>
    </div>
    <div id="preview-area">
      <div id="empty-msg" style="text-align:center;color:#555;padding:40px;font-size:13px">왼쪽에서 화를 클릭하면 이미지가 표시됩니다</div>
      <div id="preview-grid"></div>
    </div>
  </div>

  <div id="vol-panel">
    <div id="vol-header" style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;border-bottom:1px solid #2a2a35;font-size:12px;color:#888">
      <span>📦 권 구성</span>
      <button style="font-size:11px;padding:3px 8px" onclick="openFolderPdf()">📄 폴더→PDF</button>
    </div>
    <div id="vol-list"><div style="text-align:center;color:#555;padding:20px;font-size:12px">아직 권이 없습니다</div></div>
    <div id="vol-form">
      <input id="vol-name" placeholder="권 이름 (예: 1권)" />
      <div style="display:flex;gap:4px;margin-bottom:6px">
        <span id="vol-dest-label" style="flex:1;background:#1a1a1f;border:1px solid #2a2a35;color:#888;padding:6px 10px;border-radius:6px;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:flex;align-items:center" title="">저장 경로 미설정</span>
        <button style="padding:6px 10px;font-size:12px;flex-shrink:0" onclick="browseDestFolder()" title="저장 폴더 선택">📂</button>
      </div>
      <input id="vol-dest" type="hidden" />
      <div style="display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-size:12px;color:#aaa;margin-bottom:4px">
        <label style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="chk-folder" checked style="width:13px;height:13px"> 폴더</label>
        <label style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="chk-pdf" style="width:13px;height:13px"> PDF</label>
        <label style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="chk-cbz" style="width:13px;height:13px"> CBZ</label>
        <label style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="chk-zip" style="width:13px;height:13px"> ZIP</label>
      </div>
      <div style="border-top:1px solid #2a2a35;padding-top:6px;margin-bottom:6px">
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:#aaa;margin-bottom:4px">
          <input type="checkbox" id="chk-compress" style="width:13px;height:13px" onchange="toggleCompress()">
          PNG → JPG 변환 (용량 절감)
        </label>
        <div id="compress-opts" style="display:none;padding-left:4px">
          <div style="display:flex;align-items:center;gap:8px;font-size:12px;color:#888">
            <span>품질</span>
            <input type="range" id="jpg-quality" min="60" max="95" value="85" step="5"
              style="flex:1;accent-color:#7c6fd4" oninput="document.getElementById('quality-val').textContent=this.value">
            <span id="quality-val" style="color:#c5bff5;width:24px;text-align:right">85</span>
          </div>
        </div>
      </div>
      <button class="success" style="width:100%" onclick="createVolume()" id="btn-save-vol">📁 저장</button>
    </div>
  </div>
</div>

<!-- 스캔 오버레이 -->
<div id="scan-overlay">
  <div id="scan-spinner"></div>
  <div id="scan-title">🔍 스캔 중...</div>
  <div id="scan-log"></div>
  <div id="scan-done"></div>
</div>

<!-- 저장 미리보기 모달 -->
<div id="preview-modal">
  <div id="preview-box">
    <h2>📦 저장 미리보기 — 드래그로 순서 변경 가능</h2>
    <div id="preview-vol-list"></div>
    <div id="preview-footer">
      <button onclick="closePreviewModal()">취소</button>
      <button class="success" onclick="confirmSave()">✅ 저장</button>
    </div>
  </div>
</div>

<!-- 양면 분리 모달 -->
<div id="split-modal">
  <div id="split-box">
    <h2>✂ 양면 이미지 분리 <span style="font-size:11px;color:#555;font-weight:400">노란선 드래그로 개별 조정 | 보라선=개별조정됨</span></h2>
    <div class="split-controls">
      <label><input type="radio" name="split-dir" value="ltr" checked> 좌→우 a|b (한국/서양)</label>
      <label><input type="radio" name="split-dir" value="rtl"> 좌←우 b|a (일본)</label>
      <span>|</span>
      <span>기준선:</span>
      <input type="range" id="split-global-x" min="20" max="80" value="50" step="1" oninput="applyGlobalSplitX(this.value)">
      <span id="split-global-val" style="color:#c5bff5;min-width:28px">50%</span>
      <button style="font-size:11px;padding:2px 8px" onclick="resetSplitLines()">초기화</button>
      <span>|</span>
      <span id="split-target-info" style="color:#888"></span>
    </div>
    <div id="split-grid"></div>
    <div id="split-footer">
      <span id="split-footer-info"></span>
      <div style="display:flex;gap:8px">
        <button onclick="closeSplitModal()">취소</button>
        <button class="success" onclick="applySplit()">✅ 분리 실행</button>
      </div>
    </div>
  </div>
</div>

<!-- 파일명 변경 모달 -->
<div id="rename-modal">
  <div id="rename-box">
    <div id="rename-header">
      <h2>✏ 파일명 일괄 변경</h2>
      <span id="rename-chapter-name" style="font-size:12px;color:#888"></span>
    </div>
    <div id="rename-controls">
      <div class="rename-row">
        <label>접두사</label>
        <input id="rn-prefix" placeholder="예: 파일명_1화_" oninput="updatePreview()" />
      </div>
      <div class="rename-row">
        <label>번호 자릿수</label>
        <select id="rn-digits" onchange="updatePreview()">
          <option value="2">2자리 (01, 02...)</option>
          <option value="3" selected>3자리 (001, 002...)</option>
          <option value="4">4자리 (0001, 0002...)</option>
        </select>
        <label style="margin-left:16px;width:auto">시작 번호</label>
        <input id="rn-start" type="number" value="1" min="1" style="width:70px" oninput="updatePreview()" />
      </div>
      <div class="rename-row">
        <label>미리보기</label>
        <span id="rn-sample" style="color:#c5bff5;font-size:13px;font-family:monospace"></span>
      </div>
    </div>
    <div id="rename-preview"></div>
    <div class="rename-footer">
      <button onclick="closeRename()">취소</button>
      <button class="success" onclick="applyRename()">✅ 적용</button>
    </div>
  </div>
</div>

<!-- 편집 모달 -->
<div id="edit-modal">
  <div id="edit-topbar">
    <button onclick="editNavigate(-1)" title="이전 이미지 (←)">◀</button>
    <span style="color:#fff;font-size:14px;font-weight:500" id="edit-filename"></span>
    <span style="color:#888;font-size:12px" id="edit-counter"></span>
    <span style="color:#666;font-size:12px" id="edit-imgsize"></span>
    <button onclick="editNavigate(1)" title="다음 이미지 (→)">▶</button>
  </div>
  <div id="edit-btnbar">
    <label>모드:</label>
    <button id="btn-crop" class="primary" onclick="setMode('crop')">✂ 크롭</button>
    <button id="btn-mask-w" onclick="setMode('mask-white')">⬜ 흰색 마스킹</button>
    <button id="btn-mask-b" onclick="setMode('mask-black')">⬛ 검정 마스킹</button>
    <span style="color:#555">|</span>
    <button class="warn" onclick="applyEdit(false)">이 이미지에만 적용</button>
    <button class="success" onclick="applyEdit(true)">📋 현재 화 전체에 적용</button>
    <span style="color:#555">|</span>
    <button onclick="doRestore(false)">↩ 복원</button>
    <button onclick="doRestore(true)">↩ 화 전체 복원</button>
    <button id="btn-split-mode" onclick="toggleSplitMode()">✂ 분리선</button>
    <span id="split-dir-wrap" style="display:none;align-items:center;gap:6px;font-size:12px;color:#aaa">
      <label style="display:flex;align-items:center;gap:3px;cursor:pointer"><input type="radio" name="edit-split-dir" value="ltr" checked> 좌→우 a|b</label>
      <label style="display:flex;align-items:center;gap:3px;cursor:pointer"><input type="radio" name="edit-split-dir" value="rtl"> 좌←우 b|a</label>
    </span>
    <button id="btn-split-exec" style="display:none" class="warn" onclick="executeSplit()">✂ 분리 실행</button>
    <button id="btn-split-restore" style="display:none" class="danger" onclick="doSplitRestore()">↩ 분리 복원</button>
    <button class="danger" onclick="closeEdit()">닫기</button>
  </div>
  <div id="backup-status" style="font-size:11px;color:#7fbf87;text-align:center;height:18px;padding:2px 0"></div>
  <div id="canvas-wrap">
    <img id="edit-canvas" draggable="false" />
    <div id="sel-overlay"></div>
    <div id="split-vline" style="display:none;position:absolute;top:0;bottom:0;width:7px;background:rgba(245,197,24,0.9);cursor:col-resize;pointer-events:auto;transform:translateX(-3px);z-index:10"></div>
  </div>
  <div id="edit-info">이미지를 드래그해서 영역을 선택하세요</div>
  <div style="font-size:11px;color:#444;text-align:center;margin-top:4px">
    <kbd style="background:#2a2a35;border:1px solid #3a3a45;border-radius:3px;padding:1px 5px">C</kbd> 크롭 &nbsp;
    <kbd style="background:#2a2a35;border:1px solid #3a3a45;border-radius:3px;padding:1px 5px">W</kbd> 흰색마스크 &nbsp;
    <kbd style="background:#2a2a35;border:1px solid #3a3a45;border-radius:3px;padding:1px 5px">B</kbd> 검정마스크 &nbsp;
    <kbd style="background:#2a2a35;border:1px solid #3a3a45;border-radius:3px;padding:1px 5px">Ctrl+S</kbd> 적용 &nbsp;
    <kbd style="background:#2a2a35;border:1px solid #3a3a45;border-radius:3px;padding:1px 5px">Ctrl+Shift+S</kbd> 전체적용 &nbsp;
    <kbd style="background:#2a2a35;border:1px solid #3a3a45;border-radius:3px;padding:1px 5px">Ctrl+Z</kbd> 실행취소 &nbsp;
    <kbd style="background:#2a2a35;border:1px solid #3a3a45;border-radius:3px;padding:1px 5px">←→</kbd> 이전/다음 &nbsp;
    <kbd style="background:#2a2a35;border:1px solid #3a3a45;border-radius:3px;padding:1px 5px">Esc</kbd> 닫기
  </div>
</div>

<script>
let treeData = [];
let selectedChapters = new Set();
let volumes = [];
let currentChapterPath = '';
const _chapterCache = {};  // {path: {images, gridHTML}}
const _thumbCache = {};    // {imgPath: dataURL}
let lastClickedChapter = null;
let dragStartChapter = null;
let dragStartEl = null;
let isDraggingChapter = false;

function getChapterItems() {
  return [...document.querySelectorAll('.chapter-item')];
}

function selectChapter(path, el) {
  selectedChapters.add(path);
  if (el) el.classList.add('checking');
}

function deselectChapter(path, el) {
  selectedChapters.delete(path);
  if (el) el.classList.remove('checking');
}

function toggleChapterSelect(path, el) {
  if (selectedChapters.has(path)) {
    deselectChapter(path, el);
  } else {
    selectChapter(path, el);
  }
  updateSelectionStatus();
}

function clearChapterSelection() {
  selectedChapters.clear();
  document.querySelectorAll('.chapter-item.checking').forEach(el => el.classList.remove('checking'));
}

function rangeSelect(fromPath, toPath) {
  const items = getChapterItems();
  const fromIdx = items.findIndex(el => el.dataset.path === fromPath);
  const toIdx   = items.findIndex(el => el.dataset.path === toPath);
  if (fromIdx === -1 || toIdx === -1) return;
  const [start, end] = fromIdx < toIdx ? [fromIdx, toIdx] : [toIdx, fromIdx];
  clearChapterSelection();
  items.slice(start, end + 1).forEach(el => selectChapter(el.dataset.path, el));
  updateSelectionStatus();
}

function rangeDragSelect(fromPath, toPath) {
  const items = getChapterItems();
  const fromIdx = items.findIndex(el => el.dataset.path === fromPath);
  const toIdx   = items.findIndex(el => el.dataset.path === toPath);
  if (fromIdx === -1 || toIdx === -1) return;
  const [start, end] = fromIdx < toIdx ? [fromIdx, toIdx] : [toIdx, fromIdx];
  items.forEach(el => el.classList.remove('checking'));
  selectedChapters.clear();
  items.slice(start, end + 1).forEach(el => selectChapter(el.dataset.path, el));
  updateSelectionStatus();
}

function updateSelectionStatus() {
  if (selectedChapters.size > 0) {
    document.getElementById('status').textContent = selectedChapters.size + '개 화 선택됨';
  }
}
let currentChapterImages = [];
let editMode = 'crop';
let editImgPath = '';
let selStart = null, selRect = null;
let isDragging = false;

// ── 최근 경로 관리 ──
const RECENT_KEY = 'manga_recent_paths';
const RECENT_MAX = 10;

function getRecentPaths() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]'); } catch { return []; }
}
function saveRecentPath(path) {
  let list = getRecentPaths().filter(p => p !== path);
  list.unshift(path);
  if (list.length > RECENT_MAX) list = list.slice(0, RECENT_MAX);
  localStorage.setItem(RECENT_KEY, JSON.stringify(list));
}
function deleteRecentPath(path) {
  const list = getRecentPaths().filter(p => p !== path);
  localStorage.setItem(RECENT_KEY, JSON.stringify(list));
}

function openDropdown() {
  const list = getRecentPaths();
  const dd = document.getElementById('path-dropdown');
  dd.innerHTML = '';
  if (!list.length) {
    dd.innerHTML = '<div class="pd-empty">최근 경로 없음</div>';
  } else {
    list.forEach(p => {
      const el = document.createElement('div');
      el.className = 'pd-item';
      el.innerHTML = '📁 ' + esc(p) + '<span class="pd-del" title="삭제" onclick="event.stopPropagation();delRecent(\'' + esc(p).replace(/\\/g,'\\\\') + '\')">✕</span>';
      el.onclick = () => { document.getElementById('path-input').value = p; closeDropdown(); loadFolder(); };
      dd.appendChild(el);
    });
  }
  dd.classList.add('open');
  setTimeout(() => document.addEventListener('click', outsideClick), 0);
}
function closeDropdown() {
  document.getElementById('path-dropdown').classList.remove('open');
  document.removeEventListener('click', outsideClick);
}
function outsideClick(e) {
  if (!document.getElementById('path-wrap').contains(e.target)) closeDropdown();
}
function delRecent(path) {
  deleteRecentPath(path);
  openDropdown();
}

async function browseFolder() {
  const current = document.getElementById('path-input').value.trim();
  const res = await fetch('/api/browse?initial=' + encodeURIComponent(current));
  const data = await res.json();
  if (data.error) { alert('폴더 선택 오류: ' + data.error); return; }
  if (data.path) {
    document.getElementById('path-input').value = data.path;
    loadFolder();
  }
}

async function loadFolder() {
  const path = document.getElementById('path-input').value.trim();
  if (!path) return;
  closeDropdown();
  saveRecentPath(path);  // 열기 누르는 즉시 저장

  // 오버레이 열기
  const overlay = document.getElementById('scan-overlay');
  const logEl   = document.getElementById('scan-log');
  const titleEl = document.getElementById('scan-title');
  const doneEl  = document.getElementById('scan-done');
  logEl.innerHTML = ''; doneEl.textContent = '';
  titleEl.textContent = '🔍 스캔 중...';
  overlay.classList.add('open');

  let finalResult = null;

  await new Promise((resolve) => {
    const es = new EventSource('/api/scan?path=' + encodeURIComponent(path));
    es.onmessage = (e) => {
      const d = JSON.parse(e.data);
      if (d.error) {
        es.close();
        overlay.classList.remove('open');
        alert(d.error);
        resolve();
      } else if (d.status === 'start') {
        titleEl.textContent = '🔍 ' + d.title + ' 스캔 중...';
      } else if (d.status === 'chapter') {
        const line = document.createElement('div');
        line.className = 'scan-line new';
        line.textContent = '✓ ' + d.chapter + '  (' + d.count + 'p)';
        logEl.appendChild(line);
        // 최근 5줄만 표시
        const lines = logEl.querySelectorAll('.scan-line');
        lines.forEach((l, i) => { l.classList.remove('new'); if (i < lines.length - 5) l.style.opacity = '0.3'; });
        logEl.scrollTop = logEl.scrollHeight;
      } else if (d.status === 'done') {
        finalResult = d.result;
        es.close();
        resolve();
      }
    };
    es.onerror = () => { es.close(); overlay.classList.remove('open'); alert('스캔 중 오류가 발생했습니다.'); resolve(); };
  });

  if (!finalResult) return;

  // 완료 표시 잠깐 보여주기
  titleEl.textContent = '✅ 스캔 완료';
  doneEl.textContent = finalResult.reduce((s, t) => s + t.chapters.length, 0) + '개 화 발견';
  await new Promise(r => setTimeout(r, 600));
  overlay.classList.remove('open');

  treeData = finalResult;
  Object.keys(_chapterCache).forEach(k => delete _chapterCache[k]);
  // 새 폴더 열 때 전체 초기화
  volumes = [];
  currentChapterPath = '';
  currentChapterImages = [];
  selectedChapters.clear();
  lastClickedChapter = null;
  renderVolumes();
  document.getElementById('vol-name').value = '';
  document.getElementById('preview-grid').innerHTML = '';
  document.getElementById('empty-msg').style.display = 'block';
  document.getElementById('status').textContent = '화를 선택하면 이미지가 표시됩니다';
  renderTree();
  document.getElementById('tree-info').textContent = finalResult.length + '개 작품';
  const btnOpen = document.getElementById('btn-open');
  if (btnOpen) btnOpen.title = path;
  // 작품이 1개면 자동으로 펼치기
  if (finalResult.length === 1) {
    const firstList = document.getElementById('chl-0');
    const firstArrow = document.getElementById('arr-0');
    if (firstList) firstList.classList.add('open');
    if (firstArrow) firstArrow.classList.add('open');
  }
  const savePath = path + '\\[권정리]';
  setDestPath(savePath);
  // 기존 권정리 폴더 자동 불러오기
  loadExistingVolumes(savePath);
}

// 시작 시 마지막 경로 복원
(async () => {
  try {
    const res = await fetch('/api/lastpath');
    const data = await res.json();
    if (data.path) document.getElementById('path-input').value = data.path;
  } catch {}
})();

function renderTree() {
  const tree = document.getElementById('tree');
  tree.innerHTML = '';
  treeData.forEach((title, ti) => {
    const titleEl = document.createElement('div');
    titleEl.className = 'title-item';
    titleEl.innerHTML = `<span class="title-arrow" id="arr-${ti}">▶</span> 📁 ${esc(title.title)} <span style="font-size:10px;color:#666;margin-left:auto">${title.chapters.length}화</span>`;
    titleEl.onclick = () => { document.getElementById('chl-'+ti).classList.toggle('open'); document.getElementById('arr-'+ti).classList.toggle('open'); };
    tree.appendChild(titleEl);
    const chList = document.createElement('div');
    chList.className = 'chapter-list'; chList.id = 'chl-'+ti;
    title.chapters.forEach((ch, ci) => {
      const el = document.createElement('div');
      el.className = 'chapter-item'; el.id = `ch-${ti}-${ci}`; el.dataset.path = ch.path;
      el.innerHTML = `${esc(ch.name)} <span class="ch-count">${ch.count}p</span>`;
      el.onclick = (e) => {
        if (e.shiftKey && lastClickedChapter !== null) {
          // Shift+클릭: 범위 선택
          rangeSelect(lastClickedChapter, ch.path);
        } else if (e.ctrlKey || e.metaKey) {
          // Ctrl+클릭: 개별 토글
          toggleChapterSelect(ch.path, el);
        } else {
          // 일반 클릭: 단일 선택 + 미리보기
          if (!isDraggingChapter) {
            clearChapterSelection();
            selectChapter(ch.path, el);
            previewChapter(ch.path, ch.name);
          }
        }
        lastClickedChapter = ch.path;
        isDraggingChapter = false;
      };
      el.onmousedown = (e) => {
        if (e.button !== 0) return;
        dragStartChapter = ch.path;
        dragStartEl = el;
        isDraggingChapter = false;
      };
      el.onmouseenter = (e) => {
        if (dragStartChapter && e.buttons === 1) {
          isDraggingChapter = true;
          clearChapterSelection();
          rangeDragSelect(dragStartChapter, ch.path);
        }
      };
      el.dataset.title = ti;
      el.dataset.chidx = ci;
      chList.appendChild(el);
    });
    tree.appendChild(chList);
  });
}

function expandAll() {
  document.querySelectorAll('.chapter-list').forEach(el => el.classList.add('open'));
  document.querySelectorAll('.title-arrow').forEach(el => el.classList.add('open'));
}

function toggleCheck(cb, path) {
  if (cb.checked) selectedChapters.add(path); else selectedChapters.delete(path);
  document.getElementById('status').textContent = selectedChapters.size + '개 화 선택됨';
}

function selectAllVisible() {
  clearChapterSelection();
  document.querySelectorAll('.chapter-item').forEach(el => selectChapter(el.dataset.path, el));
  updateSelectionStatus();
}

function clearSelection() {
  clearChapterSelection();
  document.getElementById('status').textContent = '선택 해제됨';
}

async function previewChapter(path, name) {
  currentChapterPath = path;
  // 사이드바 선택 화 하이라이트
  document.querySelectorAll('.chapter-item').forEach(el => el.classList.remove('selected'));
  const selEl = document.querySelector(`.chapter-item[data-path="${path.replace(/\\/g,'\\\\').replace(/"/g,'\\"')}"]`);
  if (selEl) {
    selEl.classList.add('selected');
    selEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  document.getElementById('empty-msg').style.display = 'none';
  const grid = document.getElementById('preview-grid');

  // ── 캐시 히트: 저장된 그리드 HTML 즉시 복원 ──
  if (_chapterCache[path]) {
    const cache = _chapterCache[path];
    currentChapterImages = cache.images;
    grid.innerHTML = cache.html;
    // 이벤트 재등록
    grid.querySelectorAll('.img-card').forEach((card, i) => {
      const img = cache.images[i];
      if (!img) return;
      card.addEventListener('dblclick', (e) => { e.preventDefault(); e.stopPropagation(); openEdit(img.path, img.name); });
      card.addEventListener('click', (e) => {
        e.preventDefault();
        if (document.getElementById('edit-modal').classList.contains('open')) return;
        document.querySelectorAll('.img-card.focused').forEach(c => c.classList.remove('focused'));
        card.classList.add('focused');
      });
    });
    document.getElementById('status').textContent = '"' + name + '" — ' + cache.images.length + '페이지 (더블클릭: 편집 | ←→: 이동 | Del: 제외)';
    return;
  }

  document.getElementById('status').textContent = '"' + name + '" 로딩 중...';
  grid.innerHTML = '<div style="color:#555;padding:20px;font-size:13px">로딩 중...</div>';
  let data;
  try {
    const res = await fetch('/api/images?path=' + encodeURIComponent(path));
    data = await res.json();
  } catch(e) {
    if (currentChapterPath === path) grid.innerHTML = '<div style="color:#f88;padding:20px;font-size:13px">로딩 오류: ' + e.message + '</div>';
    return;
  }
  if (currentChapterPath !== path) return;
  if (data.error || !data.images) {
    grid.innerHTML = '<div style="color:#f88;padding:20px;font-size:13px">오류: ' + (data.error || '이미지 없음') + '</div>';
    return;
  }
  currentChapterImages = data.images;
  grid.innerHTML = '';

  // 카드 먼저 생성 (플레이스홀더)
  const cardMap = {};
  data.images.forEach(img => {
    const card = document.createElement('div');
    card.className = 'img-card';
    card.dataset.path = img.path;
    const thumbSrc = _thumbCache[img.path] || null;
    card.innerHTML = `<img src="${thumbSrc || 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs='}" style="${thumbSrc ? '' : 'opacity:0'}"><div class="img-label">${esc(img.name)}</div>`;
    cardMap[img.path] = card;
    card.addEventListener('dblclick', (e) => {
      e.preventDefault();
      e.stopPropagation();
      openEdit(img.path, img.name);
    });
    card.addEventListener('click', (e) => {
      e.preventDefault();
      if (document.getElementById('edit-modal').classList.contains('open')) return;
      document.querySelectorAll('.img-card.focused').forEach(c => c.classList.remove('focused'));
      card.classList.add('focused');
    });
    grid.appendChild(card);
  });

  // 이전 배치 요청 취소 후 새 요청 시작
  if (window._thumbAbort) window._thumbAbort.abort();
  const abort = new AbortController();
  window._thumbAbort = abort;
  const myPath = path; // 현재 화 경로 캡처

  // 캐시에 없는 이미지 10장씩 배치 요청
  const uncached = data.images.filter(img => !_thumbCache[img.path]);
  const BATCH = 10;
  for (let i = 0; i < uncached.length; i += BATCH) {
    // 화가 바뀌었으면 중단
    if (abort.signal.aborted || currentChapterPath !== myPath) break;
    const batch = uncached.slice(i, i + BATCH);
    try {
      const tr = await fetch('/api/thumbs?paths=' + encodeURIComponent(batch.map(x=>x.path).join('|')), {signal: abort.signal});
      if (abort.signal.aborted || currentChapterPath !== myPath) break;
      const thumbs = await tr.json();
      Object.assign(_thumbCache, thumbs);
      // 현재 표시 중인 카드에만 적용
      batch.forEach(img => {
        const thumbSrc = thumbs[img.path];
        if (!thumbSrc) return;
        // cardMap이 현재 화 것인지 확인
        const card = document.querySelector(`.img-card[data-path="${img.path.replace(/\\/g,'\\\\')}"]`);
        if (card) {
          const el = card.querySelector('img');
          if (el) { el.src = thumbSrc; el.style.opacity = '1'; }
        }
      });
    } catch(e) { if (e.name === 'AbortError') break; }
  }

  // 배치 완료 후 캐시 저장
  if (currentChapterPath === myPath) {
    _chapterCache[path] = { images: data.images, html: grid.innerHTML };
  }
  document.getElementById('status').textContent = '"' + name + '" — ' + data.images.length + '페이지 (더블클릭: 편집 | ←→: 이동 | Del: 제외)';
}

function addToVolume() {
  if (!selectedChapters.size) { alert('화를 먼저 체크박스로 선택하세요'); return; }
  const name = document.getElementById('vol-name').value.trim() || (volumes.length + 1) + '권';
  volumes.push({ name, chapters: [...selectedChapters] });
  renderVolumes();
  autoSetNextVolName();
  // 권 목록 맨 아래로 스크롤
  const volList = document.getElementById('vol-list');
  if (volList) volList.scrollTop = volList.scrollHeight;
  selectedChapters.forEach(path => {
    document.querySelectorAll('.chapter-item').forEach(el => {
      if (el.dataset.path === path) {
        el.classList.remove('checking');
        el.classList.add('in-vol');
      }
    });
  });
  clearChapterSelection();
}

function renderVolumes() {
  const list = document.getElementById('vol-list');
  list.innerHTML = '';
  if (!volumes.length) { list.innerHTML = '<div style="text-align:center;color:#555;padding:20px;font-size:12px">아직 권이 없습니다</div>'; return; }
  volumes.forEach((vol, vi) => {
    const card = document.createElement('div'); card.className = 'vol-card';
    const isSaved = !!vol._saved;
    card.innerHTML = `<div class="vol-title">
        <span style="display:flex;align-items:center;gap:6px;flex:1;min-width:0">
          ${isSaved ? '✅' : '📦'}
          <span class="vol-name-text" ${isSaved ? '' : `onclick="startRenameVol(${vi}, this)" title="클릭하여 이름 변경"`}
            style="${isSaved ? '' : 'cursor:pointer;'}flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(vol.name)}</span>
        </span>
        <span style="font-size:10px;color:#666;flex-shrink:0">${isSaved ? vol._count + 'p' : vol.chapters.length + '화'}</span>
      </div>
      ${isSaved
        ? `<div style="font-size:11px;color:#555;padding:4px 0 6px">이미 저장된 권입니다</div><div style="margin-bottom:4px"><button style="font-size:11px;padding:3px 8px;width:100%" onclick="openSavedVol(${vi})">📂 열어서 작업</button></div>`
        : `<div class="vol-ch">${vol.chapters.map(p=>`<div class="vol-ch-item" onclick="jumpToChapter('${p.replace(/\\/g,'\\\\').replace(/'/g,"\\'")}')" style="cursor:pointer" title="클릭하여 이동">${esc(p.split('\\').pop()||p.split('/').pop())}</div>`).join('')}</div>`
      }
      <div class="vol-actions"><button class="danger" onclick="removeVol(${vi})">목록에서 제거</button></div>`;
    list.appendChild(card);
  });
}

function startRenameVol(vi, el) {
  const oldName = volumes[vi].name;
  const input = document.createElement('input');
  input.value = oldName;
  input.style.cssText = 'flex:1;background:#2a2a35;border:1px solid #7c6fd4;color:#e0e0e0;padding:2px 6px;border-radius:4px;font-size:13px;outline:none;min-width:0';
  el.replaceWith(input);
  input.focus();
  input.select();
  const apply = () => {
    const newName = input.value.trim() || oldName;
    volumes[vi].name = newName;
    renderVolumes();
  };
  input.onblur = apply;
  input.onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); apply(); }
    if (e.key === 'Escape') { volumes[vi].name = oldName; renderVolumes(); }
  };
}

function removeVol(vi) { volumes.splice(vi,1); renderVolumes(); }

function openSavedVol(vi) {
  const vol = volumes[vi];
  if (!vol || !vol._saved || !vol.path) return;
  // 저장된 권 폴더를 가운데 그리드에 로드
  const folderPath = vol.path;
  const name = vol.name;
  currentChapterPath = folderPath;
  document.getElementById('empty-msg').style.display = 'none';
  document.getElementById('status').textContent = '"' + name + '" 로딩 중...';
  const grid = document.getElementById('preview-grid');
  grid.innerHTML = '';

  // 캐시 확인
  if (_chapterCache[folderPath]) {
    const cache = _chapterCache[folderPath];
    currentChapterImages = cache.images;
    grid.innerHTML = cache.html;
    grid.querySelectorAll('.img-card').forEach((card, i) => {
      const img = cache.images[i];
      if (!img) return;
      card.addEventListener('dblclick', (e) => { e.preventDefault(); e.stopPropagation(); openEdit(img.path, img.name); });
      card.addEventListener('click', (e) => {
        e.preventDefault();
        if (document.getElementById('edit-modal').classList.contains('open')) return;
        document.querySelectorAll('.img-card.focused').forEach(c => c.classList.remove('focused'));
        card.classList.add('focused');
      });
      card.addEventListener('mousedown', () => {
        document.querySelectorAll('.img-card.focused').forEach(c => c.classList.remove('focused'));
        card.classList.add('focused');
      });
    });
    document.getElementById('status').textContent = '"' + name + '" — ' + cache.images.length + '페이지 (더블클릭: 편집 | ←→: 이동 | Del: 제외)';
    return;
  }

  fetch('/api/images?path=' + encodeURIComponent(folderPath))
    .then(r => r.json())
    .then(async data => {
      currentChapterImages = data.images;
      const cardMap = {};
      data.images.forEach(img => {
        const card = document.createElement('div');
        card.className = 'img-card';
        card.dataset.path = img.path;
        const thumbSrc = _thumbCache[img.path] || null;
        card.innerHTML = `<img src="${thumbSrc || 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs='}" style="${thumbSrc ? '' : 'opacity:0'}"><div class="img-label">${esc(img.name)}</div>`;
        cardMap[img.path] = card;
        card.addEventListener('dblclick', (e) => { e.preventDefault(); e.stopPropagation(); openEdit(img.path, img.name); });
        card.addEventListener('click', (e) => {
          e.preventDefault();
          if (document.getElementById('edit-modal').classList.contains('open')) return;
          document.querySelectorAll('.img-card.focused').forEach(c => c.classList.remove('focused'));
          card.classList.add('focused');
        });
        card.addEventListener('mousedown', () => {
          document.querySelectorAll('.img-card.focused').forEach(c => c.classList.remove('focused'));
          card.classList.add('focused');
        });
        grid.appendChild(card);
      });

      if (window._thumbAbort) window._thumbAbort.abort();
      const abort = new AbortController();
      window._thumbAbort = abort;
      const myPath = folderPath;
      const uncached = data.images.filter(img => !_thumbCache[img.path]);
      const BATCH = 10;
      for (let i = 0; i < uncached.length; i += BATCH) {
        if (abort.signal.aborted || currentChapterPath !== myPath) break;
        const batch = uncached.slice(i, i + BATCH);
        try {
          const tr = await fetch('/api/thumbs?paths=' + encodeURIComponent(batch.map(x=>x.path).join('|')), {signal: abort.signal});
          if (abort.signal.aborted || currentChapterPath !== myPath) break;
          const thumbs = await tr.json();
          Object.assign(_thumbCache, thumbs);
          batch.forEach(img => {
            const thumbSrc = thumbs[img.path];
            if (!thumbSrc) return;
            const card = cardMap[img.path];
            if (card) { const el = card.querySelector('img'); if (el) { el.src = thumbSrc; el.style.opacity = '1'; } }
          });
        } catch(e) { if (e.name === 'AbortError') break; }
      }
      if (currentChapterPath === myPath) {
        _chapterCache[folderPath] = { images: data.images, html: grid.innerHTML };
      }
      document.getElementById('status').textContent = '"' + name + '" — ' + data.images.length + '페이지 (더블클릭: 편집 | ←→: 이동 | Del: 제외)';
    });
}


function jumpToChapter(path) {
  // 사이드바에서 해당 화 찾아서 클릭
  const items = document.querySelectorAll('.chapter-item');
  for (const el of items) {
    if (el.dataset.path === path) {
      // 부모 chapter-list 펼치기
      const chList = el.closest('.chapter-list');
      if (chList && !chList.classList.contains('open')) {
        chList.classList.add('open');
        const ti = chList.id.replace('chl-', '');
        const arrow = document.getElementById('arr-' + ti);
        if (arrow) arrow.classList.add('open');
      }
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      el.click();
      return;
    }
  }
}

async function createVolume() {
  if (!volumes.length) { alert('먼저 권을 구성하세요'); return; }
  const dest = document.getElementById('vol-dest').value.trim();
  if (!dest) { alert('저장 폴더를 선택하세요 (📂 버튼)'); return; }
  const makePdf     = document.getElementById('chk-pdf').checked;
  const makeFolder  = document.getElementById('chk-folder').checked;
  const makeCbz     = document.getElementById('chk-cbz').checked;
  const makeZip     = document.getElementById('chk-zip').checked;
  const compressJpg = document.getElementById('chk-compress').checked;
  const jpgQuality  = parseInt(document.getElementById('jpg-quality').value);
  if (!makePdf && !makeFolder && !makeCbz && !makeZip) { alert('폴더 / PDF / CBZ / ZIP 중 하나는 선택하세요'); return; }
  const toSave = volumes.filter(v => !v._saved);
  if (!toSave.length) { alert('저장할 권이 없습니다.\n(이미 저장된 권은 제외됩니다)'); return; }
  openPreviewModal(toSave, dest, makePdf, makeFolder, makeCbz, makeZip, compressJpg, jpgQuality);
}

let _saveParams = null;

function openPreviewModal(toSave, dest, makePdf, makeFolder, makeCbz, makeZip, compressJpg, jpgQuality) {
  _saveParams = {toSave, dest, makePdf, makeFolder, makeCbz, makeZip, compressJpg, jpgQuality};
  // 각 권의 화를 자연 정렬로 미리 정렬
  toSave.forEach(vol => {
    vol._sortedChapters = [...vol.chapters].sort((a, b) => {
      const an = a.replace(/\d+(\.\d+)?/g, n => n.padStart(10,'0'));
      const bn = b.replace(/\d+(\.\d+)?/g, n => n.padStart(10,'0'));
      return an.localeCompare(bn);
    });
  });
  renderPreviewModal();
  document.getElementById('preview-modal').classList.add('open');
}

function renderPreviewModal() {
  const list = document.getElementById('preview-vol-list');
  list.innerHTML = '';
  _saveParams.toSave.forEach((vol, vi) => {
    const title = document.createElement('div');
    title.className = 'preview-vol-title';
    title.textContent = '📦 ' + vol.name + ' — ' + vol._sortedChapters.length + '화';
    list.appendChild(title);
    vol._sortedChapters.forEach((ch, ci) => {
      const item = document.createElement('div');
      item.className = 'preview-ch-item';
      item.draggable = true;
      item.dataset.vi = vi; item.dataset.ci = ci;
      const name = ch.split(/[\\/]/).pop();
      item.innerHTML = '<span class="preview-ch-handle">⠿</span><span class="preview-ch-name">' + esc(name) + '</span>';
      item.addEventListener('dragstart', e => { e.dataTransfer.setData('text', vi+','+ci); item.style.opacity='0.4'; });
      item.addEventListener('dragend', () => { item.style.opacity='1'; list.querySelectorAll('.preview-ch-item').forEach(el=>el.classList.remove('drag-over')); });
      item.addEventListener('dragover', e => { e.preventDefault(); item.classList.add('drag-over'); });
      item.addEventListener('dragleave', () => item.classList.remove('drag-over'));
      item.addEventListener('drop', e => {
        e.preventDefault(); item.classList.remove('drag-over');
        const [fvi, fci] = e.dataTransfer.getData('text').split(',').map(Number);
        const tvi = parseInt(item.dataset.vi), tci = parseInt(item.dataset.ci);
        if (fvi !== tvi) return;
        const arr = _saveParams.toSave[fvi]._sortedChapters;
        const [moved] = arr.splice(fci, 1);
        arr.splice(tci, 0, moved);
        renderPreviewModal();
      });
      list.appendChild(item);
    });
  });
}

function closePreviewModal() {
  document.getElementById('preview-modal').classList.remove('open');
  _saveParams = null;
}

async function confirmSave() {
  document.getElementById('preview-modal').classList.remove('open');
  const {toSave, dest, makePdf, makeFolder, makeCbz, makeZip, compressJpg, jpgQuality} = _saveParams;
  _saveParams = null;
  // _sortedChapters를 chapters로 적용
  toSave.forEach(vol => { vol.chapters = vol._sortedChapters; });
  const res = await fetch('/api/create', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({volumes: toSave, dest, make_pdf: makePdf, make_folder: makeFolder,
                         make_cbz: makeCbz, make_zip: makeZip, compress_jpg: compressJpg, jpg_quality: jpgQuality})
  });
  const data = await res.json();
  if (data.error) { alert('오류: ' + data.error); return; }
  // 충돌 확인
  const conflicts = data.results.filter(r => r.conflict);
  if (conflicts.length) {
    const names = conflicts.map(r => r.name + ' (' + r.conflict + ')').join('\n');
    const ok = confirm('아래 폴더가 이미 존재합니다. 덮어쓸까요?\n\n' + names);
    if (!ok) return;
    // 덮어쓰기 재요청
    const overwriteVols = volumes.filter(v => conflicts.some(c => c.name === v.name));
    const res2 = await fetch('/api/create', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({volumes: overwriteVols, dest, make_pdf: makePdf, make_folder: makeFolder,
                           make_cbz: makeCbz, make_zip: makeZip, compress_jpg: compressJpg,
                           jpg_quality: jpgQuality, overwrite: true})
    });
    const data2 = await res2.json();
    if (data2.error) { alert('오류: ' + data2.error); return; }
    // 결과 합치기
    data.results = data.results.map(r => conflicts.some(c => c.name === r.name)
      ? (data2.results.find(r2 => r2.name === r.name) || r) : r);
  }
  const done = data.results.filter(r => !r.conflict || r.pages);
  if (done.length) {
    // 저장 완료된 권의 화들을 saved(파란색)로 변경
    toSave.forEach(vol => {
      if (done.find(r => r.name === vol.name)) {
        vol.chapters.forEach(path => {
          document.querySelectorAll('.chapter-item').forEach(el => {
            if (el.dataset.path === path) {
              el.classList.remove('in-vol');
              el.classList.add('saved');
            }
          });
        });
      }
    });
    alert('완료!\n' + done.map(r => {
      let s = r.name + ': ' + r.pages + '페이지';
      if (r.pdf)       s += ' · PDF ✅';
      if (r.cbz)       s += ' · CBZ ✅';
      if (r.zip)       s += ' · ZIP ✅';
      if (r.pdf_error) s += ' · PDF 오류: ' + r.pdf_error;
      if (r.cbz_error) s += ' · CBZ 오류: ' + r.cbz_error;
      if (r.zip_error) s += ' · ZIP 오류: ' + r.zip_error;
      return s;
    }).join('\n'));
  }
  volumes = []; renderVolumes();
}

function toggleCompress() {
  const on = document.getElementById('chk-compress').checked;
  document.getElementById('compress-opts').style.display = on ? 'block' : 'none';
}

async function openFolderPdf() {
  const folder = prompt('PDF로 변환할 폴더 경로 입력:\n(폴더 안 이미지 전부 합쳐서 PDF 1개 생성)');
  if (!folder) return;
  const dest = prompt('저장할 PDF 경로:', folder + '\\output.pdf');
  if (!dest) return;
  const res = await fetch('/api/folder2pdf', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({folder, dest})
  });
  const data = await res.json();
  if (data.error) { alert('오류: ' + data.error); return; }
  alert('✅ PDF 생성 완료!\n' + data.dest + '\n' + data.pages + '페이지');
}

/* ── 편집 기능 ── */
let editImgIndex = -1;

function openEdit(imgPath, imgName) {
  editImgPath = imgPath;
  editImgIndex = currentChapterImages.findIndex(i => i.path === imgPath);
  selRect = null;
  document.getElementById('sel-overlay').style.display = 'none';
  document.getElementById('edit-info').textContent = '이미지를 드래그해서 영역을 선택하세요';
  _loadEditImage(imgPath, imgName);
  document.getElementById('edit-modal').classList.add('open');
  setMode('crop');
}

function _loadEditImage(imgPath, imgName) {
  editImgPath = imgPath;
  document.getElementById('edit-filename').textContent = imgName;
  // 현재 인덱스 / 전체 표시
  const total = currentChapterImages.length;
  const idx = currentChapterImages.findIndex(i => i.path === imgPath);
  editImgIndex = idx;
  document.getElementById('edit-imgsize').textContent = '';
  const counter = document.getElementById('edit-counter');
  if (counter) counter.textContent = total > 0 ? (idx + 1) + ' / ' + total : '';
  const img = document.getElementById('edit-canvas');
  img.onload = () => { document.getElementById('edit-imgsize').textContent = img.naturalWidth + ' × ' + img.naturalHeight + 'px'; };
  img.src = '/api/img?path=' + encodeURIComponent(imgPath) + '&t=' + Date.now();
  selRect = null;
  document.getElementById('sel-overlay').style.display = 'none';
  // a/b 분리 파일이면 분리 복원 버튼 표시
  const srBtn = document.getElementById('btn-split-restore');
  const splitModeBtn = document.getElementById('btn-split-mode');
  if (srBtn) {
    const fname = imgPath.split('\\').pop().split('/').pop();
    const isSplit = /^(.+)[ab](\.[^.]+)$/.test(fname);
    srBtn.style.display = isSplit ? 'inline-flex' : 'none';
    if (splitModeBtn) splitModeBtn.style.display = isSplit ? 'none' : '';
  }
}

function editNavigate(delta) {
  if (!currentChapterImages.length) return;
  const newIdx = editImgIndex + delta;
  if (newIdx < 0 || newIdx >= currentChapterImages.length) return;
  const target = currentChapterImages[newIdx];
  _loadEditImage(target.path, target.name);
}

function closeEdit() {
  _splitMode = false;
  const btn = document.getElementById('btn-split-mode');
  if (btn) { btn.classList.remove('primary'); btn.textContent = '✂ 분리선'; }
  const execBtn = document.getElementById('btn-split-exec');
  if (execBtn) execBtn.style.display = 'none';
  const dirWrap = document.getElementById('split-dir-wrap');
  if (dirWrap) dirWrap.style.display = 'none';
  document.getElementById('split-vline').style.display = 'none';
  document.getElementById('edit-modal').classList.remove('open');
  // 편집된 이미지 썸네일만 새로고침 (전체 재로드 안 함)
  if (editImgPath) {
    document.querySelectorAll('.img-card').forEach(card => {
      if (card.dataset.path === editImgPath) {
        const thumb = card.querySelector('img');
        if (thumb) thumb.src = '/api/img?path=' + encodeURIComponent(editImgPath) + '&t=' + Date.now();
      }
    });
  }
}

function setMode(mode) {
  editMode = mode;
  document.getElementById('btn-crop').className = mode==='crop' ? 'primary' : '';
  document.getElementById('btn-mask-w').className = mode==='mask-white' ? 'primary' : '';
  document.getElementById('btn-mask-b').className = mode==='mask-black' ? 'primary' : '';
}

// 드래그 선택
const cw = document.getElementById('canvas-wrap');
const overlay = document.getElementById('sel-overlay');
let _splitMode = false;
let _splitX = 0.5; // 분할선 비율

function toggleSplitMode() {
  _splitMode = !_splitMode;
  const btn = document.getElementById('btn-split-mode');
  const execBtn = document.getElementById('btn-split-exec');
  const dirWrap = document.getElementById('split-dir-wrap');
  const vline = document.getElementById('split-vline');
  if (_splitMode) {
    btn.classList.add('primary');
    btn.textContent = '✂ 분리선 OFF';
    execBtn.style.display = 'inline-flex';
    dirWrap.style.display = 'flex';
    // 현재 이미지 위에 분할선 표시
    const img = document.getElementById('edit-canvas');
    const rect = img.getBoundingClientRect();
    const wrap = document.getElementById('canvas-wrap');
    const wrapRect = wrap.getBoundingClientRect();
    vline.style.display = 'block';
    vline.style.left = (_splitX * rect.width + rect.left - wrapRect.left) + 'px';
    vline.style.top = (rect.top - wrapRect.top) + 'px';
    vline.style.height = rect.height + 'px';
    document.getElementById('edit-info').textContent = '분할선을 드래그해서 위치 조정 후 [분리 실행] 클릭';
  } else {
    btn.classList.remove('primary');
    btn.textContent = '✂ 분리선';
    execBtn.style.display = 'none';
    dirWrap.style.display = 'none';
    vline.style.display = 'none';
    document.getElementById('edit-info').textContent = '이미지를 드래그해서 영역을 선택하세요';
  }
}

async function executeSplit() {
  if (!editImgPath) return;
  const dirRadio = document.querySelector('input[name="edit-split-dir"]:checked');
  const dir = dirRadio ? dirRadio.value : 'ltr';
  const splitPath = editImgPath;
  const res = await fetch('/api/split', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ paths: [splitPath], direction: dir, split_xs: {[splitPath]: _splitX}, default_x: _splitX })
  });
  const data = await res.json();
  if (data.error) { alert('오류: ' + data.error); return; }

  // edit 모달 닫기
  _splitMode = false;
  const btn = document.getElementById('btn-split-mode');
  if (btn) { btn.classList.remove('primary'); btn.textContent = '✂ 분리선'; }
  const execBtn = document.getElementById('btn-split-exec');
  if (execBtn) execBtn.style.display = 'none';
  const dirWrap = document.getElementById('split-dir-wrap');
  if (dirWrap) dirWrap.style.display = 'none';
  document.getElementById('split-vline').style.display = 'none';
  document.getElementById('edit-modal').classList.remove('open');

  // 캐시 무효화
  delete _thumbCache[splitPath];
  delete _chapterCache[currentChapterPath];

  // 썸네일 그리드: 원본 카드 → a/b 두 카드로 교체
  const resultPaths = data.results || [];
  const grid = document.getElementById('preview-grid');
  const oldCard = grid ? [...grid.querySelectorAll('.img-card')].find(c => c.dataset.path === splitPath) : null;
  if (oldCard && resultPaths.length === 2) {
    const newCards = resultPaths.map(p => {
      const fname = p.split('\\').pop().split('/').pop();
      const card = document.createElement('div');
      card.className = 'img-card';
      card.dataset.path = p;
      // 기존 카드와 동일한 방식: 빈 이미지로 시작 후 썸네일 로드
      card.innerHTML = `<img src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=" style="opacity:0"><div class="img-label">${esc(fname)}</div>`;
      card.addEventListener('dblclick', (e) => { e.preventDefault(); e.stopPropagation(); openEdit(p, fname); });
      card.addEventListener('click', (e) => {
        e.preventDefault();
        if (document.getElementById('edit-modal').classList.contains('open')) return;
        document.querySelectorAll('.img-card.focused').forEach(c => c.classList.remove('focused'));
        card.classList.add('focused');
      });
      return card;
    });
    oldCard.replaceWith(...newCards);

    // currentChapterImages 갱신
    const idx = currentChapterImages.findIndex(i => i.path === splitPath);
    if (idx !== -1) {
      const newEntries = resultPaths.map(p => ({ path: p, name: p.split('\\').pop().split('/').pop() }));
      currentChapterImages.splice(idx, 1, ...newEntries);
    }

    // 썸네일 배치 로드 — 화 전환 시에도 영향 없도록 캡처된 경로 사용
    const splitChapterPath = currentChapterPath;
    for (const p of resultPaths) {
      fetch('/api/thumbs?paths=' + encodeURIComponent(p))
        .then(r => r.json()).then(thumbs => {
          const url = thumbs[p];
          if (!url) return;
          _thumbCache[p] = url;
          if (currentChapterPath !== splitChapterPath) return;
          const cardEl = grid.querySelector('.img-card[data-path="' + p.replace(/\\/g,'\\\\').replace(/"/g,'\\"') + '"]');
          if (cardEl) {
            const imgEl = cardEl.querySelector('img');
            if (imgEl) { imgEl.src = url; imgEl.style.opacity = '1'; }
          }
        }).catch(() => {
          if (currentChapterPath !== splitChapterPath) return;
          const cardEl = grid.querySelector('.img-card[data-path="' + p.replace(/\\/g,'\\\\').replace(/"/g,'\\"') + '"]');
          if (cardEl) {
            const imgEl = cardEl.querySelector('img');
            if (imgEl) { imgEl.src = '/api/img?path=' + encodeURIComponent(p) + '&t=' + Date.now(); imgEl.style.opacity = '1'; }
          }
        });
    }
  } else {
    // fallback: 화 전체 재로드
    if (currentChapterPath) await previewChapter(currentChapterPath, '현재 화');
  }
}

let _splitDragging = false;

// 분할선 자체 드래그
document.getElementById('split-vline').addEventListener('mousedown', e => {
  if (!_splitMode) return;
  _splitDragging = true;
  e.stopPropagation();
  e.preventDefault();
});

cw.addEventListener('mousedown', e => {
  if (_splitMode) {
    if (_splitDragging) return; // 분할선 드래그 중이면 스킵
    // 이미지 클릭으로 분할선 위치 지정
    const img = document.getElementById('edit-canvas');
    const rect = img.getBoundingClientRect();
    const wrap = document.getElementById('canvas-wrap');
    const wrapRect = wrap.getBoundingClientRect();
    _splitX = Math.max(0.05, Math.min(0.95, (e.clientX - rect.left) / rect.width));
    const vline = document.getElementById('split-vline');
    vline.style.left = (_splitX * rect.width + rect.left - wrapRect.left) + 'px';
    document.getElementById('edit-info').textContent = '분할 위치: ' + Math.round(_splitX * 100) + '% | [분리 실행] 버튼을 눌러 분리';
    return;
  }
  const img = document.getElementById('edit-canvas');
  const rect = img.getBoundingClientRect();
  selStart = { x: e.clientX - rect.left, y: e.clientY - rect.top };
  isDragging = true;
  overlay.style.display = 'none';
});

cw.addEventListener('mousemove', e => {
  if (_splitDragging) {
    const img = document.getElementById('edit-canvas');
    const rect = img.getBoundingClientRect();
    const wrap = document.getElementById('canvas-wrap');
    const wrapRect = wrap.getBoundingClientRect();
    _splitX = Math.max(0.05, Math.min(0.95, (e.clientX - rect.left) / rect.width));
    const vline = document.getElementById('split-vline');
    vline.style.left = (_splitX * rect.width + rect.left - wrapRect.left) + 'px';
    document.getElementById('edit-info').textContent = '분할 위치: ' + Math.round(_splitX * 100) + '% | [분리 실행] 버튼을 눌러 분리';
    return;
  }
  if (!isDragging) return;
  const img = document.getElementById('edit-canvas');
  const rect = img.getBoundingClientRect();
  const cx = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
  const cy = Math.max(0, Math.min(e.clientY - rect.top, rect.height));
  const x = Math.min(selStart.x, cx), y = Math.min(selStart.y, cy);
  const w = Math.abs(cx - selStart.x), h = Math.abs(cy - selStart.y);
  overlay.style.cssText = `display:block;left:${x+img.offsetLeft}px;top:${y+img.offsetTop}px;width:${w}px;height:${h}px`;
  selRect = { x: x/rect.width, y: y/rect.height, w: w/rect.width, h: h/rect.height };
  document.getElementById('edit-info').textContent = `선택 영역: ${Math.round(x)}px, ${Math.round(y)}px — ${Math.round(w)}×${Math.round(h)}`;
});

cw.addEventListener('mouseup', () => { isDragging = false; });
document.addEventListener('mouseup', () => { _splitDragging = false; });

async function applyEdit(applyAll) {
  if (!selRect || selRect.w < 0.01 || selRect.h < 0.01) { alert('먼저 영역을 드래그해서 선택하세요'); return; }
  const color = editMode === 'mask-black' ? 'black' : 'white';
  const type = editMode === 'crop' ? 'crop' : 'mask';
  const targets = applyAll ? currentChapterImages.map(i => i.path) : [editImgPath];
  if (!confirm((applyAll ? '현재 화 전체 ' + targets.length + '장에' : '이 이미지에') + ' 적용할까요?\n⚠ 원본이 수정됩니다.')) return;
  document.getElementById('edit-info').textContent = '처리 중...';
  const res = await fetch('/api/edit', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ paths: targets, type, color, x: selRect.x, y: selRect.y, w: selRect.w, h: selRect.h })
  });
  const data = await res.json();
  if (data.error) { alert('오류: ' + data.error); return; }
  document.getElementById('edit-info').textContent = '✅ 완료! (' + data.done + '장 처리됨)';
  const ts = Date.now();
  // 편집 모달 이미지 새로고침
  const img = document.getElementById('edit-canvas');
  img.src = '/api/img?path=' + encodeURIComponent(editImgPath) + '&t=' + ts;
  // 영향받은 썸네일만 새로고침 (전체 재로드 안 함)
  targets.forEach(p => {
    document.querySelectorAll('.img-card').forEach(card => {
      if (card.dataset.path === p) {
        const thumb = card.querySelector('img');
        if (thumb) thumb.src = '/api/img?path=' + encodeURIComponent(p) + '&t=' + ts;
      }
    });
  });
  selRect = null; overlay.style.display = 'none';
}

/* ── 파일명 변경 ── */
let renameImages = [];

function openRename() {
  if (!currentChapterImages.length) { alert('먼저 화를 클릭해서 이미지를 로드하세요'); return; }
  renameImages = currentChapterImages;
  document.getElementById('rename-chapter-name').textContent = currentChapterPath.split('\\').pop() || currentChapterPath.split('/').pop();
  document.getElementById('rn-prefix').value = '';
  document.getElementById('rn-start').value = '1';
  updatePreview();
  document.getElementById('rename-modal').classList.add('open');
}

function closeRename() {
  document.getElementById('rename-modal').classList.remove('open');
}

function buildNewName(prefix, idx, digits) {
  const num = String(idx).padStart(parseInt(digits), '0');
  return prefix + num;
}

function updatePreview() {
  const prefix = document.getElementById('rn-prefix').value;
  const digits = document.getElementById('rn-digits').value;
  const start = parseInt(document.getElementById('rn-start').value) || 1;
  // 샘플
  const sample1 = buildNewName(prefix, start, digits);
  const sample2 = buildNewName(prefix, start + 1, digits);
  document.getElementById('rn-sample').textContent = sample1 + '.jpg,  ' + sample2 + '.jpg  ...';
  // 미리보기 목록
  const preview = document.getElementById('rename-preview');
  preview.innerHTML = '';
  renameImages.forEach((img, i) => {
    const ext = img.name.lastIndexOf('.') >= 0 ? img.name.slice(img.name.lastIndexOf('.')) : '';
    const newName = buildNewName(prefix, start + i, digits) + ext;
    const row = document.createElement('div');
    row.className = 'rename-item';
    const changed = newName !== img.name;
    row.innerHTML = `<span class="rename-old">${esc(img.name)}</span><span class="rename-arrow">→</span><span class="rename-new" style="color:${changed?'#c5bff5':'#555'}">${esc(newName)}</span>`;
    preview.appendChild(row);
  });
}

async function applyRename() {
  const prefix = document.getElementById('rn-prefix').value;
  const digits = document.getElementById('rn-digits').value;
  const start = parseInt(document.getElementById('rn-start').value) || 1;
  const renames = renameImages.map((img, i) => {
    const ext = img.name.lastIndexOf('.') >= 0 ? img.name.slice(img.name.lastIndexOf('.')) : '';
    return { old_path: img.path, new_name: buildNewName(prefix, start + i, digits) + ext };
  });
  if (!confirm(renames.length + '개 파일명을 변경할까요?')) return;
  const res = await fetch('/api/rename', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ renames })
  });
  const data = await res.json();
  if (data.error) { alert('오류: ' + data.error); return; }
  alert('✅ ' + data.done + '개 파일명 변경 완료');
  closeRename();
  if (currentChapterPath) previewChapter(currentChapterPath, '현재 화');
}


async function doRestore(all) {
  const targets = all ? currentChapterImages.map(i => i.path) : [editImgPath];
  const label = all ? '현재 화 전체 ' + targets.length + '장을' : '이 이미지를';
  if (!confirm(label + ' 원본으로 복원할까요?\\n_원본백업 폴더의 파일로 덮어씁니다.')) return;
  setBackupStatus('복원 중...');
  const res = await fetch('/api/restore', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({paths: targets}) });
  const data = await res.json();
  if (data.error) { setBackupStatus('❌ ' + data.error); return; }
  setBackupStatus('✅ ' + data.done + '장 복원 완료' + (data.missing > 0 ? ' (' + data.missing + '장 백업 없음)' : ''));
  const ts = Date.now();
  // 편집 모달 이미지 새로고침
  const img = document.getElementById('edit-canvas');
  img.src = '/api/img?path=' + encodeURIComponent(editImgPath) + '&t=' + ts;
  // 영향받은 썸네일만 새로고침
  targets.forEach(p => {
    document.querySelectorAll('.img-card').forEach(card => {
      if (card.dataset.path === p) {
        const thumb = card.querySelector('img');
        if (thumb) thumb.src = '/api/img?path=' + encodeURIComponent(p) + '&t=' + ts;
      }
    });
  });
}

function setBackupStatus(msg) {
  document.getElementById('backup-status').textContent = msg;
}
async function doSplitRestore() {
  if (!editImgPath) return;
  // 파일명에서 원본 경로 추론: 005a.jpg → 005.jpg
  const sep = editImgPath.includes('\\') ? '\\' : '/';
  const dir = editImgPath.split(sep).slice(0, -1).join(sep);
  const fname = editImgPath.split(sep).pop();
  const m = fname.match(/^(.+)([ab])(\.[^.]+)$/);
  if (!m) { alert('분리된 파일이 아닙니다 (파일명이 a/b로 끝나야 함)'); return; }
  const origName = m[1] + m[3];
  const origPath = dir + sep + origName;
  const pathA = dir + sep + m[1] + 'a' + m[3];
  const pathB = dir + sep + m[1] + 'b' + m[3];
  if (!confirm('원본 ' + origName + ' 으로 복원하고\n' + pathA.split(sep).pop() + ', ' + pathB.split(sep).pop() + ' 을 삭제할까요?')) return;
  setBackupStatus('복원 중...');
  const res = await fetch('/api/split_restore', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ orig_path: origPath, split_paths: [pathA, pathB] })
  });
  const data = await res.json();
  if (data.error) { setBackupStatus('❌ ' + data.error); return; }
  setBackupStatus('✅ 복원 완료');

  // currentChapterImages 갱신: a/b 제거 후 원본 삽입
  const idxA = currentChapterImages.findIndex(i => i.path === pathA);
  const idxB = currentChapterImages.findIndex(i => i.path === pathB);
  const insertIdx = idxA !== -1 ? idxA : (idxB !== -1 ? idxB : -1);
  if (insertIdx !== -1) {
    currentChapterImages = currentChapterImages.filter(i => i.path !== pathA && i.path !== pathB);
    currentChapterImages.splice(insertIdx, 0, { path: origPath, name: origName });
  }

  // 썸네일 그리드: a/b 카드 제거 후 원본 카드 삽입
  delete _thumbCache[pathA]; delete _thumbCache[pathB];
  const grid = document.getElementById('preview-grid');
  const cardA = grid ? grid.querySelector('.img-card[data-path="' + pathA.replace(/\\/g,'\\\\') + '"]') : null;
  const cardB = grid ? grid.querySelector('.img-card[data-path="' + pathB.replace(/\\/g,'\\\\') + '"]') : null;
  const anchor = cardA || cardB;
  if (anchor) {
    const newCard = document.createElement('div');
    newCard.className = 'img-card';
    newCard.dataset.path = origPath;
    newCard.innerHTML = '<img src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=" style="opacity:0"><div class="img-label">' + esc(origName) + '</div>';
    newCard.addEventListener('dblclick', (e) => { e.preventDefault(); e.stopPropagation(); openEdit(origPath, origName); });
    newCard.addEventListener('click', (e) => {
      e.preventDefault();
      if (document.getElementById('edit-modal').classList.contains('open')) return;
      document.querySelectorAll('.img-card.focused').forEach(c => c.classList.remove('focused'));
      newCard.classList.add('focused');
    });
    anchor.replaceWith(newCard);
    if (cardB && cardB.parentNode) cardB.remove();
    if (cardA && cardA !== anchor && cardA.parentNode) cardA.remove();
    // 썸네일 로드
    fetch('/api/thumbs?paths=' + encodeURIComponent(origPath))
      .then(r => r.json()).then(thumbs => {
        const url = thumbs[origPath];
        if (!url) return;
        _thumbCache[origPath] = url;
        const imgEl = newCard.querySelector('img');
        if (imgEl) { imgEl.src = url; imgEl.style.opacity = '1'; }
      });
  }
  delete _chapterCache[currentChapterPath];
  closeEdit();
}

async function loadExistingVolumes(destPath) {
  const res = await fetch('/api/load_volumes?path=' + encodeURIComponent(destPath));
  const data = await res.json();
  if (data.error || !data.volumes.length) return;
  // 기존 권 목록을 패널에 표시 (chapters는 빈 배열 — 저장 완료된 권이라 재저장 불필요)
  const existing = data.volumes.map(v => ({ name: v.name, path: v.path, count: v.count, saved: true }));
  // 현재 volumes에 없는 것만 추가
  existing.forEach(v => {
    if (!volumes.find(vol => vol.name === v.name)) {
      volumes.push({ name: v.name, chapters: [], _saved: true, _count: v.count, path: v.path });
    }
  });
  if (existing.length) {
    renderVolumes();
    autoSetNextVolName();
  }
}

function autoSetNextVolName() {
  if (!volumes.length) return;
  const input = document.getElementById('vol-name');
  const existingNames = new Set(volumes.map(v => v.name));

  // 현재 입력값이 이미 존재하는 이름이면 다음 번호로 갱신, 비어있어도 자동 설정
  const curVal = input.value.trim();
  if (curVal && !existingNames.has(curVal)) return; // 입력값이 있고 중복 아니면 그대로

  // 모든 권 이름에서 가장 큰 번호 + prefix 추출
  let maxNum = 0;
  let prefix = '';
  volumes.forEach(v => {
    const m = v.name.match(/^(.*?)\s*(\d+)권?\s*$/);
    if (m) {
      const n = parseInt(m[2]);
      if (n > maxNum) { maxNum = n; prefix = m[1].trim(); }
    }
  });
  if (maxNum === 0) return;

  // 중복되지 않는 다음 번호 찾기
  let next = maxNum + 1;
  while (existingNames.has((prefix ? prefix + ' ' : '') + next + '권')) next++;
  input.value = (prefix ? prefix + ' ' : '') + next + '권';
}

function setDestPath(p) {
  document.getElementById('vol-dest').value = p;
  const label = document.getElementById('vol-dest-label');
  if (label) { label.textContent = p || '저장 경로 미설정'; label.title = p; label.style.color = p ? '#c5bff5' : '#888'; }
  const saveBtn = document.getElementById('btn-save-vol');
  if (saveBtn) saveBtn.title = p ? '저장 경로: ' + p : '';
}

async function browseDestFolder() {
  const current = document.getElementById('vol-dest').value;
  const res = await fetch('/api/browse?initial=' + encodeURIComponent(current));
  const data = await res.json();
  if (data.error) { alert('폴더 선택 오류: ' + data.error); return; }
  if (data.path) setDestPath(data.path);
}

// ── 양면 분리 ──
let _splitSingleMode = false;
let _splitLines = {};
let _splitTargets = [];

function openSplitModal(singleMode) {
  _splitSingleMode = singleMode;
  _splitLines = {};
  const allTargets = singleMode
    ? [{path: editImgPath, name: editImgPath.split('\\').pop()||editImgPath.split('/').pop()}]
    : currentChapterImages;
  _splitTargets = allTargets;
  const splitCount = allTargets.filter(i => !isAlreadySplit(i.path)).length;
  const skipCount  = allTargets.length - splitCount;
  document.getElementById('split-target-info').textContent =
    singleMode ? '현재 이미지 1장' :
    '전체 ' + allTargets.length + '장' + (skipCount ? ' (이미 분리된 ' + skipCount + '장 제외 → ' + splitCount + '장 처리)' : '');
  document.getElementById('split-global-x').value = 50;
  document.getElementById('split-global-val').textContent = '50%';
  renderSplitGrid();
  document.getElementById('split-modal').classList.add('open');
}

function closeSplitModal() {
  document.getElementById('split-modal').classList.remove('open');
}

function renderSplitGrid() {
  const grid = document.getElementById('split-grid');
  grid.innerHTML = '';
  _splitTargets.forEach(img => {
    const sx = _splitLines[img.path] !== undefined ? _splitLines[img.path] : 0.5;
    const card = document.createElement('div');
    card.className = 'split-card' + (_splitLines[img.path] !== undefined ? ' adjusted' : '');

    const wrap = document.createElement('div');
    wrap.className = 'split-img-wrap';

    const imgEl = document.createElement('img');
    imgEl.src = _thumbCache[img.path] || ('/api/thumb?path=' + encodeURIComponent(img.path));
    imgEl.draggable = false;

    const line = document.createElement('div');
    line.className = 'split-line';
    line.style.left = (sx * 100) + '%';

    wrap.appendChild(imgEl);
    wrap.appendChild(line);

    let dragging = false;
    wrap.addEventListener('mousedown', e => { dragging = true; moveLine(e); e.preventDefault(); });
    wrap.addEventListener('mousemove', e => { if (dragging) moveLine(e); });
    document.addEventListener('mouseup', () => { dragging = false; });

    function moveLine(e) {
      const rect = wrap.getBoundingClientRect();
      const x = Math.max(0.1, Math.min(0.9, (e.clientX - rect.left) / rect.width));
      line.style.left = (x * 100) + '%';
      _splitLines[img.path] = x;
      card.classList.add('adjusted');
      updateSplitFooterInfo();
    }

    const name = document.createElement('div');
    name.className = 'split-card-name';
    name.textContent = img.name || img.path.split('\\').pop();

    card.appendChild(wrap);
    card.appendChild(name);
    grid.appendChild(card);
  });
  updateSplitFooterInfo();
}

function applyGlobalSplitX(val) {
  document.getElementById('split-global-val').textContent = val + '%';
  document.querySelectorAll('.split-card:not(.adjusted) .split-line').forEach(line => {
    line.style.left = val + '%';
  });
  updateSplitFooterInfo();
}

function resetSplitLines() {
  _splitLines = {};
  const val = document.getElementById('split-global-x').value;
  document.querySelectorAll('.split-card').forEach(card => {
    card.classList.remove('adjusted');
    const line = card.querySelector('.split-line');
    if (line) line.style.left = val + '%';
  });
  updateSplitFooterInfo();
}

function updateSplitPreview() {}

function updateSplitFooterInfo() {
  const adjusted = Object.keys(_splitLines).length;
  document.getElementById('split-footer-info').textContent =
    _splitTargets.length + '장 중 ' + adjusted + '장 개별 조정됨';
}

function isAlreadySplit(path) {
  // 파일명이 a나 b로 끝나면 이미 분리된 것으로 판단 (예: 009a.jpg, 009b.jpg)
  const name = path.split('\\').pop().split('/').pop();
  const stem = name.replace(/\.[^.]+$/, '');
  return /[ab]$/.test(stem);
}

async function applySplit() {
  const dir = document.querySelector('input[name="split-dir"]:checked').value;
  const globalX = parseInt(document.getElementById('split-global-x').value) / 100;
  // 이미 분리된 파일 제외
  const targets = _splitTargets.map(i => i.path).filter(p => !isAlreadySplit(p));
  const skipped = _splitTargets.length - targets.length;
  if (!targets.length) { alert('분리할 이미지가 없습니다.\n(이미 분리된 파일만 있습니다)'); return; }
  const msg = targets.length + '장을 양면 분리할까요?' + (skipped ? '\n(' + skipped + '장은 이미 분리된 파일로 제외됩니다)' : '') + '\n원본은 자동으로 백업됩니다.';
  if (!confirm(msg)) return;
  closeSplitModal();
  document.getElementById('status').textContent = '분리 중...';
  const res = await fetch('/api/split', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ paths: targets, direction: dir, split_xs: _splitLines, default_x: globalX })
  });
  const data = await res.json();
  if (data.error) { alert('오류: ' + data.error); return; }
  targets.forEach(p => { delete _thumbCache[p]; });
  delete _chapterCache[currentChapterPath];
  const chPath2 = currentChapterPath;
  if (_splitSingleMode) closeEdit();
  if (chPath2) await previewChapter(chPath2, '현재 화');
}

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function shutdownApp() {
  if (!confirm('서버를 종료할까요?\n브라우저 탭도 함께 닫힙니다.')) return;
  try { await fetch('/api/shutdown'); } catch {}
  window.close();
}

// ── 편집 모달 단축키 ──

// 새 키보드 단쳐키 (그리드 + 편집 모달)
document.addEventListener('keydown', async (e) => {
  const modalOpen = document.getElementById('edit-modal').classList.contains('open');

  // 그리드 모드
  if (!modalOpen) {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      if (!currentChapterImages.length) return;
      e.preventDefault();
      const cards = [...document.querySelectorAll('.img-card')];
      const focused = document.querySelector('.img-card.focused');
      const idx = focused ? cards.indexOf(focused) : -1;
      const newIdx = Math.max(0, Math.min(cards.length - 1, idx + (e.key === 'ArrowLeft' ? -1 : 1)));
      cards.forEach(c => c.classList.remove('focused'));
      cards[newIdx].classList.add('focused');
      cards[newIdx].scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      return;
    }
    if (e.key === 'Enter') {
      const focused = document.querySelector('.img-card.focused');
      if (focused) {
        const img = currentChapterImages.find(i => i.path === focused.dataset.path);
        if (img) { openEdit(img.path, img.name); return; }
      }
    }
    if (e.key === 'Delete') {
      const focused = document.querySelector('.img-card.focused');
      if (!focused) return;
      const path = focused.dataset.path;
      // currentChapterImages에서 제거
      currentChapterImages = currentChapterImages.filter(i => i.path !== path);
      // 다음 포커스 대상 미리 찾기
      const allCards = [...document.querySelectorAll('.img-card')];
      const fi = allCards.indexOf(focused);
      // 즉시 제거
      focused.remove();
      // 캐시 업데이트
      if (_chapterCache[currentChapterPath]) {
        _chapterCache[currentChapterPath].images = currentChapterImages;
        _chapterCache[currentChapterPath].html = document.getElementById('preview-grid').innerHTML;
      }
      // 다음 카드 포커스
      const rem = [...document.querySelectorAll('.img-card')];
      const nxt = rem[Math.min(fi, rem.length - 1)];
      if (nxt) nxt.classList.add('focused');
      document.getElementById('status').textContent =
        '현재 화 — ' + currentChapterImages.length + '페이지 (더블클릭:편집 | ←→:이동 | Del:제외)';
      return;
    }
    // 사이드바 위아래 방향키: 화 이동
    if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
      const items = [...document.querySelectorAll('.chapter-item')];
      if (!items.length) return;
      e.preventDefault();
      const sel = document.querySelector('.chapter-item.selected');
      let idx = sel ? items.indexOf(sel) : -1;
      const newIdx = Math.max(0, Math.min(items.length - 1, idx + (e.key === 'ArrowUp' ? -1 : 1)));
      const target = items[newIdx];
      target.click();
      target.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      return;
    }
    return;
  }

  // 편집 모달 모드
  if (e.key === 'ArrowLeft')  { e.preventDefault(); editNavigate(-1); return; }
  if (e.key === 'ArrowRight') { e.preventDefault(); editNavigate(1);  return; }
  if (e.key === 'Escape') { e.preventDefault(); closeEdit(); return; }
  if ((e.key === 'c' || e.key === 'C') && !e.ctrlKey) { e.preventDefault(); setMode('crop'); return; }
  if ((e.key === 'w' || e.key === 'W') && !e.ctrlKey) { e.preventDefault(); setMode('mask-white'); return; }
  if ((e.key === 'b' || e.key === 'B') && !e.ctrlKey) { e.preventDefault(); setMode('mask-black'); return; }
  if (e.ctrlKey && e.shiftKey && e.key === 'S') { e.preventDefault(); applyEdit(true); return; }
  if (e.ctrlKey && !e.shiftKey && e.key === 's') { e.preventDefault(); applyEdit(false); return; }
  if (e.ctrlKey && !e.shiftKey && e.key === 'z') {
    e.preventDefault();
    if (!editImgPath) return;
    setBackupStatus('복원 중...');
    const res = await fetch('/api/restore', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({paths:[editImgPath]}) });
    const data = await res.json();
    if (data.error || data.missing > 0) { setBackupStatus('❌ 백업 파일이 없습니다'); return; }
    const ts = Date.now();
    document.getElementById('edit-canvas').src = '/api/img?path=' + encodeURIComponent(editImgPath) + '&t=' + ts;
    document.querySelectorAll('.img-card').forEach(card => {
      if (card.dataset.path === editImgPath) { const t = card.querySelector('img'); if (t) t.src = '/api/img?path=' + encodeURIComponent(editImgPath) + '&t=' + ts; }
    });
    setBackupStatus('✅ 복원 완료');
  }
});

// heartbeat
function startHeartbeat() {
  setInterval(async () => {
    try { await fetch('/api/heartbeat'); } catch {}
  }, 2000);
}
startHeartbeat();
</script>
</body>
</html>
"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, f, *a): pass

    def handle_error(self, request, client_address):
        pass  # 연결 끊김 오류 무시

    def handle(self):
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # 브라우저가 먼저 연결을 끊었을 때 무시

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)

        if path == '/':
            body = HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)

        elif path == '/api/shutdown':
            self.send_json({'ok': True})
            _shutdown()

        elif path == '/api/heartbeat':
            _reset_heartbeat()
            self.send_json({'ok': True})

        elif path == '/api/load_volumes':
            folder = unquote(qs.get('path',[''])[0])
            try:
                p = Path(folder)
                if not p.exists():
                    self.send_json({'volumes': []})
                    return
                vols = []
                for d in sorted(p.iterdir(), key=natural_key):
                    if d.is_dir():
                        imgs = sorted([f for f in d.iterdir() if f.is_file() and is_image(f)], key=natural_key)
                        vols.append({'name': d.name, 'path': str(d), 'count': len(imgs)})
                self.send_json({'volumes': vols})
            except Exception as e:
                self.send_json({'error': str(e)}, 500)

        elif path == '/api/lastpath':
            self.send_json({'path': load_last_path()})

        elif path == '/api/browse':
            if not HAS_TK:
                self.send_json({'error': 'tkinter 미설치'}, 500)
                return
            try:
                root_win = tk.Tk()
                root_win.withdraw()
                root_win.wm_attributes('-topmost', True)
                initial = unquote(qs.get('initial', [''])[0]) or load_last_path() or '/'
                selected = filedialog.askdirectory(parent=root_win, initialdir=initial, title='만화 폴더 선택')
                root_win.destroy()
                if selected:
                    p = selected.replace('/', '\\')
                    save_last_path(p)
                    self.send_json({'path': p})
                else:
                    self.send_json({'path': ''})
            except Exception as e:
                self.send_json({'error': str(e)}, 500)

        elif path == '/api/scan':
            folder = unquote(qs.get('path',[''])[0])
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('X-Accel-Buffering', 'no')
                self.end_headers()

                def emit(data):
                    msg = 'data: ' + json.dumps(data, ensure_ascii=False) + '\n\n'
                    self.wfile.write(msg.encode('utf-8'))
                    self.wfile.flush()

                root = Path(folder)
                if not root.exists():
                    emit({'error': f'경로를 찾을 수 없습니다: {folder}'}); return
                dirs = sorted([e for e in root.iterdir() if e.is_dir()], key=natural_key)
                if not dirs:
                    emit({'error': f'하위 폴더가 없습니다.\n현재 경로: {folder}'}); return

                def get_images(d):
                    return sorted([f for f in d.iterdir()
                                   if f.is_file() and is_image(f) and BACKUP_DIR_NAME not in f.parts])

                two_level = any(get_images(d) for d in dirs)
                result = []

                if two_level:
                    emit({'status': 'start', 'title': root.name})
                    chapters = []
                    for ch_dir in dirs:
                        images = get_images(ch_dir)
                        if images:
                            ch = {'name': ch_dir.name, 'path': str(ch_dir),
                                  'images': [str(f) for f in images], 'count': len(images)}
                            chapters.append(ch)
                            emit({'status': 'chapter', 'title': root.name, 'chapter': ch_dir.name, 'count': len(images)})
                    if chapters:
                        result.append({'title': root.name, 'path': str(root), 'chapters': chapters})
                else:
                    for title_dir in dirs:
                        emit({'status': 'start', 'title': title_dir.name})
                        chapters = []
                        ch_dirs = sorted([e for e in title_dir.iterdir() if e.is_dir()], key=natural_key)
                        for ch_dir in ch_dirs:
                            images = get_images(ch_dir)
                            if images:
                                ch = {'name': ch_dir.name, 'path': str(ch_dir),
                                      'images': [str(f) for f in images], 'count': len(images)}
                                chapters.append(ch)
                                emit({'status': 'chapter', 'title': title_dir.name, 'chapter': ch_dir.name, 'count': len(images)})
                        if chapters:
                            result.append({'title': title_dir.name, 'path': str(title_dir), 'chapters': chapters})

                if not result:
                    emit({'error': '인식된 만화가 없습니다.\n화 폴더 안에 이미지 파일이 있는지 확인하세요.'})
                else:
                    save_last_path(folder)
                    emit({'status': 'done', 'result': result})
                    # 백그라운드에서 썸네일 미리 생성
                    def _precache():
                        for title in result:
                            for ch in title['chapters']:
                                for img_path in ch.get('images', []):
                                    try:
                                        if HAS_PIL:
                                            create_thumbnail(img_path)
                                    except Exception:
                                        pass
                    threading.Thread(target=_precache, daemon=True).start()
            except Exception as e:
                try: emit({'error': str(e)})
                except Exception: pass

        elif path == '/api/images':
            folder = unquote(qs.get('path',[''])[0])
            try:
                p = Path(folder)
                images = sorted([{'name': f.name, 'path': str(f)} for f in p.iterdir() if f.is_file() and is_image(f)], key=lambda x: natural_key(Path(x['name'])))
                self.send_json({'images': images})
            except Exception as e: self.send_json({'error': str(e)}, 400)

        elif path == '/api/thumbs':
            # 여러 이미지 썸네일을 base64로 한번에 반환 (병렬 처리)
            import base64
            from concurrent.futures import ThreadPoolExecutor
            paths = unquote(qs.get('paths',[''])[0]).split('|')
            result_map = {}

            def make_thumb(img_path):
                try:
                    p = Path(img_path).resolve()
                    if not is_image(p) or not p.exists(): return None
                    if HAS_PIL:
                        data = create_thumbnail(str(p))
                    else:
                        data = p.read_bytes()
                    ext = p.suffix.lower()
                    mime = 'image/jpeg' if ext in {'.jpg','.jpeg','.jped'} else 'image/png'
                    return img_path, 'data:' + mime + ';base64,' + base64.b64encode(data).decode()
                except Exception:
                    return None

            for result in _thumb_executor.map(make_thumb, paths):
                if result: result_map[result[0]] = result[1]
            self.send_json(result_map)

        elif path == '/api/thumb':
            img_path = unquote(qs.get('path',[''])[0])
            try:
                p = Path(img_path).resolve()
                if not is_image(p) or not p.exists() or not p.is_file():
                    raise ValueError("not image")
                if HAS_PIL:
                    data = create_thumbnail(str(p))
                    mime = 'image/jpeg' if p.suffix.lower() in {'.jpg','.jpeg','.jped'} else 'image/png'
                else:
                    data = p.read_bytes()
                    mime = mimetypes.guess_type(str(p))[0] or 'image/jpeg'
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', len(data))
                self.send_header('Cache-Control', 'max-age=60')
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_response(404); self.end_headers()

        elif path == '/api/img':
            img_path = unquote(qs.get('path',[''])[0])
            try:
                p = Path(img_path).resolve()
                if not is_image(p):
                    raise ValueError("이미지 파일이 아닙니다")
                # 경로 순회 공격 방지: 절대 경로만 허용하고 로컬 파일 시스템 내에 있어야 함
                if not p.is_absolute() or not p.exists() or not p.is_file():
                    raise ValueError("파일을 찾을 수 없습니다")
                mime = mimetypes.guess_type(str(p))[0] or 'image/jpeg'
                data = p.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', len(data))
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_response(404); self.end_headers()

        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length))
        parsed = urlparse(self.path)

        if parsed.path == '/api/create':
            try:
                results = []
                make_folder = body.get('make_folder', True)
                overwrite   = body.get('overwrite', False)
                make_pdf    = body.get('make_pdf', False)
                make_cbz    = body.get('make_cbz', False)
                make_zip    = body.get('make_zip', False)
                compress    = body.get('compress_jpg', False)   # PNG→JPG 변환
                jpg_quality = int(body.get('jpg_quality', 85))
                dest_root   = body.get('dest', '')

                for vol in body.get('volumes', []):
                    vol_name    = vol['name']
                    folder_dest = str(Path(dest_root) / vol_name)
                    result      = {'name': vol_name, 'pages': 0}

                    # ── 원본 이미지 수집 ──
                    raw_imgs = collect_chapter_images(vol['chapters'])

                    # ── 폴더 저장 ──
                    if make_folder:
                        dest_p = Path(folder_dest)
                        if dest_p.exists() and any(dest_p.iterdir()) and not overwrite:
                            result['conflict'] = str(dest_p)
                            results.append(result)
                            continue
                        elif dest_p.exists() and overwrite:
                            shutil.rmtree(dest_p)
                        dest_p.mkdir(parents=True, exist_ok=True)
                        page_num = 1
                        for src in raw_imgs:
                            if compress and src.suffix.lower() not in {'.jpg', '.jpeg'}:
                                out = dest_p / f"{page_num:04d}.jpg"
                                compress_image(src, out, jpg_quality)
                            else:
                                out = dest_p / f"{page_num:04d}{src.suffix.lower()}"
                                shutil.copy2(src, out)
                            page_num += 1
                        result['pages'] = page_num - 1

                    # ── PDF 저장 ──
                    if make_pdf:
                        pdf_dest = str(Path(dest_root) / (vol_name + '.pdf'))
                        try:
                            if make_folder:
                                imgs = sorted([str(f) for f in Path(folder_dest).iterdir() if f.suffix.lower() in IMG_EXTS])
                                create_pdf(imgs, pdf_dest, jpg_quality)
                                if not result['pages']:
                                    result['pages'] = len(imgs)
                            else:
                                all_imgs = [str(f) for f in raw_imgs]
                                if compress:
                                    # 임시 버퍼로 변환 후 PDF 생성
                                    tmp_imgs = []
                                    tmp_dir = Path(dest_root) / ('__tmp_' + vol_name)
                                    tmp_dir.mkdir(parents=True, exist_ok=True)
                                    for i, src in enumerate(raw_imgs):
                                        if src.suffix.lower() not in {'.jpg', '.jpeg'}:
                                            tmp = tmp_dir / f"{i+1:04d}.jpg"
                                            compress_image(src, tmp, jpg_quality)
                                            tmp_imgs.append(str(tmp))
                                        else:
                                            tmp_imgs.append(str(src))
                                    create_pdf(tmp_imgs, pdf_dest, jpg_quality)
                                    shutil.rmtree(tmp_dir, ignore_errors=True)
                                else:
                                    create_pdf(all_imgs, pdf_dest, jpg_quality)
                                if not result['pages']:
                                    result['pages'] = len(raw_imgs)
                            result['pdf'] = pdf_dest
                        except Exception as pe:
                            result['pdf_error'] = str(pe)

                    # ── CBZ / ZIP 저장 (공통 헬퍼) ──
                    def _save_archive(ext, key):
                        arc_dest = str(Path(dest_root) / (vol_name + ext))
                        try:
                            if make_folder:
                                imgs = sorted([str(f) for f in Path(folder_dest).iterdir() if f.suffix.lower() in IMG_EXTS])
                                create_archive(imgs, arc_dest, compress_to_jpg=False)
                            else:
                                create_archive([str(f) for f in raw_imgs], arc_dest,
                                               compress_to_jpg=compress, jpg_quality=jpg_quality)
                            result[key] = arc_dest
                            if not result['pages']:
                                result['pages'] = len(raw_imgs)
                        except Exception as ae:
                            result[key + '_error'] = str(ae)

                    if make_cbz:
                        _save_archive('.cbz', 'cbz')
                    if make_zip:
                        _save_archive('.zip', 'zip')

                    results.append(result)
                self.send_json({'results': results})
            except Exception as e: self.send_json({'error': str(e)}, 500)

        elif parsed.path == '/api/edit':
            if not HAS_PIL:
                self.send_json({'error': 'Pillow 미설치. pip install Pillow 후 재시작하세요.'}, 500)
                return
            try:
                paths = body['paths']
                done = 0
                for p in paths:
                    apply_edit(p, body['type'], body['x'], body['y'], body['w'], body['h'], body.get('color','white'))
                    done += 1
                self.send_json({'done': done})
            except Exception as e: self.send_json({'error': str(e)}, 500)

        elif parsed.path == '/api/backup':
            try:
                paths = body.get('paths', [])
                done = 0
                for p in paths:
                    backup_image(p)
                    done += 1
                self.send_json({'done': done})
            except Exception as e: self.send_json({'error': str(e)}, 500)

        elif parsed.path == '/api/folder2pdf':
            try:
                folder = body.get('folder','')
                dest = body.get('dest','')
                pdf_path = folder_to_pdf(folder, dest)
                imgs = [f for f in Path(folder).rglob('*') if f.suffix.lower() in IMG_EXTS and '_원본백업' not in str(f)]
                self.send_json({'dest': pdf_path, 'pages': len(imgs)})
            except Exception as e: self.send_json({'error': str(e)}, 500)

        elif parsed.path == '/api/rename':
            try:
                renames = body.get('renames', [])
                done = 0
                # 충돌 방지: 임시 이름으로 먼저 전부 변경 후 최종 이름으로
                tmp_map = []
                for item in renames:
                    p = Path(item['old_path'])
                    tmp = p.parent / ('__tmp__' + str(done) + p.suffix)
                    p.rename(tmp)
                    tmp_map.append((tmp, p.parent / item['new_name']))
                    done += 1
                for tmp, final in tmp_map:
                    tmp.rename(final)
                self.send_json({'done': done})
            except Exception as e: self.send_json({'error': str(e)}, 500)

        elif parsed.path == '/api/split':
            if not HAS_PIL:
                self.send_json({'error': 'Pillow 필요. pip install Pillow'}, 500)
                return
            try:
                paths = body.get('paths', [])
                direction = body.get('direction', 'ltr')
                split_xs = body.get('split_xs', {})
                default_x = body.get('default_x', 0.5)
                results = []
                for img_path in paths:
                    sx = split_xs.get(img_path, default_x)
                    backup_image(img_path)
                    new_paths = split_image(img_path, direction=direction, split_x=sx)
                    Path(img_path).unlink()
                    results.extend(new_paths)
                self.send_json({'done': len(paths), 'results': results})
            except Exception as e:
                self.send_json({'error': str(e)}, 500)

        elif parsed.path == '/api/restore':
            paths = body.get('paths', [])
            done = 0
            missing = 0
            errors = []
            for p in paths:
                try:
                    restore_image(p)
                    done += 1
                except FileNotFoundError:
                    missing += 1
                except Exception as e:
                    errors.append(str(e))
            if errors:
                self.send_json({'error': errors[0]}, 500)
            else:
                self.send_json({'done': done, 'missing': missing})

        elif parsed.path == '/api/split_restore':
            if not HAS_PIL:
                self.send_json({'error': 'Pillow 필요'}, 500)
                return
            try:
                orig_path = body.get('orig_path', '')
                split_paths = body.get('split_paths', [])
                p = Path(orig_path)
                # 백업에서 원본 복원
                backup = p.parent / BACKUP_DIR_NAME / p.name
                if not backup.exists():
                    self.send_json({'error': f'백업 없음: {backup}'}, 500)
                    return
                shutil.copy2(backup, p)
                # a/b 파일 삭제
                for sp in split_paths:
                    sp_p = Path(sp)
                    if sp_p.exists():
                        sp_p.unlink()
                self.send_json({'done': True, 'orig': str(p)})
            except Exception as e:
                self.send_json({'error': str(e)}, 500)

import socket

# 전역 스레드 풀 — 썸네일 생성용 (서버 시작 시 한 번만 생성)
_thumb_executor = ThreadPoolExecutor(max_workers=8)
class ThreadPoolServer(HTTPServer):
    """스레드 풀 기반 HTTPServer — 미리 생성된 스레드가 요청을 처리"""
    allow_reuse_address = True

    def __init__(self, *args, pool_size=16, **kwargs):
        super().__init__(*args, **kwargs)
        self._pool = ThreadPoolExecutor(max_workers=pool_size)

    def process_request(self, request, client_address):
        """요청을 스레드 풀에 제출"""
        self._pool.submit(self.process_request_thread, request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    def server_close(self):
        self._pool.shutdown(wait=False)
        super().server_close()

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()

# 하위 호환 별칭
ReuseAddrServer = ThreadPoolServer

if __name__ == '__main__':
    import subprocess, sys as _sys

    # pythonw(백그라운드) 실행 시 stdout/stderr 없음 → 로그 파일로 리다이렉트
    LOG_FILE = Path(__file__).parent / 'manga_organizer.log'
    if sys.stdout is None or sys.stderr is None:
        log = open(LOG_FILE, 'w', encoding='utf-8', buffering=1)
        sys.stdout = log
        sys.stderr = log

    if not HAS_PIL:
        print("⚠  Pillow 미설치 — 이미지 편집 기능을 사용하려면:")
        print("   pip install Pillow\n")

    # 포트 충돌 시 자동으로 기존 프로세스 종료 후 재시도
    server = None
    for attempt in range(2):
        try:
            server = ThreadPoolServer((HOST, PORT), Handler, pool_size=16)
            break
        except OSError:
            if attempt == 0:
                print(f"⚠  포트 {PORT} 충돌 — 기존 프로세스 종료 시도 중...")
                try:
                    if os.name == 'nt':
                        # netstat으로 포트 점유 PID 찾아서 강제 종료
                        r = subprocess.run(['netstat', '-ano'], capture_output=True, text=True)
                        for line in r.stdout.splitlines():
                            if f':{PORT}' in line and 'LISTENING' in line:
                                pid = line.strip().split()[-1]
                                subprocess.run(['taskkill', '/PID', pid, '/F'], capture_output=True)
                    else:
                        subprocess.run(f'fuser -k {PORT}/tcp', shell=True, capture_output=True)
                    time.sleep(1)
                except Exception:
                    pass
            else:
                print(f"❌ 포트 {PORT}를 사용할 수 없습니다. 다른 프로그램을 확인하세요.")
                _sys.exit(1)

    print(f"✅ 만화 정리기 시작!")
    print(f"🌐 http://{HOST}:{PORT}")
    print(f"   종료: 브라우저의 ⏹ 종료 버튼 또는 Ctrl+C\n")
    import webbrowser
    webbrowser.open(f"http://{HOST}:{PORT}")
    _server_ref = server
    try: server.serve_forever()
    except KeyboardInterrupt: print("\n종료됨")