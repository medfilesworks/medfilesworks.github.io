"""Non-destructive watermark renderer for medfilesworks V3.

Only binary PDFs and still PNG/JPEG/WebP images are supported. The renderer
returns NEW bytes; it never changes the source Drive file or its permissions.
"""
from __future__ import annotations

import io
import math
from functools import lru_cache
from pathlib import Path

import fitz
from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError:
    arabic_reshaper = None
    get_display = None

SUPPORTED_MIMES = {
    'application/pdf', 'image/png', 'image/jpeg', 'image/webp'
}
MAX_IMAGE_PIXELS = 50_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


def validate_config(config: dict, logo_bytes: bytes | None = None) -> dict:
    cfg = dict(config)
    mode = str(cfg.get('mode', 'text'))
    if mode not in ('text', 'logo', 'both'):
        raise ValueError('نوع العلامة المائية غير معروف')
    if mode in ('text', 'both') and not str(cfg.get('text', '')).strip():
        raise ValueError('اكتبي نص العلامة المائية')
    if len(str(cfg.get('text', ''))) > 180:
        raise ValueError('نص العلامة المائية طويل جدًا')
    if mode in ('logo', 'both') and not logo_bytes and not cfg.get('logo_file_id'):
        raise ValueError('اختاري صورة للشعار')
    cfg['opacity'] = float(cfg.get('opacity', 0.22))
    cfg['rotation'] = int(cfg.get('rotation', 30))
    cfg['scale'] = float(cfg.get('scale', 1.0))
    cfg['placement'] = str(cfg.get('placement', 'center'))
    if not 0.05 <= cfg['opacity'] <= 0.75:
        raise ValueError('الشفافية خارج المجال المسموح')
    if not -75 <= cfg['rotation'] <= 75:
        raise ValueError('زاوية الدوران خارج المجال المسموح')
    if not 0.5 <= cfg['scale'] <= 2.5:
        raise ValueError('حجم العلامة خارج المجال المسموح')
    if cfg['placement'] not in ('center', 'bottom_right', 'repeated'):
        raise ValueError('موضع العلامة غير معروف')
    ImageColor.getrgb(str(cfg.get('color', '#45546B')))
    return cfg


@lru_cache(maxsize=20)
def _font(size: int):
    candidates = (
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf',
        '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
        '/usr/share/fonts/DejaVuSans.ttf',
    )
    for item in candidates:
        if Path(item).is_file():
            return ImageFont.truetype(item, size=size)
    return ImageFont.load_default()


def _logo_image(data: bytes) -> Image.Image:
    if len(data) > 5 * 1024 * 1024:
        raise ValueError('الشعار أكبر من 5 MB')
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > MAX_IMAGE_PIXELS:
                raise ValueError('أبعاد الشعار كبيرة جدًا')
            return im.convert('RGBA')
    except (OSError, ValueError) as exc:
        raise ValueError('ملف الشعار يجب أن يكون صورة سليمة') from exc


def _stamp(width: int, height: int, cfg: dict, logo_bytes: bytes | None) -> Image.Image:
    """Create one rotated transparent mark, sized for the canvas."""
    baseline = max(22, int(min(width, height) * 0.06 * cfg['scale']))
    font = _font(baseline)
    text = str(cfg.get('text', '')) if cfg['mode'] in ('text', 'both') else ''
    if text and arabic_reshaper and get_display and any('\u0600' <= c <= '\u06ff' for c in text):
        text = get_display(arabic_reshaper.reshape(text))
    rgb = ImageColor.getrgb(str(cfg.get('color', '#45546B')))
    logo = _logo_image(logo_bytes) if logo_bytes and cfg['mode'] in ('logo', 'both') else None

    temporary = Image.new('RGBA', (1, 1), (0, 0, 0, 0))
    temp_draw = ImageDraw.Draw(temporary)
    bounds = temp_draw.textbbox((0, 0), text, font=font) if text else (0, 0, 0, 0)
    text_width = max(0, bounds[2] - bounds[0])
    text_height = max(0, bounds[3] - bounds[1])
    logo_h = 0
    logo_w = 0
    if logo is not None:
        size = int(min(width * 0.40, height * 0.28) * cfg['scale'])
        size = max(20, size)
        ratio = min(size / logo.width, size / logo.height, 1 if cfg['scale'] <= 1 else 2.5)
        logo_w = max(1, round(logo.width * ratio))
        logo_h = max(1, round(logo.height * ratio))
        logo = logo.resize((logo_w, logo_h), Image.Resampling.LANCZOS)
    gap = 20 if text and logo is not None else 0
    sw = max(1, text_width + 24, logo_w + 24)
    sh = max(1, text_height + logo_h + gap + 28)
    stamp = Image.new('RGBA', (sw, sh), (0, 0, 0, 0))
    pen = ImageDraw.Draw(stamp)
    y = 14
    if logo is not None:
        stamp.alpha_composite(logo, ((sw - logo_w)//2, y))
        y += logo_h + gap
    if text:
        pen.text(((sw - text_width)//2 - bounds[0], y - bounds[1]), text,
                 font=font, fill=(*rgb, 255))
    stamp = stamp.rotate(cfg['rotation'], resample=Image.Resampling.BICUBIC,
                         expand=True)
    alpha = stamp.getchannel('A').point(lambda a: int(a * cfg['opacity']))
    stamp.putalpha(alpha)
    return stamp


def overlay(width: int, height: int, config: dict,
            logo_bytes: bytes | None = None) -> Image.Image:
    cfg = validate_config(config, logo_bytes)
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError('أبعاد الصورة تتجاوز الحد المسموح')
    if min(width, height) < 40:
        raise ValueError('أبعاد الملف صغيرة جدًا')
    canvas = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    stamp = _stamp(width, height, cfg, logo_bytes)
    if cfg['placement'] == 'repeated':
        step_x = max(stamp.width + int(width*0.07), int(width * 0.32))
        step_y = max(stamp.height + int(height*0.08), int(height * 0.28))
        for row, y in enumerate(range(-stamp.height, height + stamp.height, step_y)):
            offset = step_x//2 if row % 2 else 0
            for x in range(-stamp.width-offset, width + stamp.width, step_x):
                canvas.alpha_composite(stamp, (x+offset, y))
    elif cfg['placement'] == 'bottom_right':
        x = max(0, width - stamp.width - max(18, int(width*0.04)))
        y = max(0, height - stamp.height - max(18, int(height*0.04)))
        canvas.alpha_composite(stamp, (x, y))
    else:
        canvas.alpha_composite(stamp, ((width-stamp.width)//2,
                                       (height-stamp.height)//2))
    return canvas


def watermark_bytes(payload: bytes, mime_type: str, config: dict,
                    logo_bytes: bytes | None = None) -> bytes:
    cfg = validate_config(config, logo_bytes)
    if mime_type not in SUPPORTED_MIMES:
        raise ValueError('يدعم Watermark Studio ملفات PDF وPNG وJPG وWebP الثابتة فقط')
    if not payload:
        raise ValueError('الملف فارغ')
    if mime_type == 'application/pdf':
        try:
            doc = fitz.open(stream=payload, filetype='pdf')
        except Exception as exc:
            raise ValueError('تعذر فتح ملف PDF') from exc
        try:
            if doc.is_encrypted:
                raise ValueError('ملفات PDF المشفرة غير مدعومة')
            if doc.get_sigflags() in (1, 3):
                raise ValueError('ملف PDF موقع رقميًا؛ إضافة العلامة ستلغي توقيعه')
            if len(doc) == 0:
                raise ValueError('PDF لا يحتوي صفحات')
            if len(doc) > 1000:
                raise ValueError('الملف يتجاوز 1000 صفحة')
            cache = {}
            for page in doc:
                rect = page.rect
                w = max(40, min(1600, round(rect.width * 1.65)))
                h = max(40, min(1600, round(rect.height * 1.65)))
                key = (w, h)
                if key not in cache:
                    im = overlay(w, h, cfg, logo_bytes)
                    buffer = io.BytesIO()
                    im.save(buffer, format='PNG', optimize=True)
                    cache[key] = (buffer.getvalue(), 0)
                image_data, xref = cache[key]
                xref = page.insert_image(rect, stream=image_data if not xref else None,
                                         xref=xref, overlay=True, keep_proportion=False)
                cache[key] = (image_data, xref)
            output = doc.tobytes(garbage=3, deflate=True)
        finally:
            doc.close()
        return output
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if getattr(image, 'n_frames', 1) > 1:
                raise ValueError('الصور المتحركة غير مدعومة')
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError('أبعاد الصورة تتجاوز الحد المسموح')
            image = ImageOps.exif_transpose(image).convert('RGBA')
            image.alpha_composite(overlay(image.width, image.height, cfg, logo_bytes))
            output = io.BytesIO()
            if mime_type == 'image/jpeg':
                image.convert('RGB').save(output, format='JPEG', quality=95, optimize=True)
            elif mime_type == 'image/webp':
                image.save(output, format='WEBP', quality=94, method=4)
            else:
                image.save(output, format='PNG', optimize=True)
            return output.getvalue()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('تعذر معالجة الصورة أو أن حجمها غير آمن') from exc


def preview_png(payload: bytes, mime_type: str, width: int = 900) -> bytes:
    if mime_type == 'application/pdf':
        doc = fitz.open(stream=payload, filetype='pdf')
        try:
            if not len(doc):
                raise ValueError('ملف PDF فارغ')
            page = doc[0]
            pix = page.get_pixmap(matrix=fitz.Matrix(min(2.0, width/page.rect.width),
                                                     min(2.0, width/page.rect.width)),
                                  alpha=False)
            return pix.tobytes('png')
        finally:
            doc.close()
    with Image.open(io.BytesIO(payload)) as image:
        image.thumbnail((width, width*2))
        result = io.BytesIO()
        image.convert('RGB').save(result, format='PNG')
        return result.getvalue()


def normalize_logo_bytes(content: bytes) -> bytes:
    """Canonicalize an uploaded PNG/JPEG logo to PNG before storing it on Drive."""
    logo = _logo_image(content)
    output = io.BytesIO()
    logo.save(output, format='PNG', optimize=True)
    result = output.getvalue()
    if len(result) > 5 * 1024 * 1024:
        raise ValueError('Prepared logo is larger than 5 MB')
    return result


def preview_first_page_pair(payload: bytes, mime_type: str, config: dict,
                            logo_bytes: bytes | None = None) -> tuple[bytes, bytes]:
    """Render a before/after first-page proof without processing all PDF pages.

    The exact same watermark configuration is later applied to every page of
    every supported file by watermark_bytes during staging.
    """
    validate_config(config, logo_bytes)
    if mime_type not in SUPPORTED_MIMES:
        raise ValueError('Unsupported PDF/image format for sample preview')
    if mime_type == 'application/pdf':
        source = fitz.open(stream=payload, filetype='pdf')
        try:
            if source.is_encrypted:
                raise ValueError('Encrypted PDFs are not supported')
            if source.get_sigflags() in (1, 3):
                raise ValueError('Signed PDF cannot be watermarked safely')
            if source.page_count == 0:
                raise ValueError('PDF has no pages')
            sample = fitz.open()
            try:
                sample.insert_pdf(source, from_page=0, to_page=0)
                only_first = sample.tobytes(garbage=3, deflate=True)
            finally:
                sample.close()
        finally:
            source.close()
        payload = only_first
    before = preview_png(payload, mime_type)
    after = preview_png(watermark_bytes(payload, mime_type, config, logo_bytes), mime_type)
    return before, after
