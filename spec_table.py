"""Прямой структурный парсинг таблиц спецификации — БЕЗ участия LLM.

Причина: таблицы характеристик товара часто содержат формулировки вроде
"раствор кислот и щелочей 40%" (защитные перчатки), "щелочь 5%" (чистящее
средство) — совершенно обычный язык описания товара, но провоцирует
ложные срабатывания фильтра безопасности некоторых LLM ("Я не могу
обсуждать эту тему"). Раз это уже готовая структурированная таблица —
её не нужно отдавать модели вообще, читаем колонки напрямую.

Работает для .docx (python-docx таблицы) и .xlsx (openpyxl листы).
Определяет таблицу спецификации по ключевым словам в заголовке — устойчиво
к разным формулировкам между закупками товаров/работ/услуг.

Таблица считается спецификацией по наименованию + (количеству ИЛИ
характеристикам) — не обязательно оба сразу. У некоторых закупок
(например рамочные договоры) в таблице позиций нет колонки "количество"
вообще — количество не фиксировано заранее, а определяется заявками по
ходу исполнения договора. Такую таблицу всё равно нужно вынести из LLM,
иначе она целиком (часто 50-100+ строк) уходит в текст и упирается в
лимит контекста модели.
"""

NAME_KEYS   = ("наименован",)
CHAR_KEYS   = ("характеристик", "требован")
UNIT_KEYS   = ("единиц", "ед.изм", "ед. изм")
QTY_KEYS    = ("количеств", "кол-во", "объем", "объём")
# "Цена за единицу" — именно ЦЕНА ЗА ЕДИНИЦУ, не "сумма"/"итого" (это total по строке,
# смешивать с ценой за штуку нельзя — вводит в заблуждение о реальной цене товара).
PRICE_KEYS  = ("цена",)
# Колонки с этими словами — цена/сумма, а не количество/единица измерения.
# Без этого фильтра "Цена за единицу измерения" ложно матчится как колонка
# "единица измерения" (оба содержат "единиц") — вот откуда была путаница цены и количества.
PRICE_EXCLUDE = ("цена", "стоимост", "сумма", "руб")
MAX_SUMMARY = 120


def _match_col(header_cells: list[str], keys: tuple[str, ...], exclude: tuple[str, ...] = ()) -> int | None:
    """Находит индекс колонки, чей заголовок содержит одно из ключевых слов
    и НЕ содержит ни одного из исключающих слов."""
    best_idx, best_len = None, -1
    for i, cell in enumerate(header_cells):
        low = cell.lower()
        if exclude and any(x in low for x in exclude):
            continue
        if any(k in low for k in keys):
            if len(cell) > best_len:
                best_idx, best_len = i, len(cell)
    return best_idx


def _detect_columns(header_cells: list[str]) -> dict | None:
    name_col = _match_col(header_cells, NAME_KEYS)
    qty_col  = _match_col(header_cells, QTY_KEYS, exclude=PRICE_EXCLUDE)
    char_col = _match_col(header_cells, CHAR_KEYS)
    if name_col is None or (qty_col is None and char_col is None):
        return None  # без названия и хотя бы количества/характеристик — не таблица спецификации
    return {
        "name": name_col,
        "qty":  qty_col,   # может быть None — например, рамочный договор без фиксированного кол-ва
        "unit": _match_col(header_cells, UNIT_KEYS, exclude=PRICE_EXCLUDE),
        "char": char_col,
        "price": _match_col(header_cells, PRICE_KEYS),  # "Цена за единицу" — конкретно за штуку, не "Сумма"
    }


def _row_to_item(cells: list[str], cols: dict) -> dict | None:
    name = cells[cols["name"]].strip() if cols["name"] < len(cells) else ""
    if not name or name.lower() in ("итого", "всего"):
        return None
    if name.isdigit():
        return None  # вторая строка-заголовок вида "1 | 2 | 3 | 4 | 5" (номера колонок), не данные
    qty   = cells[cols["qty"]].strip()   if cols["qty"]   is not None and cols["qty"]   < len(cells) else ""
    unit  = cells[cols["unit"]].strip()  if cols["unit"]  is not None and cols["unit"]  < len(cells) else ""
    char  = cells[cols["char"]].strip()  if cols["char"]  is not None and cols["char"]  < len(cells) else ""
    price = cells[cols["price"]].strip() if cols["price"] is not None and cols["price"] < len(cells) else ""
    return {"name": name, "qty": qty, "unit": unit, "summary": char[:MAX_SUMMARY], "price": price}


def extract_from_docx_tables(doc) -> tuple[list[dict], set[int]]:
    """doc — объект docx.Document. Возвращает (позиции спецификации, индексы использованных таблиц)."""
    items: list[dict] = []
    used_tables: set[int] = set()

    for t_idx, table in enumerate(doc.tables):
        if len(table.rows) < 2:
            continue
        header_cells = [c.text.strip() for c in table.rows[0].cells]
        cols = _detect_columns(header_cells)
        if not cols:
            continue

        found_any = False
        for row in table.rows[1:]:
            cells = [c.text.strip() for c in row.cells]
            item = _row_to_item(cells, cols)
            if item:
                items.append(item)
                found_any = True
        if found_any:
            used_tables.add(t_idx)

    return items, used_tables


def extract_from_xlsx_sheet(sheet) -> list[dict]:
    """sheet — лист openpyxl. Ищет строку-заголовок спецификации и парсит строки под ней."""
    items: list[dict] = []
    rows = list(sheet.iter_rows(values_only=True))
    cols = None

    for row in rows:
        cells = [str(c).strip() if c is not None else "" for c in row]
        if cols is None:
            candidate = _detect_columns(cells)
            if candidate:
                cols = candidate  # нашли строку-заголовок, дальше парсим данные
            continue
        item = _row_to_item(cells, cols)
        if item:
            items.append(item)

    return items
