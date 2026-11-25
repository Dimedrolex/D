import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:10809")
os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:10809")

import cv2
import numpy as np
import pymupdf
from google import genai
from google.genai import types
from rapidocr_onnxruntime import RapidOCR

SKILL_RULES = """Ты — профессиональный технический переводчик инженерной документации (вибростенды ETS Solutions, усилители мощности, логические модули, электросхемы, мануалы).
Переводи текст на грамотный технический русский язык без машинной кальки.

Правила перевода:
1. Терминология:
   - shaker -> вибростенд
   - amplifier -> усилитель
   - armature -> якорь
   - slip table -> стол скольжения
   - Power Module -> силовой модуль
   - Logic Module -> логический модуль
   - test bench -> испытательный стенд
   - Air Isolation Feet -> пневмоопоры
   - Mounting Plate -> монтажная плита
   - Pressure Regulator -> регулятор давления
   - Manifold Block / Mannifold Block -> блок коллектора
   - Drawing Number -> Номер чертежа
   - Designed by -> Разработал
   - Checked by -> Проверил
   - Approved by -> Утвердил
   - Signature -> Подпись
   - Checked -> Проверено
   - Edition -> Изм.
   - Sheet -> Лист
   - Scale -> Масштаб
   - Date -> Дата
   - Title / Tile -> Наименование
   - RevNo -> № изм.
   - Revision note -> Примечание к изм.
2. Не переводи позиционные обозначения (E1/TB1, SA2071, SA2171, SA194, J1, J2, J3, KM1, QF), маркировки контактов, номиналы предохранителей (Fu 80A, Fu 2A), сечения проводов, единицы измерения, коды моделей, артикулы, даты и инициалы.
3. Явные опечатки оригинала и OCR исправляй молча по смыслу.
4. Формулируй перевод максимально компактно, чтобы он свободно помещался в исходные рамки текста, таблицы и подписи на рисунках.
"""

FONT_REGULAR = r"C:\Windows\Fonts\arial.ttf"
FONT_BOLD = r"C:\Windows\Fonts\arialbd.ttf"
DPI = 300
SCALE = DPI / 72.0

IGNORE_BRAND_WORDS = {
    "ets", "solutions", "ets solutions", "dynamic", "technology", "advancing technology",
}


def load_env_file(env_path: Path):
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and not os.environ.get(key):
                os.environ[key] = val


load_env_file(Path(".env"))
load_env_file(Path(__file__).resolve().with_name(".env"))

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    print("ОШИБКА: Не найден GEMINI_API_KEY в переменных среды или .env", file=sys.stderr)
    sys.exit(1)

extra_rules = ""
for md_candidate in (Path("GEMINI.md"), Path(__file__).resolve().with_name("GEMINI.md")):
    if md_candidate.exists():
        extra_rules = md_candidate.read_text(encoding="utf-8").strip()
        break

SYSTEM_INSTRUCTION = f"{SKILL_RULES}\n\n{extra_rules}".strip() if extra_rules else SKILL_RULES

client = genai.Client(api_key=API_KEY)
ocr_engine = RapidOCR(
    det_limit_side_len=2560,
    det_db_thresh=0.15,
    det_db_box_thresh=0.3,
    det_db_unclip_ratio=1.6,
)


def discover_models():
    try:
        available = [
            m.name.replace("models/", "")
            for m in client.models.list()
            if "generateContent" in (m.supported_actions or [])
            and "flash" in m.name.lower()
            and not any(x in m.name.lower() for x in ("tts", "image", "omni", "2.5", "2.0"))
        ]
        available.reverse()
        if available:
            return available
    except Exception as e:
        print(f"[WARN] Не удалось получить список моделей: {e}")
    return ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]


MODELS_PRIORITY = discover_models()
current_model_idx = 0


def should_translate(text: str) -> bool:
    s = text.strip()
    if len(s) < 2 or s.lower() in IGNORE_BRAND_WORDS:
        return False
    if re.fullmatch(r"[\d\s.,:;+\-*/()\[\]{}=<>°²³%#&@!?'\"\\|_~^]+", s):
        return False
    if re.fullmatch(r"[A-Z]{1,3}", s) and s not in ("NA", "NO", "NC", "ON", "OFF"):
        return False
    if re.fullmatch(r"[A-Z0-9]{1,6}[-/][A-Z0-9+\-]{1,6}", s):
        return False
    if re.fullmatch(r"Fu\s*\d+A", s, re.IGNORECASE):
        return False
    if re.fullmatch(r"\d{4}/\d{2}/\d{2}", s):
        return False
    return bool(re.search(r"[A-Za-z\u4e00-\u9fff]{2,}", s))


def normalize_angle(raw_deg: float) -> int:
    deg = raw_deg % 360
    if 45 <= deg < 135:
        return 90
    if 135 <= deg < 225:
        return 180
    if 225 <= deg < 315:
        return 270
    return 0


def int_to_rgb(color_int: int) -> tuple[float, float, float]:
    r = ((color_int >> 16) & 255) / 255.0
    g = ((color_int >> 8) & 255) / 255.0
    b = (color_int & 255) / 255.0
    return (r, g, b)


def sample_bg_color(img_bgr: np.ndarray, px_box: list[int]) -> tuple[float, float, float]:
    """Определяет реальный цвет фона по контуру бокса, чтобы не оставлять белых заплаток на цветных схемах."""
    h, w = img_bgr.shape[:2]
    x1, y1, x2, y2 = max(0, px_box[0] - 2), max(0, px_box[1] - 2), min(w - 1, px_box[2] + 2), min(h - 1, px_box[3] + 2)
    border_pixels = []
    if y1 < h:
        border_pixels.append(img_bgr[y1, x1:x2 + 1])
    if y2 < h:
        border_pixels.append(img_bgr[y2, x1:x2 + 1])
    if x1 < w:
        border_pixels.append(img_bgr[y1:y2 + 1, x1])
    if x2 < w:
        border_pixels.append(img_bgr[y1:y2 + 1, x2])
    if not border_pixels:
        return (1.0, 1.0, 1.0)
    all_px = np.vstack(border_pixels)
    med_bgr = np.median(all_px, axis=0)
    return (float(med_bgr[2]) / 255.0, float(med_bgr[1]) / 255.0, float(med_bgr[0]) / 255.0)


def rect_overlap_ratio(r1: pymupdf.Rect, r2: pymupdf.Rect) -> float:
    inter = r1 & r2
    if inter.is_empty:
        return 0.0
    min_area = min(r1.get_area(), r2.get_area())
    return inter.get_area() / min_area if min_area > 0 else 0.0


def extract_page_blocks(page: pymupdf.Page, run_ocr: bool = True) -> list[dict]:
    """
    1. Извлекает нативный текст PDF, склеивая смежные строки одного абзаца/ячейки.
    2. Прогоняет OCR в памяти и добавляет ТОЛЬКО те надписи, которых нет в текстовом слое (текст на рисунках и схемах).
    """
    blocks = []
    native_rects = []

    # --- 1. Нативный текстовый слой (в визуальных координатах, без двойного поворота) ---
    text_dict = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    for b in text_dict.get("blocks", []):
        if "lines" not in b:
            continue
        # Группируем строки внутри блока по углу и близости кегля/интервала
        subgroups = []
        for l in b["lines"]:
            spans = [s for s in l["spans"] if s["text"].strip()]
            if not spans:
                continue
            line_text = " ".join(s["text"].strip() for s in spans)
            dir_x, dir_y = l["dir"]
            angle = normalize_angle(np.degrees(np.arctan2(dir_y, dir_x)))
            rect = pymupdf.Rect(l["bbox"])
            avg_size = float(np.median([s.get("size", 9.0) for s in spans]))
            is_bold = any(bool(s.get("flags", 0) & 16) or "bold" in s.get("font", "").lower() for s in spans)
            color = int_to_rgb(spans[0].get("color", 0))

            if (
                subgroups
                and subgroups[-1]["angle"] == angle
                and angle == 0
                and abs(subgroups[-1]["fontsize"] - avg_size) <= max(1.5, avg_size * 0.2)
                and (rect.y0 - subgroups[-1]["rect"].y1) <= avg_size * 1.4
                and abs(rect.x0 - subgroups[-1]["rect"].x0) <= avg_size * 3.0
            ):
                subgroups[-1]["texts"].append(line_text)
                subgroups[-1]["rect"] |= rect
            else:
                subgroups.append({
                    "texts": [line_text],
                    "rect": rect,
                    "angle": angle,
                    "fontsize": avg_size,
                    "is_bold": is_bold,
                    "color": color,
                })

        for sg in subgroups:
            full_text = " ".join(sg["texts"]).strip()
            native_rects.append(sg["rect"])
            if not should_translate(full_text):
                continue
            blocks.append({
                "source": "NATIVE",
                "text": full_text,
                "rect": sg["rect"],
                "angle": sg["angle"],
                "fontsize": sg["fontsize"],
                "is_bold": sg["is_bold"],
                "color": sg["color"],
                "bg_color": None,
            })

    # --- 2. OCR только для надписей внутри рисунков/схем/кривых ---
    if run_ocr:
        pix = page.get_pixmap(dpi=DPI, alpha=False)
        img_rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
        img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)

        ocr_res, _ = ocr_engine(img_bgr)
        if ocr_res:
            for poly, text, conf in ocr_res:
                text = str(text).strip()
                if float(conf) < 0.40 or not should_translate(text):
                    continue
                xs, ys = [p[0] for p in poly], [p[1] for p in poly]
                px_box = [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]
                pt_rect = pymupdf.Rect(px_box[0] / SCALE, px_box[1] / SCALE, px_box[2] / SCALE, px_box[3] / SCALE)

                # Если этот текст уже есть в текстовом слое — пропускаем
                if any(rect_overlap_ratio(pt_rect, nr) > 0.20 for nr in native_rects):
                    continue

                angle = normalize_angle(np.degrees(np.arctan2(poly[1][1] - poly[0][1], poly[1][0] - poly[0][0])))
                char_h_pt = (pt_rect.width if angle in (90, 270) else pt_rect.height) * 0.78
                bg_col = sample_bg_color(img_bgr, px_box)
                # Если фон тёмный — пишем белым, иначе чёрным
                luminance = 0.299 * bg_col[0] + 0.587 * bg_col[1] + 0.114 * bg_col[2]
                fg_col = (1.0, 1.0, 1.0) if luminance < 0.45 else (0.0, 0.0, 0.0)

                blocks.append({
                    "source": "IMAGE_OCR",
                    "text": text,
                    "rect": pt_rect,
                    "angle": angle,
                    "fontsize": min(12.0, max(5.0, char_h_pt)),
                    "is_bold": False,
                    "color": fg_col,
                    "bg_color": bg_col,
                })

    return blocks


def call_gemini_json(prompt: str) -> str:
    global current_model_idx
    config = types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.1)
    for _ in range(4):
        while current_model_idx < len(MODELS_PRIORITY):
            model_name = MODELS_PRIORITY[current_model_idx]
            for attempt in range(2):
                try:
                    resp = client.models.generate_content(model=model_name, contents=prompt, config=config)
                    time.sleep(0.8)
                    if resp.text and resp.text.strip():
                        return resp.text.strip()
                    raise RuntimeError("Пустой ответ")
                except Exception as e:
                    err = str(e)
                    if any(k in err for k in ("404", "429", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE")):
                        print(f"    [{model_name}] Лимит/недоступна, переключаюсь...")
                        current_model_idx += 1
                        break
                    print(f"    [{model_name}] Ошибка ({attempt + 1}/2): {e}")
                    time.sleep(2.5)
            else:
                current_model_idx += 1
        print("  Все модели в лимите. Пауза 25 сек...")
        time.sleep(25)
        current_model_idx = 0
    raise RuntimeError("API недоступно после всех попыток.")


def translate_unique_texts(unique_texts: list[str], cache: dict, cache_path: Path, batch_size: int = 30):
    missing = [t for t in unique_texts if t not in cache]
    if not missing:
        return
    print(f"Новых уникальных фрагментов для перевода: {len(missing)}")
    for start in range(0, len(missing), batch_size):
        chunk = missing[start:start + batch_size]
        payload = [{"id": i, "text": txt} for i, txt in enumerate(chunk, 1)]
        for _ in range(3):
            if not payload:
                break
            prompt = (
                "Переведи на технический русский язык значения 'text' для каждого объекта из JSON-списка. "
                "Верни ТОЛЬКО валидный JSON-массив вида [{\"id\": 1, \"ru\": \"Перевод\"}] без markdown и комментариев:\n"
                + json.dumps(payload, ensure_ascii=False)
            )
            raw = call_gemini_json(prompt)
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
            match = re.search(r"\[.*\]", cleaned, re.DOTALL)
            if not match:
                continue
            try:
                parsed = json.loads(match.group(0))
            except Exception:
                continue
            returned = {
                int(item["id"]): str(item["ru"]).strip()
                for item in parsed
                if isinstance(item, dict) and "id" in item and "ru" in item and str(item["ru"]).strip()
            }
            next_payload = []
            for p in payload:
                if p["id"] in returned:
                    cache[p["text"]] = returned[p["id"]]
                else:
                    next_payload.append(p)
            payload = next_payload

        # Сохраняем кэш после каждого батча
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  Переведено: {min(start + batch_size, len(missing))}/{len(missing)}")


def insert_fitted_text(page: pymupdf.Page, b: dict, ru_text: str):
    rect = pymupdf.Rect(b["rect"]) & page.rect
    if rect.is_empty or not ru_text:
        return

    font_file = FONT_BOLD if b["is_bold"] and os.path.exists(FONT_BOLD) else FONT_REGULAR
    font_name = "arialb" if b["is_bold"] else "arial"
    orig_size = max(5.0, float(b["fontsize"]))
    min_size = max(4.2, orig_size * 0.52)
    rot = b["angle"]

    # Сначала пробуем ровно в исходный бокс, затем допускаем микро-запас по высоте (+1.5pt),
    # но НЕ расширяем по ширине, чтобы не наползать на соседние колонки и рисунки.
    try_rects = [
        rect,
        (pymupdf.Rect(rect.x0, rect.y0 - 0.5, rect.x1, rect.y1 + 1.5) & page.rect) if rot == 0 else rect,
    ]

    for target_rect in try_rects:
        size = orig_size
        while size >= min_size:
            rc = page.insert_textbox(
                target_rect,
                ru_text,
                fontsize=size,
                fontname=font_name,
                fontfile=font_file,
                color=b["color"],
                align=pymupdf.TEXT_ALIGN_LEFT,
                rotate=rot,
            )
            if rc >= 0:
                return
            size -= 0.4

    # Фолбэк, если текст экстремально длинный
    page.insert_textbox(
        try_rects[-1],
        ru_text,
        fontsize=min_size,
        fontname=font_name,
        fontfile=font_file,
        color=b["color"],
        align=pymupdf.TEXT_ALIGN_LEFT,
        rotate=rot,
    )


def process_pdf(pdf_path: str, pages_arg: str | None = None, no_ocr: bool = False, rebuild_only: bool = False):
    doc = pymupdf.open(pdf_path)
    total = len(doc)

    if pages_arg:
        selected = set()
        for part in pages_arg.split(","):
            if "-" in part:
                a, b = map(int, part.split("-", 1))
                selected.update(range(a, b + 1))
            else:
                selected.add(int(part))
        page_indices = [p - 1 for p in sorted(selected) if 1 <= p <= total]
    else:
        page_indices = list(range(total))

    cache_path = Path(os.path.splitext(pdf_path)[0] + "_translations.json")
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    print(f"Анализ документа: {pdf_path} (страниц в работе: {len(page_indices)}/{total})")
    page_blocks_map = {}
    unique_texts = []
    seen = set()

    for idx in page_indices:
        blocks = extract_page_blocks(doc[idx], run_ocr=not no_ocr)
        page_blocks_map[idx] = blocks
        n_nat = sum(1 for b in blocks if b["source"] == "NATIVE")
        n_ocr = sum(1 for b in blocks if b["source"] == "IMAGE_OCR")
        print(f"  Стр. {idx + 1}: текст={n_nat}, надписи на рисунках={n_ocr}")
        for b in blocks:
            if b["text"] not in seen:
                seen.add(b["text"])
                unique_texts.append(b["text"])

    if not rebuild_only:
        translate_unique_texts(unique_texts, cache, cache_path)

    print("Сборка векторного PDF...")
    for idx in page_indices:
        page = doc[idx]
        blocks = page_blocks_map[idx]

        # 1. Прозрачно удаляем только нативный английский текст (фоны, таблицы и картинки остаются)
        has_native = False
        for b in blocks:
            if b["source"] == "NATIVE" and b["text"] in cache:
                page.add_redact_annot(b["rect"], fill=None)
                has_native = True
        if has_native:
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_GRAPHIC_NONE,
            )

        # 2. Перекрываем надписи на рисунках в цвет их реального фона
        for b in blocks:
            if b["source"] == "IMAGE_OCR" and b["text"] in cache:
                bg = b["bg_color"] or (1.0, 1.0, 1.0)
                page.draw_rect(b["rect"], color=bg, fill=bg)

        # 3. Вписываем русский перевод единым шрифтом
        for b in blocks:
            ru = cache.get(b["text"])
            if ru:
                insert_fitted_text(page, b, ru)

    suffix = f"_p{pages_arg.replace(',', '_')}_RU.pdf" if pages_arg else "_RU.pdf"
    out_path = os.path.splitext(pdf_path)[0] + suffix
    if pages_arg:
        out_doc = pymupdf.open()
        for idx in page_indices:
            out_doc.insert_pdf(doc, from_page=idx, to_page=idx)
        out_doc.save(out_path, garbage=4, deflate=True)
    else:
        doc.save(out_path, garbage=4,