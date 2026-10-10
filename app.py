#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XiaoZhi News Manager
====================
Лёгкое трей-приложение для Windows: собирает русскоязычные RSS-ленты,
управляет заметками (каждая заметка — отдельный .md-файл), предоставляет
веб-интерфейс на Flask и MCP-сервер для голосового AI-ассистента XiaoZhi
(куб на ESP32, подключается через Astra Cloud).

Архитектура — один процесс pythonw:
  * pystray — основной поток (если не --no-tray / не DOCKER)
  * tkinter — окно быстрой заметки (отдельные потоки по запросу)
  * Flask (порт 5000) — daemon-поток
  * FastMCP (порт 8000, transport=sse) — daemon-поток
  * RSS auto-refresh — daemon-поток
"""

import os
import sys
import json
import time
import threading
import logging
import re
import html
import traceback
import webbrowser
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

# pythonw: stdout/stderr = None, а uvicorn/логирование требуют файловых объектов
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Feature detection (опциональные зависимости)
# ---------------------------------------------------------------------------
try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

try:
    from flask import Flask, request, jsonify, render_template_string, redirect
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

try:
    from mcp.server.fastmcp import FastMCP
    HAS_MCP = True
except ImportError:
    HAS_MCP = False

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

try:
    import tkinter as tk
    from tkinter import ttk
    HAS_TK = True
except ImportError:
    HAS_TK = False

try:
    from rapidfuzz import fuzz
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------
APP_NAME = "XiaoZhi News Manager"
APP_VERSION = "1.0.0"

# Базовая директория: рядом с app.py
BASE_DIR = Path(__file__).resolve().parent
if os.environ.get("DOCKER") == "true":
    DATA_DIR = BASE_DIR / "data"
    DEFAULT_NOTES_DIR = BASE_DIR / "notes"
else:
    DATA_DIR = BASE_DIR
    DEFAULT_NOTES_DIR = BASE_DIR / "notes"

STATIC_DIR = BASE_DIR / "static"
ICON_PATH = STATIC_DIR / "icon.png"
LOG_PATH = BASE_DIR / "app.log"
CONFIG_PATH = DATA_DIR / "config.json"
FEEDS_PATH = DATA_DIR / "feeds.json"

# Цвета трея-иконки
ICON_BG = (26, 26, 46)       # #1a1a2e
ICON_FG = (255, 255, 255)     # white

# ---------------------------------------------------------------------------
# Дефолтная конфигурация
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "notes_dir": str(DEFAULT_NOTES_DIR),
    "theme": "dark",
    "feeds_file": "feeds.json",
    "web_port": 5000,
    "mcp_port": 8000,
    "cache_ttl": 600,
    "news_depth": 10,
    "news_count": 7,
    "auto_refresh": True,
    "refresh_interval": 600,
    "summary_max": 800,
    "digest_summary_max": 300,
    "details_summary_max": 800,
    "full_article_max": 4000,
    "use_full_content": True,
    "xiaozhi_endpoint": "wss://api.xiaozhi.me/mcp/?token=REPLACE_WITH_YOUR_TOKEN",
    "adapter_dir": str(Path(__file__).resolve().parent.parent / "xiaozhi_mcp_adapter"),
}

# ---------------------------------------------------------------------------
# Предустановленные RSS-ленты (12 категорий, ~80 лент, только русскоязычные)
# ---------------------------------------------------------------------------
DEFAULT_FEEDS = {
    "технологии": [
        {"url": "https://habr.com/ru/rss/best/daily/", "name": "Habr", "enabled": True},
        {"url": "https://habr.com/ru/rss/hubs/3182400/", "name": "Habr IT-компании", "enabled": True},
        {"url": "https://www.3dnews.ru/rss/", "name": "3DNews", "enabled": True},
        {"url": "https://www.opennet.ru/opennews/opennews_all_utf.rss", "name": "OpenNet", "enabled": True},
        {"url": "https://dtf.ru/rss", "name": "DTF", "enabled": True},
        {"url": "https://dtf.ru/rss/tech", "name": "DTF Tech", "enabled": True},
        {"url": "https://vc.ru/rss/all/all", "name": "VC.ru", "enabled": True},
        {"url": "https://vc.ru/rss/tech", "name": "VC.ru Технологии", "enabled": True},
        {"url": "https://www.computerra.ru/rss/", "name": "Компьютерра", "enabled": False},
    ],
    "игры": [
        {"url": "https://dtf.ru/rss/games", "name": "DTF Игры", "enabled": True},
        {"url": "https://dtf.ru/rss/games/industry", "name": "DTF Игровая индустрия", "enabled": True},
        {"url": "https://www.igromania.ru/rss/", "name": "Игромания", "enabled": True},
        {"url": "https://www.igromania.ru/rss/articles/", "name": "Игромания статьи", "enabled": True},
        {"url": "https://kanobu.ru/rss/", "name": "Канобу", "enabled": True},
        {"url": "https://stopgame.ru/rss/all/", "name": "StopGame", "enabled": True},
        {"url": "https://stopgame.ru/rss/news/", "name": "StopGame новости", "enabled": True},
    ],
    "кино и сериалы": [
        {"url": "https://www.kinonews.ru/rss/", "name": "КиноНовости", "enabled": True},
        {"url": "https://dtf.ru/rss/cinema", "name": "DTF Кино", "enabled": True},
        {"url": "https://www.film.ru/rss", "name": "Film.ru", "enabled": True},
        {"url": "https://www.kinopoisk.ru/rss/news", "name": "Кинопоиск", "enabled": True},
    ],
    "музыка": [
        {"url": "https://dtf.ru/rss/music", "name": "DTF Музыка", "enabled": True},
        {"url": "https://the-flow.ru/rss", "name": "The Flow", "enabled": True},
    ],
    "спорт": [
        {"url": "https://www.sports.ru/rss/all_news.xml", "name": "Sports.ru", "enabled": True},
        {"url": "https://www.sports.ru/rss/football.xml", "name": "Sports.ru Футбол", "enabled": True},
        {"url": "https://www.sports.ru/rss/hockey.xml", "name": "Sports.ru Хоккей", "enabled": True},
        {"url": "https://www.championat.com/rss/news.xml", "name": "Чемпионат", "enabled": True},
        {"url": "https://www.championat.com/rss/football.xml", "name": "Чемпионат Футбол", "enabled": True},
        {"url": "https://dtf.ru/rss/esports", "name": "DTF Киберспорт", "enabled": True},
        {"url": "https://cybersport.ru/rss/", "name": "Cybersport.ru", "enabled": True},
    ],
    "наука": [
        {"url": "https://nplus1.ru/rss", "name": "N+1", "enabled": True},
        {"url": "https://22century.ru/rss", "name": "22 Век", "enabled": True},
        {"url": "https://scientificrussia.ru/rss", "name": "Научная Россия", "enabled": True},
        {"url": "https://www.popmech.ru/rss/", "name": "Популярная механика", "enabled": True},
        {"url": "https://elementy.ru/rss", "name": "Элементы", "enabled": True},
    ],
    "финансы": [
        {"url": "https://rssexport.rbc.ru/rbcnews/news.rss", "name": "РБК Новости", "enabled": True},
        {"url": "https://rssexport.rbc.ru/rbcnews/economics.rss", "name": "РБК Экономика", "enabled": True},
        {"url": "https://rssexport.rbc.ru/rbcnews/technology_and_media.rss", "name": "РБК Технологии", "enabled": True},
        {"url": "https://rssexport.rbc.ru/rbcnews/business.rss", "name": "РБК Бизнес", "enabled": True},
        {"url": "https://www.finanz.ru/rss/novosti", "name": "Финам", "enabled": True},
        {"url": "https://www.vedomosti.ru/rss/news", "name": "Ведомости", "enabled": True},
        {"url": "https://www.kommersant.ru/RSS/news.xml", "name": "Коммерсантъ", "enabled": True},
    ],
    "авто": [
        {"url": "https://www.kolesa.ru/rss/all", "name": "Колёса.ру", "enabled": True},
        {"url": "https://www.drive.ru/rss/", "name": "Drive.ru", "enabled": True},
        {"url": "https://www.autonews.ru/rss/", "name": "Автоновости", "enabled": True},
        {"url": "https://dtf.ru/rss/auto", "name": "DTF Авто", "enabled": True},
    ],
    "общество": [
        {"url": "https://lenta.ru/rss", "name": "Lenta.ru", "enabled": True},
        {"url": "https://lenta.ru/rss/news", "name": "Lenta.ru Новости", "enabled": True},
        {"url": "https://tass.ru/rss/v2.xml", "name": "ТАСС", "enabled": True},
        {"url": "https://www.gazeta.ru/export/rss/first.xml", "name": "Газета.Ru", "enabled": True},
    ],
    "лайфстайл": [
        {"url": "https://www.vogue.ru/rss/feed", "name": "Vogue RU", "enabled": True},
        {"url": "https://www.gq.ru/rss", "name": "GQ RU", "enabled": True},
        {"url": "https://www.cosmopolitan.ru/rss", "name": "Cosmopolitan RU", "enabled": True},
        {"url": "https://www.afisha.ru/rss/", "name": "Афиша", "enabled": True},
        {"url": "https://www.wonderzine.com/rss", "name": "Wonderzine", "enabled": True},
    ],
    "юмор и развлечения": [
        {"url": "https://dtf.ru/rss/humor", "name": "DTF Юмор", "enabled": True},
        {"url": "https://pikabu.ru/rss/", "name": "Пикабу", "enabled": True},
        {"url": "https://www.factroom.ru/rss/", "name": "Фактрум", "enabled": True},
    ],
    "психология и саморазвитие": [
        {"url": "https://www.psychologies.ru/rss/", "name": "Psychologies RU", "enabled": True},
        {"url": "https://dtf.ru/rss/self-development", "name": "DTF Саморазвитие", "enabled": True},
        {"url": "https://tinkoff-journal.ru/feed/", "name": "Т-Ж Журнал", "enabled": True},
    ],
}

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
def setup_logging():
    """Настраивает логирование в файл app.log."""
    log_format = "[%(asctime)s] %(levelname)s %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    try:
        logging.basicConfig(
            level=logging.INFO,
            format=log_format,
            datefmt=date_format,
            handlers=[
                logging.FileHandler(str(LOG_PATH), encoding="utf-8"),
            ],
        )
    except Exception:
        # Если файл недоступен, используем только stderr
        logging.basicConfig(level=logging.INFO, format=log_format, datefmt=date_format)
    logging.info("=== Старт %s v%s ===", APP_NAME, APP_VERSION)


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
class ConfigManager:
    """Загрузка/сохранение config.json."""

    def __init__(self):
        self.config = dict(DEFAULT_CONFIG)
        self.lock = threading.Lock()
        self.load()

    def load(self):
        try:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    user = json.load(f)
                for k, v in user.items():
                    self.config[k] = v
                logging.info("Конфиг загружен: %s", CONFIG_PATH)
            else:
                self.save()
                logging.info("Создан дефолтный конфиг: %s", CONFIG_PATH)
        except Exception as e:
            logging.error("Не удалось загрузить конфиг: %s", e)

    def save(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error("Не удалось сохранить конфиг: %s", e)

    def get(self, key, default=None):
        with self.lock:
            return self.config.get(key, default)

    def set(self, key, value):
        with self.lock:
            self.config[key] = value
        self.save()

    def update(self, data):
        with self.lock:
            for k, v in data.items():
                if k in self.config or k in DEFAULT_CONFIG:
                    self.config[k] = v
        self.save()

    def all(self):
        with self.lock:
            return dict(self.config)

    def reset(self):
        with self.lock:
            self.config = dict(DEFAULT_CONFIG)
        self.save()


CONFIG = ConfigManager()


def notes_dir_path():
    """Возвращает Path к папке заметок (создаёт если нет)."""
    p = Path(CONFIG.get("notes_dir", str(DEFAULT_NOTES_DIR)))
    if not p.is_absolute():
        p = BASE_DIR / p
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logging.error("Не удалось создать папку заметок %s: %s", p, e)
    return p


def _sync_adapter_endpoint(endpoint):
    """Прокидывает endpoint в конфиг xiaozhi_mcp_adapter (mcp_config.json)
    и просит работающий адаптер перезагрузиться. Ошибки не критичны."""
    endpoint = (endpoint or "").strip()
    if not endpoint.startswith(("wss://", "ws://")):
        return False
    adapter_dir = Path(CONFIG.get("adapter_dir", "") or "")
    if not adapter_dir.is_absolute():
        adapter_dir = BASE_DIR / adapter_dir
    cfg_path = adapter_dir / "mcp_config.json"
    try:
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        else:
            cfg = {}
        cfg.setdefault("upstreams", [])
        cfg.setdefault("mcpServers", {})
        cfg.setdefault("bindings", [])
        # endpoint в первый (или единственный) upstream
        if cfg["upstreams"]:
            cfg["upstreams"][0]["endpoint"] = endpoint
        else:
            cfg["upstreams"] = [{"name": "xiaozhi", "endpoint": endpoint}]
        # убеждаемся, что наш новостной SSE-сервер описан и привязан
        name = cfg["upstreams"][0].get("name", "xiaozhi")
        cfg["mcpServers"].setdefault(
            "xiaozhi-news", {"type": "sse", "url": "http://localhost:8001/sse"}
        )
        if not any(b.get("upstream") == name and b.get("server") == "xiaozhi-news"
                   for b in cfg["bindings"]):
            cfg["bindings"].append({"upstream": name, "server": "xiaozhi-news"})
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error("Не удалось обновить конфиг адаптера %s: %s", cfg_path, e)
        return False
    # best-effort: горячий reload работающего адаптера
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:8765/api/reload", method="POST", data=b""
        )
        urllib.request.urlopen(req, timeout=3)
        logging.info("Адаптер перезагружен с новым endpoint")
    except Exception as e:
        logging.info("Адаптер не запущен или не ответил (перезапустите его): %s", e)
    return True


# ---------------------------------------------------------------------------
# Менеджер лент (feeds.json)
# ---------------------------------------------------------------------------
class FeedsManager:
    """Загрузка/сохранение/модификация feeds.json."""

    def __init__(self):
        self.feeds = {}
        self.lock = threading.Lock()
        self.load()

    def load(self):
        try:
            if FEEDS_PATH.exists():
                with open(FEEDS_PATH, "r", encoding="utf-8") as f:
                    self.feeds = json.load(f)
                logging.info("Ленты загружены: %s", FEEDS_PATH)
            else:
                self.feeds = json.loads(json.dumps(DEFAULT_FEEDS))  # deep copy
                self.save()
                logging.info("Создан дефолтный feeds.json: %s", FEEDS_PATH)
        except Exception as e:
            logging.error("Не удалось загрузить ленты: %s", e)
            self.feeds = json.loads(json.dumps(DEFAULT_FEEDS))

    def save(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(FEEDS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.feeds, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error("Не удалось сохранить ленты: %s", e)

    def all(self):
        with self.lock:
            return json.loads(json.dumps(self.feeds))

    def categories(self):
        with self.lock:
            return sorted(self.feeds.keys())

    def active_feeds(self, category=""):
        """Возвращает [(cat, feed_dict), ...] только активные."""
        out = []
        with self.lock:
            cats = [category] if category and category in self.feeds else list(self.feeds.keys())
            for c in cats:
                for feed in self.feeds.get(c, []):
                    if feed.get("enabled", True):
                        out.append((c, feed))
        return out

    def add_feed(self, url, name, category):
        with self.lock:
            cat = category.strip() if category else "разное"
            if cat not in self.feeds:
                self.feeds[cat] = []
            # не дублируем URL
            for f in self.feeds[cat]:
                if f.get("url") == url:
                    return False
            self.feeds[cat].append({"url": url, "name": name or url, "enabled": True})
        self.save()
        return True

    def delete_feed(self, category, url):
        with self.lock:
            if category in self.feeds:
                self.feeds[category] = [f for f in self.feeds[category] if f.get("url") != url]
                if not self.feeds[category]:
                    # оставляем пустую категорию — пусть пользователь сам удалит
                    pass
        self.save()

    def delete_category(self, category):
        with self.lock:
            if category in self.feeds:
                del self.feeds[category]
        self.save()

    def toggle_feed(self, category, url, enable):
        with self.lock:
            if category in self.feeds:
                for f in self.feeds[category]:
                    if f.get("url") == url:
                        f["enabled"] = bool(enable)
        self.save()

    def toggle_category(self, category, enable):
        with self.lock:
            if category in self.feeds:
                for f in self.feeds[category]:
                    f["enabled"] = bool(enable)
        self.save()

    def restore_defaults(self):
        with self.lock:
            self.feeds = json.loads(json.dumps(DEFAULT_FEEDS))
        self.save()

    def stats(self):
        with self.lock:
            cats = len(self.feeds)
            total = 0
            active = 0
            for c, feeds in self.feeds.items():
                total += len(feeds)
                active += sum(1 for f in feeds if f.get("enabled", True))
        return {"categories": cats, "feeds": total, "active_feeds": active}


FEEDS = FeedsManager()


# ---------------------------------------------------------------------------
# Менеджер заметок (каждая заметка = отдельный .md)
# ---------------------------------------------------------------------------
class NoteManager:
    """Работа с заметками в папке notes/."""

    def __init__(self):
        self.lock = threading.Lock()

    def _safe_filename(self, title):
        """Безопасное имя файла из заголовка (русские буквы допустимы)."""
        if not title:
            title = "Без заголовка"
        name = title.strip()
        # запрещённые символы в Windows
        name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name)
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            name = "Без заголовка"
        if len(name) > 100:
            name = name[:100].rstrip()
        return name + ".md"

    def _unique_path(self, title):
        """Если файл существует — добавить timestamp к имени."""
        base = self._safe_filename(title)
        path = notes_dir_path() / base
        if not path.exists():
            return path
        stem = path.stem
        ts = datetime.now().strftime("_%Y%m%d_%H%M%S")
        path = notes_dir_path() / (stem + ts + ".md")
        return path

    def _format_note(self, title, content, source="трей"):
        ts = datetime.now().strftime("%d.%m.%Y %H:%M")
        return f"# {title}\n\n*Создана: {ts}*\n*Источник: {source}*\n\n{content}\n"

    def add_note(self, title, content, source="трей"):
        """Создаёт заметку. Возвращает имя файла."""
        with self.lock:
            try:
                title = (title or "").strip() or "Без заголовка"
                content = content or ""
                path = self._unique_path(title)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(self._format_note(title, content, source), encoding="utf-8")
                logging.info("Заметка создана: %s", path.name)
                return path.name
            except Exception as e:
                logging.error("Не удалось создать заметку: %s", e)
                return None

    def update_note(self, path, content):
        """Обновляет содержимое существующей заметки (сохраняет заголовок)."""
        with self.lock:
            try:
                p = notes_dir_path() / path
                if not p.exists():
                    return False
                text = p.read_text(encoding="utf-8")
                # сохраняем шапку (до первой пустой строки после метаданных)
                lines = text.splitlines()
                header_end = 0
                empty_seen = 0
                for i, line in enumerate(lines):
                    if line.strip() == "":
                        empty_seen += 1
                        if empty_seen >= 2:
                            header_end = i + 1
                            break
                    elif line.startswith("*Источник:"):
                        header_end = i + 1
                if header_end == 0:
                    header_end = len(lines)
                header = "\n".join(lines[:header_end]).rstrip() + "\n\n"
                p.write_text(header + content + "\n", encoding="utf-8")
                logging.info("Заметка обновлена: %s", p.name)
                return True
            except Exception as e:
                logging.error("Не удалось обновить заметку %s: %s", path, e)
                return False

    def delete_note(self, path):
        with self.lock:
            try:
                p = notes_dir_path() / path
                if p.exists():
                    p.unlink()
                    logging.info("Заметка удалена: %s", p.name)
                    return True
                # частичное совпадение имени
                for f in notes_dir_path().glob("*.md"):
                    if path.lower() in f.name.lower():
                        f.unlink()
                        logging.info("Заметка удалена (частичное совпадение): %s", f.name)
                        return True
                return False
            except Exception as e:
                logging.error("Не удалось удалить заметку %s: %s", path, e)
                return False

    def list_notes(self):
        """Все заметки: [{name, date, size, title}]."""
        out = []
        try:
            d = notes_dir_path()
            d.mkdir(parents=True, exist_ok=True)
            for f in sorted(d.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
                try:
                    stat = f.stat()
                    text = f.read_text(encoding="utf-8")
                    first_line = ""
                    for line in text.splitlines():
                        if line.strip().startswith("# "):
                            first_line = line.strip()[2:]
                            break
                    if not first_line:
                        first_line = f.stem
                    out.append({
                        "name": f.name,
                        "title": first_line,
                        "date": datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m.%Y %H:%M"),
                        "size": stat.st_size,
                    })
                except Exception:
                    continue
        except Exception as e:
            logging.error("Не удалось перечислить заметки: %s", e)
        return out

    def read_note(self, path):
        """Читает заметку целиком."""
        try:
            p = notes_dir_path() / path
            if not p.exists():
                # частичное совпадение
                for f in notes_dir_path().glob("*.md"):
                    if path.lower() in f.name.lower():
                        p = f
                        break
            if not p.exists():
                return None
            return p.read_text(encoding="utf-8")
        except Exception as e:
            logging.error("Не удалось прочитать заметку %s: %s", path, e)
            return None

    def find_by_name(self, query):
        """Частичное совпадение имени. Возвращает имя файла или None."""
        q = query.lower().strip()
        if not q:
            return None
        try:
            d = notes_dir_path()
            # точное
            for f in d.glob("*.md"):
                if f.name.lower() == q or f.stem.lower() == q:
                    return f.name
            # частичное
            for f in d.glob("*.md"):
                if q in f.name.lower() or q in f.stem.lower():
                    return f.name
        except Exception:
            return None
        return None

    def search(self, query):
        """Поиск по содержимому с релевантностью."""
        q = query.lower().strip()
        if not q:
            return []
        results = []
        q_words = [w for w in re.split(r"\s+", q) if w]
        try:
            d = notes_dir_path()
            d.mkdir(parents=True, exist_ok=True)
            for f in d.glob("*.md"):
                try:
                    text = f.read_text(encoding="utf-8")
                    lower_text = text.lower()
                    score = 0
                    # совпадение в имени файла +50
                    if q in f.name.lower():
                        score += 50
                    # первая строка (заголовок) +40
                    first_line = ""
                    for line in text.splitlines():
                        if line.strip().startswith("# "):
                            first_line = line.strip()[2:].lower()
                            break
                    if q in first_line:
                        score += 40
                    # каждое слово в тексте +5 (макс +20)
                    word_score = 0
                    for w in q_words:
                        if w in lower_text:
                            word_score += 5
                    word_score = min(word_score, 20)
                    score += word_score
                    # полная фраза +30
                    if q in lower_text:
                        score += 30
                    if score <= 0:
                        continue
                    # фрагмент вокруг совпадения
                    snippet = ""
                    idx = lower_text.find(q)
                    if idx < 0 and q_words:
                        idx = lower_text.find(q_words[0])
                    if idx >= 0:
                        start = max(0, idx - 100)
                        end = min(len(text), idx + 200)
                        snippet = text[start:end].replace("\n", " ")
                        if start > 0:
                            snippet = "…" + snippet
                        if end < len(text):
                            snippet = snippet + "…"
                    else:
                        snippet = text[:300].replace("\n", " ")
                    results.append({
                        "name": f.name,
                        "score": score,
                        "snippet": snippet.strip(),
                    })
                except Exception:
                    continue
            results.sort(key=lambda x: x["score"], reverse=True)
            return results[:10]
        except Exception as e:
            logging.error("Ошибка поиска заметок: %s", e)
            return []

    def fuzzy_search(self, query, threshold=60):
        """Нечёткий поиск через rapidfuzz (fallback на обычный)."""
        if not HAS_RAPIDFUZZ:
            return self.search(query)
        q = query.lower().strip()
        if not q:
            return []
        results = []
        try:
            d = notes_dir_path()
            d.mkdir(parents=True, exist_ok=True)
            for f in d.glob("*.md"):
                try:
                    text = f.read_text(encoding="utf-8")
                    name_score = fuzz.partial_ratio(q, f.name.lower())
                    title = ""
                    for line in text.splitlines():
                        if line.strip().startswith("# "):
                            title = line.strip()[2:]
                            break
                    title_score = fuzz.partial_ratio(q, title.lower())
                    body_score = fuzz.partial_ratio(q, text.lower())
                    score = max(name_score, title_score, body_score)
                    if score >= threshold:
                        idx = text.lower().find(q)
                        snippet = ""
                        if idx >= 0:
                            start = max(0, idx - 100)
                            end = min(len(text), idx + 200)
                            snippet = text[start:end].replace("\n", " ")
                            if start > 0:
                                snippet = "…" + snippet
                            if end < len(text):
                                snippet = snippet + "…"
                        else:
                            snippet = text[:300].replace("\n", " ")
                        results.append({
                            "name": f.name,
                            "score": int(score),
                            "snippet": snippet.strip(),
                        })
                except Exception:
                    continue
            results.sort(key=lambda x: x["score"], reverse=True)
            return results[:10]
        except Exception as e:
            logging.error("Ошибка нечёткого поиска: %s", e)
            return self.search(query)


NOTES = NoteManager()


# ---------------------------------------------------------------------------
# RSS-парсинг
# ---------------------------------------------------------------------------
HTML_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&mdash;": "—",
    "&ndash;": "–",
    "&laquo;": "«",
    "&raquo;": "»",
    "&hellip;": "…",
    "&copy;": "©",
    "&reg;": "®",
    "&trade;": "™",
    "&deg;": "°",
    "&rsquo;": "'",
    "&lsquo;": "'",
    "&rdquo;": '"',
    "&ldquo;": '"',
    "&bull;": "•",
    "&prime;": "'",
    "&Prime;": '"',
}


def clean_text(text):
    """Убирает HTML-теги, entities, нормализует пробелы."""
    if not text:
        return ""
    try:
        # entities через html.unescape + кастомный словарь
        for ent, ch in HTML_ENTITIES.items():
            text = text.replace(ent, ch)
        text = html.unescape(text)
        # теги
        text = re.sub(r"<[^>]+>", " ", text)
        # числовые entities
        text = re.sub(r"&#\d+;", " ", text)
        # нормализация пробелов
        text = re.sub(r"\s+", " ", text).strip()
        return text
    except Exception:
        return str(text or "")


def relative_date(date_str):
    """Пробует разобрать дату, возвращает относительную ('5 мин назад')."""
    if not date_str:
        return ""
    try:
        # feedparser обычно даёт struct_time в entry.published_parsed
        return date_str
    except Exception:
        return date_str


def parse_time(parsed):
    """Возвращает datetime из feedparser struct_time или None."""
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6])
    except Exception:
        return None


# Кэш новостей: {(category, url): {ts, items, error}}
NEWS_CACHE = {}
NEWS_CACHE_LOCK = threading.Lock()
NEWS_MEMORY = []  # последние выданные (для get_news_details)
NEWS_MEMORY_LOCK = threading.Lock()


def parse_feed(url, category, depth=None):
    """Парсит одну ленту, возвращает список элементов или {error}."""
    if not HAS_FEEDPARSER:
        return {"error": "feedparser не установлен"}
    depth = depth or CONFIG.get("news_depth", 10)
    try:
        # timeout через пользовательский agent
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0 Safari/537.36 XiaoZhiNews/1.0"
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        parsed = feedparser.parse(raw)
        if parsed.bozo and not parsed.entries:
            return {"error": f"Ошибка парсинга: {parsed.bozo_exception}"}
        items = []
        for entry in parsed.entries[:depth]:
            title = clean_text(entry.get("title", ""))
            # content:encoded (полный текст) приоритетнее description
            summary = ""
            if CONFIG.get("use_full_content", True):
                content = entry.get("content")
                if content and isinstance(content, list) and content[0].get("value"):
                    summary = clean_text(content[0]["value"])
            if not summary:
                summary = clean_text(entry.get("summary", entry.get("description", "")))
            max_len = CONFIG.get("summary_max", 800)
            if len(summary) > max_len:
                summary = summary[:max_len].rstrip() + "…"
            link = entry.get("link", "")
            date = ""
            dt = parse_time(entry.get("published_parsed") or entry.get("updated_parsed"))
            if dt:
                date = dt.strftime("%d.%m.%Y %H:%M")
            elif entry.get("published"):
                date = entry.get("published", "")
            items.append({
                "title": title,
                "summary": summary,
                "link": link,
                "date": date,
                "category": category,
                "source_url": url,
            })
        return items
    except Exception as e:
        return {"error": str(e)}


def get_feed_cached(category, feed, force=False):
    """Возвращает закэшированные элементы или парсит свежие."""
    url = feed.get("url")
    key = (category, url)
    ttl = CONFIG.get("cache_ttl", 600)
    with NEWS_CACHE_LOCK:
        cached = NEWS_CACHE.get(key)
    now = time.time()
    if not force and cached and (now - cached.get("ts", 0)) < ttl and "items" in cached:
        return cached["items"], cached.get("error")
    result = parse_feed(url, category)
    if isinstance(result, dict) and "error" in result:
        with NEWS_CACHE_LOCK:
            NEWS_CACHE[key] = {"ts": now, "error": result["error"]}
        return [], result["error"]
    with NEWS_CACHE_LOCK:
        # подставляем имя источника
        source_name = feed.get("name", url)
        for it in result:
            it["source"] = source_name
        NEWS_CACHE[key] = {"ts": now, "items": result}
    return result, None


def collect_news(category=""):
    """Собирает новости из всех активных лент указанной категории (или всех)."""
    all_items = []
    for cat, feed in FEEDS.active_feeds(category):
        items, err = get_feed_cached(cat, feed)
        if err:
            logging.warning("Лента %s (%s): %s", feed.get("name"), cat, err)
        if items:
            all_items.extend(items)
    return all_items


def build_news_digest(count=None, category=""):
    """Чередование источников: не более 2 подряд из одного источника.
    Возвращает строку с нумерованным списком."""
    count = count or CONFIG.get("news_count", 7)
    items = collect_news(category)
    if not items:
        return "Новостей пока нет. Попробуйте обновить ленты."
    import random
    # группируем по источникам и перемешиваем: рандом среди последних depth
    by_source = {}
    for it in items:
        by_source.setdefault(it.get("source", "?"), []).append(it)
    for bucket in by_source.values():
        random.shuffle(bucket)          # случайный порядок внутри источника
    sources_order = list(by_source.keys())
    random.shuffle(sources_order)       # случайный порядок источников
    # round-robin interleaving из перемешанных (разнообразие + рандом)
    interleaved = []
    idx = 0
    while True:
        progressed = False
        for src in sources_order:
            bucket = by_source[src]
            if idx < len(bucket):
                interleaved.append(bucket[idx])
                progressed = True
            if len(interleaved) >= count:
                break
        if not progressed or len(interleaved) >= count:
            break
        idx += 1
    interleaved = interleaved[:count]
    # сохраняем в память
    with NEWS_MEMORY_LOCK:
        NEWS_MEMORY.clear()
        for it in interleaved:
            NEWS_MEMORY.append(dict(it))
    # текст
    lines = []
    digest_max = CONFIG.get("digest_summary_max", 300)
    for i, it in enumerate(interleaved, 1):
        title = it.get("title", "")
        summary = it.get("summary", "")
        if len(summary) > digest_max:
            summary = summary[:digest_max].rstrip() + "…"
        source = it.get("source", "")
        lines.append(f"{i}. {title} — {summary} — {source}")
    sources_count = len({it.get("source", "?") for it in interleaved})
    lines.append(f"\nВсего {len(interleaved)} новостей из {sources_count} источников")
    return "\n".join(lines)


class NewsMemory:
    """Обёртка над глобальной NEWS_MEMORY для совместимости с описанием."""

    def get_last(self):
        with NEWS_MEMORY_LOCK:
            return list(NEWS_MEMORY)

    def get_by_index(self, index):
        with NEWS_MEMORY_LOCK:
            if not NEWS_MEMORY:
                return None, "Сначала запросите новости (NEWS_BRIEFING)."
            if index < 1 or index > len(NEWS_MEMORY):
                return None, f"Индекс вне диапазона (1..{len(NEWS_MEMORY)})."
            return dict(NEWS_MEMORY[index - 1]), None


NEWS_MEM = NewsMemory()


# ---------------------------------------------------------------------------
# Генерация иконки трея (Pillow)
# ---------------------------------------------------------------------------
def generate_icon(path):
    """Создаёт иконку 64x64: тёмно-синий круг + белая газета."""
    try:
        from PIL import Image, ImageDraw
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # круг
        d.ellipse([(2, 2), (62, 62)], fill=ICON_BG + (255,))
        # газета — прямоугольник
        d.rectangle([(14, 16), (50, 50)], fill=ICON_FG + (255,))
        # линии (текст)
        for y in [22, 28, 34, 40, 46]:
            d.line([(18, y), (46, y)], fill=ICON_BG + (255,), width=2)
        # заголовок газеты (широкая полоса)
        d.rectangle([(18, 18), (46, 20)], fill=ICON_BG + (255,))
        img.save(str(path), "PNG")
        logging.info("Иконка сгенерирована: %s", path)
        return str(path)
    except Exception as e:
        logging.error("Не удалось сгенерировать иконку: %s", e)
        return None


def load_tray_image():
    """Загружает PIL-изображение для трея."""
    try:
        from PIL import Image
        if not ICON_PATH.exists():
            generate_icon(ICON_PATH)
        return Image.open(str(ICON_PATH))
    except Exception as e:
        logging.error("Не удалось загрузить иконку: %s", e)
        # запасная — пустое изображение
        from PIL import Image
        return Image.new("RGBA", (64, 64), ICON_BG + (255,))


# ---------------------------------------------------------------------------
# Tkinter: окно быстрой заметки и молниеносной заметки
# ---------------------------------------------------------------------------
def _tk_save_and_close(title_var, text_widget, top, source="трей"):
    title = title_var.get().strip() if hasattr(title_var, "get") else (title_var or "")
    content = text_widget.get("1.0", "end").strip() if text_widget else ""
    if not title and not content:
        top.destroy()
        return
    if not title:
        title = (content[:50] + "…") if len(content) > 50 else content
    name = NOTES.add_note(title, content, source=source)
    if name:
        _tray_notify("✅ Заметка сохранена", title)
    else:
        _tray_notify("⚠️ Ошибка", "Не удалось сохранить заметку")
    top.destroy()


def open_note_window(icon=None):
    """Окно «Быстрая заметка» ~400x300, тёмная тема."""
    if not HAS_TK:
        logging.error("tkinter недоступен")
        return

    def run():
        try:
            root = tk.Tk()
            root.title("Быстрая заметка — XiaoZhi")
            root.geometry("400x300")
            # центрируем
            root.update_idletasks()
            w, h = 400, 300
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            x = (sw - w) // 2
            y = (sh - h) // 2
            root.geometry(f"{w}x{h}+{x}+{y}")
            root.configure(bg="#1e1e2e")

            lbl = tk.Label(root, text="Заголовок", bg="#1e1e2e", fg="#cdd6f4",
                           font=("Segoe UI", 10, "bold"))
            lbl.pack(pady=(12, 2), padx=14, anchor="w")
            title_var = tk.StringVar()
            entry = tk.Entry(root, textvariable=title_var, bg="#313244", fg="#cdd6f4",
                             insertbackground="#cdd6f4", relief="flat",
                             font=("Segoe UI", 11))
            entry.pack(fill="x", padx=14, pady=(0, 8))

            lbl2 = tk.Label(root, text="Текст", bg="#1e1e2e", fg="#cdd6f4",
                            font=("Segoe UI", 10, "bold"))
            lbl2.pack(padx=14, anchor="w")
            text = tk.Text(root, bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4",
                          relief="flat", font=("Segoe UI", 11), wrap="word")
            text.pack(fill="both", expand=True, padx=14, pady=(2, 8))

            btns = tk.Frame(root, bg="#1e1e2e")
            btns.pack(pady=(0, 12))

            def save():
                _tk_save_and_close(title_var, text, root, source="трей")

            save_btn = tk.Button(btns, text="Сохранить", command=save,
                                 bg="#89b4fa", fg="#1e1e2e", relief="flat",
                                 font=("Segoe UI", 10, "bold"), padx=14, pady=4,
                                 activebackground="#b4befe", activeforeground="#1e1e2e")
            save_btn.pack(side="left", padx=6)
            cancel_btn = tk.Button(btns, text="Отмена", command=root.destroy,
                                   bg="#45475a", fg="#cdd6f4", relief="flat",
                                   font=("Segoe UI", 10), padx=14, pady=4,
                                   activebackground="#585b70")
            cancel_btn.pack(side="left", padx=6)

            entry.focus_set()
            root.bind("<Return>", lambda e: save() if e.state == 0 else None)
            root.bind("<Escape>", lambda e: root.destroy())
            root.mainloop()
        except Exception as e:
            logging.error("Ошибка окна заметки: %s\n%s", e, traceback.format_exc())

    threading.Thread(target=run, daemon=True).start()


def open_quick_note_window(icon=None):
    """Молниеносная заметка — однострочный ввод."""
    if not HAS_TK:
        logging.error("tkinter недоступен")
        return

    def run():
        try:
            root = tk.Tk()
            root.title("⚡ Молниеносная заметка — XiaoZhi")
            root.geometry("380x70")
            root.update_idletasks()
            w, h = 380, 70
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            x = (sw - w) // 2
            y = (sh - h) // 2
            root.geometry(f"{w}x{h}+{x}+{y}")
            root.configure(bg="#1e1e2e")

            entry = tk.Entry(root, bg="#313244", fg="#cdd6f4",
                             insertbackground="#cdd6f4", relief="flat",
                             font=("Segoe UI", 12))
            entry.pack(fill="x", padx=10, pady=10, ipady=4)

            def save():
                text = entry.get().strip()
                if not text:
                    root.destroy()
                    return
                title = text[:50] + ("…" if len(text) > 50 else "")
                name = NOTES.add_note(title, text, source="молниеносная")
                if name:
                    _tray_notify("✅ Заметка сохранена", title)
                root.destroy()

            entry.bind("<Return>", lambda e: save())
            entry.bind("<Escape>", lambda e: root.destroy())
            entry.focus_set()
            root.mainloop()
        except Exception as e:
            logging.error("Ошибка молниеносной заметки: %s\n%s", e, traceback.format_exc())

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Трей
# ---------------------------------------------------------------------------
_tray_icon = None
_tray_pause = {"paused": False}
_tray_stop = {"stop": False}


def _tray_notify(title, msg):
    """Уведомление в трее."""
    try:
        if _tray_icon:
            _tray_icon.notify(msg, title)
            # через 3 сек снимаем
            threading.Timer(3.0, lambda: _tray_icon and _tray_icon.remove_notification()).start()
    except Exception:
        pass


def _tray_open_web(icon=None):
    try:
        port = CONFIG.get("web_port", 5000)
        webbrowser.open(f"http://localhost:{port}/")
    except Exception as e:
        logging.error("Не удалось открыть браузер: %s", e)


def _tray_open_settings(icon=None):
    try:
        port = CONFIG.get("web_port", 5000)
        webbrowser.open(f"http://localhost:{port}/settings")
    except Exception as e:
        logging.error("Не удалось открыть настройки: %s", e)


def _tray_refresh(icon=None):
    try:
        threading.Thread(target=_force_refresh_all, daemon=True).start()
        _tray_notify("🔄 Обновление", "Новости обновляются…")
    except Exception as e:
        logging.error("Ошибка обновления из трея: %s", e)


def _force_refresh_all():
    """Принудительно парсит все активные ленты."""
    for cat, feed in FEEDS.active_feeds():
        try:
            get_feed_cached(cat, feed, force=True)
        except Exception:
            continue
    _tray_notify("✅ Готово", "Новости обновлены")


def _tray_stats(icon=None):
    try:
        s = compute_stats()
        msg = (f"Категорий: {s['categories']}\n"
               f"Лент: {s['feeds']}\n"
               f"Активных лент: {s['active_feeds']}\n"
               f"Новостей в кэше: {s['cached_news']}\n"
               f"Заметок: {s['notes']}")
        _tray_notify("ℹ️ Статистика", msg)
    except Exception as e:
        logging.error("Ошибка статистики: %s", e)


def _tray_copy_mcp(icon=None):
    try:
        port = CONFIG.get("mcp_port", 8000)
        url = f"http://localhost:{port}/sse"
        # буфер обмена через tkinter
        if HAS_TK:
            r = tk.Tk()
            r.withdraw()
            r.clipboard_clear()
            r.clipboard_append(url)
            r.update()
            r.destroy()
        _tray_notify("🌐 MCP URL скопирован", url)
    except Exception as e:
        logging.error("Не удалось скопировать MCP URL: %s", e)


def _tray_toggle_pause(icon=None):
    try:
        _tray_pause["paused"] = not _tray_pause["paused"]
        if _tray_icon:
            _update_tray_menu()
        state = "пауза" if _tray_pause["paused"] else "возобновлено"
        _tray_notify("⏯️ RSS", f"Автообновление: {state}")
    except Exception as e:
        logging.error("Ошибка переключения паузы: %s", e)


def _tray_exit(icon=None):
    try:
        _tray_stop["stop"] = True
        if _tray_icon:
            _tray_icon.stop()
    except Exception as e:
        logging.error("Ошибка выхода: %s", e)


def _tray_recent_notes_submenu(icon=None):
    """Возвращает подменю с 5 последними заметками."""
    items = NOTES.list_notes()[:5]
    if not items:
        return pystray.MenuItem("📝 (нет заметок)", None, enabled=False)
    menu_items = []
    for n in items:
        title = n["title"]
        if len(title) > 40:
            title = title[:40] + "…"
        menu_items.append(pystray.MenuItem(
            title,
            lambda _icon, _name=n["name"]: _open_note_in_browser(_name),
        ))
    return pystray.Menu(*menu_items)


def _open_note_in_browser(name):
    try:
        port = CONFIG.get("web_port", 5000)
        url = f"http://localhost:{port}/note/{urllib.parse.quote(name)}"
        webbrowser.open(url)
    except Exception as e:
        logging.error("Не удалось открыть заметку в браузере: %s", e)


def compute_stats():
    s = FEEDS.stats()
    with NEWS_CACHE_LOCK:
        cached_news = sum(len(v.get("items", [])) for v in NEWS_CACHE.values())
    s["cached_news"] = cached_news
    s["notes"] = len(NOTES.list_notes())
    return s


def _build_tray_menu():
    pause_label = "▶️ Продолжить" if _tray_pause["paused"] else "⏸️ Пауза"
    return pystray.Menu(
        pystray.MenuItem("➕ Новая заметка", open_note_window, default=False),
        pystray.MenuItem("⚡ Молниеносная заметка", open_quick_note_window),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("📰 Открыть интерфейс", _tray_open_web, default=True),
        pystray.MenuItem("🔄 Обновить новости", _tray_refresh),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("📝 Последние заметки", _tray_recent_notes_submenu()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(pause_label, _tray_toggle_pause),
        pystray.MenuItem("🔧 Настройки", _tray_open_settings),
        pystray.MenuItem("ℹ️ Статистика", _tray_stats),
        pystray.MenuItem("🌐 Копировать MCP URL", _tray_copy_mcp),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("❌ Выход", _tray_exit),
    )


def _update_tray_menu():
    """Перестраивает меню (для паузы и подменю заметок)."""
    try:
        if _tray_icon:
            _tray_icon.update_menu()
    except Exception:
        pass


def _tooltip_text():
    try:
        s = compute_stats()
        return (f"XiaoZhi News — {s['categories']} кат, {s['feeds']} лент, "
                f"{s['cached_news']} нов, {s['notes']} зам")
    except Exception:
        return APP_NAME


def run_tray():
    """Запускает pystray в основном потоке."""
    global _tray_icon
    if not HAS_TRAY:
        logging.error("pystray/Pillow не установлены — трей недоступен")
        return
    try:
        icon_image = load_tray_image()
        _tray_icon = pystray.Icon(
            "xiaozhi_news",
            icon_image,
            tooltip=_tooltip_text(),
            menu=_build_tray_menu(),
        )

        # фоновый поток обновления tooltip
        def tooltip_updater():
            while not _tray_stop["stop"]:
                try:
                    time.sleep(30)
                    if _tray_icon:
                        _tray_icon.title = _tooltip_text()
                        # обновим подменю заметок
                        _tray_icon.update_menu()
                except Exception:
                    continue

        threading.Thread(target=tooltip_updater, daemon=True).start()

        logging.info("Трей запущен")
        _tray_icon.run()
        logging.info("Трей остановлен")
    except Exception as e:
        logging.error("Ошибка трея: %s\n%s", e, traceback.format_exc())


# ---------------------------------------------------------------------------
# Flask: веб-интерфейс
# ---------------------------------------------------------------------------
def create_flask_app():
    """Создаёт Flask-приложение со всеми страницами и API."""
    if not HAS_FLASK:
        logging.error("Flask не установлен")
        return None

    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False

    # ------------------------------------------------------------------
    # HTML-шаблоны
    # ------------------------------------------------------------------
    BASE_CSS = """
    :root {
      --bg: #0d1117; --card: #161b22; --border: #30363d;
      --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff;
      --accent2: #bc8cff; --success: #40c057; --error: #f85149;
      --warning: #d29922;
    }
    body.light {
      --bg: #f6f8fa; --card: #ffffff; --border: #d0d7de;
      --text: #1f2328; --muted: #656d76; --accent: #0969da;
      --accent2: #8250df; --success: #1a7f37; --error: #cf222e;
      --warning: #9a6700;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
      background: var(--bg); color: var(--text);
      transition: background 0.3s, color 0.3s;
    }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    button {
      background: var(--card); color: var(--text);
      border: 1px solid var(--border); border-radius: 6px;
      padding: 6px 12px; cursor: pointer; font-size: 14px;
      transition: all 0.2s;
    }
    button:hover { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, var(--card)); }
    button.primary { background: var(--accent); color: var(--bg); border-color: var(--accent); }
    button.primary:hover { background: color-mix(in srgb, var(--accent) 85%, white); }
    button.danger { color: var(--error); }
    button.danger:hover { border-color: var(--error); background: color-mix(in srgb, var(--error) 12%, var(--card)); }
    input, textarea, select {
      background: var(--bg); color: var(--text);
      border: 1px solid var(--border); border-radius: 6px;
      padding: 6px 10px; font-size: 14px; font-family: inherit;
    }
    input:focus, textarea:focus, select:focus { outline: none; border-color: var(--accent); }
    .header {
      display: flex; align-items: center; gap: 12px;
      padding: 14px 20px; border-bottom: 1px solid var(--border);
      background: var(--card); position: sticky; top: 0; z-index: 10;
    }
    .header h1 { margin: 0; font-size: 20px; flex: 1; }
    .header .actions { display: flex; gap: 8px; align-items: center; }
    .nav-link {
      display: inline-flex; align-items: center;
      background: var(--card); color: var(--text);
      border: 1px solid var(--border); border-radius: 6px;
      padding: 6px 12px; font-size: 14px; transition: all 0.2s;
    }
    .nav-link:hover { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, var(--card)); text-decoration: none; }
    .cmd-list { display: flex; flex-direction: column; gap: 10px; margin-top: 12px; }
    .cmd { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
    .cmd-phrase { font-weight: 600; color: var(--accent); margin-bottom: 4px; }
    .cmd-desc { color: var(--text); font-size: 14px; line-height: 1.5; }
    .cmd-desc code { background: var(--bg); padding: 1px 5px; border-radius: 4px; font-size: 12px; }
    .container { max-width: 1100px; margin: 0 auto; padding: 20px; }
    .tabs {
      display: flex; gap: 4px; padding: 0 20px; border-bottom: 1px solid var(--border);
      background: var(--card);
    }
    .tab {
      padding: 10px 18px; cursor: pointer; border: none;
      background: transparent; color: var(--muted);
      border-bottom: 2px solid transparent; border-radius: 0;
      font-size: 15px;
    }
    .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
    .tab:hover { color: var(--text); }
    .panel { display: none; padding: 20px; }
    .panel.active { display: block; }
    .stats {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px; margin-bottom: 20px;
    }
    .stat-card {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 8px; padding: 14px;
    }
    .stat-card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
    .stat-card .value { font-size: 24px; font-weight: 600; margin-top: 4px; color: var(--accent); }
    .info-block {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 8px; padding: 14px; margin-bottom: 16px;
    }
    .info-block code {
      background: var(--bg); padding: 2px 6px; border-radius: 4px;
      color: var(--accent2); font-size: 13px;
    }
    .info-block .hint { color: var(--muted); font-size: 13px; margin-top: 6px; }
    .feed-card {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 8px; padding: 14px; margin-bottom: 12px;
    }
    .feed-card .cat-title { font-size: 17px; font-weight: 600; margin-bottom: 10px; color: var(--accent2); }
    .feed-item {
      display: flex; align-items: center; gap: 10px; padding: 8px 0;
      border-top: 1px solid var(--border); font-size: 14px;
    }
    .feed-item:first-of-type { border-top: none; }
    .feed-item .name { flex: 0 0 200px; font-weight: 500; }
    .feed-item .url { flex: 1; color: var(--muted); font-size: 12px; word-break: break-all; }
    .feed-item .status { font-size: 12px; padding: 2px 8px; border-radius: 10px; }
    .feed-item .status.on { background: color-mix(in srgb, var(--success) 25%, transparent); color: var(--success); }
    .feed-item .status.off { background: color-mix(in srgb, var(--muted) 25%, transparent); color: var(--muted); }
    .feed-item .actions { display: flex; gap: 4px; }
    .note-item {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 8px; padding: 14px; margin-bottom: 10px;
      cursor: pointer; transition: border-color 0.2s;
    }
    .note-item:hover { border-color: var(--accent); }
    .note-item .title { font-weight: 600; font-size: 15px; }
    .note-item .meta { color: var(--muted); font-size: 12px; margin-top: 4px; }
    .note-item .actions { float: right; display: flex; gap: 4px; }
    .modal-bg {
      position: fixed; inset: 0; background: rgba(0,0,0,0.6);
      display: none; align-items: center; justify-content: center; z-index: 100;
    }
    .modal-bg.show { display: flex; }
    .modal {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 10px; padding: 20px; width: 520px; max-width: 90vw;
      max-height: 85vh; overflow: auto;
    }
    .modal h2 { margin-top: 0; }
    .modal input, .modal textarea { width: 100%; margin-bottom: 10px; }
    .modal textarea { min-height: 160px; resize: vertical; }
    .modal .buttons { display: flex; gap: 8px; justify-content: flex-end; }
    .toast {
      position: fixed; bottom: 20px; right: 20px;
      background: var(--card); border: 1px solid var(--border);
      border-left: 3px solid var(--success);
      padding: 12px 18px; border-radius: 8px;
      transform: translateX(120%); transition: transform 0.3s ease;
      z-index: 200; max-width: 360px; box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    }
    .toast.show { transform: translateX(0); }
    .toast.error { border-left-color: var(--error); }
    .search-bar {
      display: flex; gap: 8px; margin-bottom: 16px;
    }
    .search-bar input { flex: 1; }
    table.feeds-table {
      width: 100%; border-collapse: collapse; font-size: 14px;
    }
    table.feeds-table th, table.feeds-table td {
      padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border);
    }
    table.feeds-table th { color: var(--muted); font-weight: 500; font-size: 13px; }
    .add-feed-form {
      display: grid; grid-template-columns: 1fr 1fr 1fr auto;
      gap: 8px; margin-bottom: 20px; align-items: end;
    }
    .preview-box {
      background: var(--bg); border: 1px solid var(--border);
      border-radius: 6px; padding: 12px; margin-top: 10px;
      font-size: 13px; max-height: 240px; overflow: auto; white-space: pre-wrap;
      display: none;
    }
    .preview-box.show { display: block; }
    .row { display: flex; gap: 10px; align-items: center; margin-bottom: 14px; }
    .row label { flex: 0 0 220px; color: var(--muted); }
    .row input, .row select { flex: 1; }
    .btn-group { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 16px; }
    .note-view {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 8px; padding: 24px; white-space: pre-wrap;
      font-size: 15px; line-height: 1.6;
    }
    .note-view h1 { margin-top: 0; color: var(--accent); }
    .back-link { margin-bottom: 16px; display: inline-block; }
    """

    PAGE_TPL = """
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ title }}</title>
  <style>""" + BASE_CSS + """</style>
</head>
<body class="{{ theme }}">
  <div class="header">
    <h1>{{ header_title }}</h1>
    <div class="actions">
      <a href="/commands" class="nav-link">🎙 Команды</a>
      <a href="/settings" class="nav-link">⚙️ Настройки</a>
      <button id="theme-btn" onclick="toggleTheme()">{{ theme_btn }}</button>
      <button class="primary" onclick="openNoteModal()">➕ Заметка</button>
    </div>
  </div>
  {% block content %}{% endblock %}
  <div id="toast" class="toast"></div>
  <div id="modal-bg" class="modal-bg">
    <div class="modal">
      <h2 id="modal-title">Новая заметка</h2>
      <input id="modal-note-title" placeholder="Заголовок">
      <textarea id="modal-note-text" placeholder="Текст заметки…"></textarea>
      <div class="buttons">
        <button onclick="closeModal()">Отмена</button>
        <button class="primary" id="modal-save-btn" onclick="saveNoteFromModal()">Сохранить</button>
      </div>
    </div>
  </div>
  <script>
    function showToast(msg, isError) {
      var t = document.getElementById('toast');
      t.textContent = msg;
      t.className = 'toast show' + (isError ? ' error' : '');
      setTimeout(function(){ t.className = 'toast' + (isError ? ' error' : ''); }, 3000);
    }
    function toggleTheme() {
      fetch('/api/settings/theme', {method:'POST'}).then(function(){
        document.body.classList.toggle('light');
        var btn = document.getElementById('theme-btn');
        btn.textContent = document.body.classList.contains('light') ? '☀️' : '🌙';
      });
    }
    function openModal() { document.getElementById('modal-bg').classList.add('show'); }
    function closeModal() { document.getElementById('modal-bg').classList.remove('show'); }
    function openNoteModal() {
      document.getElementById('modal-title').textContent = 'Новая заметка';
      document.getElementById('modal-note-title').value = '';
      document.getElementById('modal-note-text').value = '';
      document.getElementById('modal-save-btn').onclick = saveNoteFromModal;
      openModal();
    }
    function saveNoteFromModal() {
      var title = document.getElementById('modal-note-title').value.trim();
      var content = document.getElementById('modal-note-text').value.trim();
      if (!title && !content) { closeModal(); return; }
      fetch('/api/notes', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({title: title || content.slice(0,50), content: content})})
        .then(function(r){ return r.json(); })
        .then(function(d){
          if (d.ok) { showToast('Заметка сохранена'); closeModal(); loadNotes(); }
          else { showToast('Ошибка: ' + (d.error||''), true); }
        });
    }
  </script>
  {% block script %}{% endblock %}
</body>
</html>
"""

    INDEX_TPL = PAGE_TPL.replace("{% block content %}{% endblock %}", """
  <div class="container">
    <div class="stats">
      <div class="stat-card"><div class="label">Категорий</div><div class="value" id="stat-cats">—</div></div>
      <div class="stat-card"><div class="label">Лент</div><div class="value" id="stat-feeds">—</div></div>
      <div class="stat-card"><div class="label">Активных</div><div class="value" id="stat-active">—</div></div>
      <div class="stat-card"><div class="label">В кэше</div><div class="value" id="stat-cache">—</div></div>
      <div class="stat-card"><div class="label">Заметок</div><div class="value" id="stat-notes">—</div></div>
    </div>
    <div class="info-block">
      <strong>MCP endpoint:</strong> <code id="mcp-url">{{ mcp_url }}</code>
      <div class="hint">Локальный адрес MCP-сервера. Для подключения к XiaoZhi Cloud запустите <code>start_xiaozhi.bat</code> (мост mcp_pipe.py).</div>
    </div>
    <div class="tabs">
      <button class="tab active" onclick="switchTab('news')">📰 Новости</button>
      <button class="tab" onclick="switchTab('notes')">📝 Заметки</button>
      <button class="tab" onclick="switchTab('sources')">📚 Источники</button>
    </div>
    <div id="tab-news" class="panel active">
      <div class="btn-group">
        <button class="primary" onclick="loadBriefing()">▶ Сводка новостей</button>
        <button onclick="loadMorning()">☀️ Утренний дайджест</button>
        <button onclick="refreshFeeds()">🔄 Обновить</button>
      </div>
      <div id="briefing" class="preview-box"></div>
      <h3 style="margin-top:24px">Добавить ленту</h3>
      <div class="add-feed-form">
        <input id="new-feed-url" placeholder="URL ленты">
        <input id="new-feed-name" placeholder="Название">
        <input id="new-feed-cat" placeholder="Категория">
        <button class="primary" onclick="addFeed()">Добавить</button>
      </div>
      <div id="feeds-container"></div>
    </div>
    <div id="tab-notes" class="panel">
      <div class="btn-group">
        <button class="primary" onclick="openNoteModal()">➕ Новая заметка</button>
        <button onclick="changeNotesFolder()">📁 Изменить папку</button>
      </div>
      <div class="search-bar">
        <input id="note-search" placeholder="Поиск заметок…" onkeydown="if(event.key==='Enter') searchNotes()">
        <button onclick="searchNotes()">🔍 Найти</button>
      </div>
      <div id="notes-list"></div>
    </div>
    <div id="tab-sources" class="panel">
      <table class="feeds-table">
        <thead><tr><th>Категория</th><th>Название</th><th>URL</th><th>Статус</th><th>Действия</th></tr></thead>
        <tbody id="sources-tbody"></tbody>
      </table>
    </div>
  </div>
""").replace("{% block script %}{% endblock %}", """
  <script>
    function switchTab(name) {
      document.querySelectorAll('.tab').forEach(function(t){ t.classList.remove('active'); });
      document.querySelectorAll('.panel').forEach(function(p){ p.classList.remove('active'); });
      event.target.classList.add('active');
      document.getElementById('tab-' + name).classList.add('active');
      if (name === 'news') loadFeeds();
      if (name === 'notes') loadNotes();
      if (name === 'sources') loadSourcesTable();
    }
    function loadStats() {
      fetch('/api/stats').then(function(r){return r.json();}).then(function(d){
        document.getElementById('stat-cats').textContent = d.categories;
        document.getElementById('stat-feeds').textContent = d.feeds;
        document.getElementById('stat-active').textContent = d.active_feeds;
        document.getElementById('stat-cache').textContent = d.cached_news;
        document.getElementById('stat-notes').textContent = d.notes;
      });
    }
    function loadFeeds() {
      fetch('/api/feeds').then(function(r){return r.json();}).then(function(d){
        var c = document.getElementById('feeds-container');
        c.innerHTML = '';
        var cats = Object.keys(d.feeds).sort();
        cats.forEach(function(cat){
          var feeds = d.feeds[cat];
          var card = document.createElement('div'); card.className='feed-card';
          var html = '<div class="cat-title">' + cat + ' <span style="color:var(--muted);font-size:13px">(' + feeds.length + ')</span></div>';
          feeds.forEach(function(f){
            var status = f.enabled ? 'on' : 'off';
            var label = f.enabled ? 'ON' : 'OFF';
            html += '<div class="feed-item"><span class="name">' + f.name + '</span>' +
              '<span class="url">' + f.url + '</span>' +
              '<span class="status ' + status + '">' + label + '</span>' +
              '<span class="actions">' +
              '<button onclick="previewFeed(\\''+f.url+'\\',this)">👁</button>' +
              '<button onclick="toggleFeed(\\''+cat+'\\',\\''+f.url+'\\','+!f.enabled+')">'+(f.enabled?'⏸':'▶')+'</button>' +
              '<button class="danger" onclick="deleteFeed(\\''+cat+'\\',\\''+f.url+'\\')">🗑</button>' +
              '</span></div>';
          });
          card.innerHTML = html;
          c.appendChild(card);
        });
      });
    }
    function loadSourcesTable() {
      fetch('/api/feeds').then(function(r){return r.json();}).then(function(d){
        var tb = document.getElementById('sources-tbody');
        tb.innerHTML = '';
        Object.keys(d.feeds).sort().forEach(function(cat){
          d.feeds[cat].forEach(function(f){
            var tr = document.createElement('tr');
            tr.innerHTML = '<td>'+cat+'</td><td>'+f.name+'</td><td style="font-size:12px;color:var(--muted)">'+f.url+'</td>'+
              '<td>'+(f.enabled?'<span class="status on">ON</span>':'<span class="status off">OFF</span>')+'</td>'+
              '<td><button onclick="toggleFeed(\\''+cat+'\\',\\''+f.url+'\\','+!f.enabled+')">'+(f.enabled?'⏸':'▶')+'</button> '+
              '<button class="danger" onclick="deleteFeed(\\''+cat+'\\',\\''+f.url+'\\')">🗑</button> '+
              '<button onclick="toggleCategory(\\''+cat+'\\','+!f.enabled+')">вся категория</button></td>';
            tb.appendChild(tr);
          });
        });
      });
    }
    function loadNotes() {
      fetch('/api/notes').then(function(r){return r.json();}).then(function(d){
        var c = document.getElementById('notes-list');
        c.innerHTML = '';
        d.notes.forEach(function(n){
          var div = document.createElement('div'); div.className='note-item';
          div.innerHTML = '<div class="actions"><button onclick="event.stopPropagation();editNote(\\''+n.name+'\\')">✏️</button>'+
            '<button class="danger" onclick="event.stopPropagation();deleteNote(\\''+n.name+'\\')">🗑</button></div>'+
            '<div class="title">'+n.title+'</div><div class="meta">'+n.date+' · '+n.size+' байт · '+n.name+'</div>';
          div.onclick = function(){ location.href='/note/'+encodeURIComponent(n.name); };
          c.appendChild(div);
        });
      });
    }
    function addFeed() {
      var url = document.getElementById('new-feed-url').value.trim();
      var name = document.getElementById('new-feed-name').value.trim();
      var cat = document.getElementById('new-feed-cat').value.trim();
      if (!url) { showToast('Введите URL', true); return; }
      fetch('/api/feeds', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({url:url, name:name, category:cat})})
        .then(function(r){return r.json();}).then(function(d){
          if (d.ok) { showToast('Лента добавлена'); loadFeeds(); }
          else { showToast(d.error||'Ошибка', true); }
        });
    }
    function deleteFeed(cat, url) {
      if (!confirm('Удалить ленту?')) return;
      fetch('/api/feeds', {method:'DELETE', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({category:cat, url:url})})
        .then(function(r){return r.json();}).then(function(d){
          if (d.ok) { showToast('Удалено'); loadFeeds(); loadSourcesTable(); }
        });
    }
    function toggleFeed(cat, url, enable) {
      fetch('/api/feeds/toggle', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({category:cat, url:url, enable:enable})})
        .then(function(r){return r.json();}).then(function(d){
          if (d.ok) { loadFeeds(); loadSourcesTable(); }
        });
    }
    function toggleCategory(cat, enable) {
      fetch('/api/feeds/toggle-category', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({category:cat, enable:enable})})
        .then(function(r){return r.json();}).then(function(d){
          if (d.ok) { loadFeeds(); loadSourcesTable(); }
        });
    }
    function previewFeed(url, btn) {
      var box = btn.closest('.feed-card').querySelector('.preview-box');
      if (!box) { box = document.createElement('div'); box.className='preview-box'; btn.closest('.feed-card').appendChild(box); }
      box.classList.toggle('show');
      if (!box.classList.contains('show')) return;
      box.textContent = 'Загрузка…';
      fetch('/api/preview?url=' + encodeURIComponent(url)).then(function(r){return r.json();})
        .then(function(d){ box.textContent = d.items ? d.items.map(function(i){return i.title + ' — ' + i.date;}).join('\\n') : (d.error||'Пусто'); });
    }
    function refreshFeeds() {
      showToast('Обновляем…');
      fetch('/api/refresh', {method:'POST'}).then(function(r){return r.json();}).then(function(d){
        showToast(d.ok?'Обновлено':'Ошибка', !d.ok);
        loadStats();
      });
    }
    function loadBriefing() {
      var box = document.getElementById('briefing');
      box.classList.add('show'); box.textContent = 'Готовим сводку…';
      fetch('/api/news-briefing').then(function(r){return r.json();}).then(function(d){
        box.textContent = d.text || d.error || 'Пусто';
      });
    }
    function loadMorning() {
      var box = document.getElementById('briefing');
      box.classList.add('show'); box.textContent = 'Готовим утренний дайджест…';
      fetch('/api/morning-digest').then(function(r){return r.json();}).then(function(d){
        box.textContent = d.text || d.error || 'Пусто';
      });
    }
    function deleteNote(name) {
      if (!confirm('Удалить заметку ' + name + '?')) return;
      fetch('/api/notes', {method:'DELETE', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({path:name})}).then(function(r){return r.json();}).then(function(d){
          if (d.ok) { showToast('Заметка удалена'); loadNotes(); }
        });
    }
    function editNote(name) {
      fetch('/api/notes/content?path=' + encodeURIComponent(name)).then(function(r){return r.json();})
        .then(function(d){
          document.getElementById('modal-title').textContent = 'Редактировать: ' + name;
          document.getElementById('modal-note-title').value = '';
          document.getElementById('modal-note-text').value = d.content || '';
          document.getElementById('modal-save-btn').onclick = function(){
            fetch('/api/notes', {method:'PUT', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({path:name, content: document.getElementById('modal-note-text').value})})
              .then(function(r){return r.json();}).then(function(r2){
                if (r2.ok) { showToast('Сохранено'); closeModal(); loadNotes(); }
                else { showToast('Ошибка', true); }
              });
          };
          openModal();
        });
    }
    function changeNotesFolder() {
      var p = prompt('Путь к папке заметок:');
      if (p) {
        fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({notes_dir: p})}).then(function(r){return r.json();}).then(function(d){
            if (d.ok) { showToast('Папка изменена'); loadNotes(); loadStats(); }
          });
      }
    }
    function searchNotes() {
      var q = document.getElementById('note-search').value.trim();
      if (!q) { loadNotes(); return; }
      fetch('/api/notes/search', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({query:q})}).then(function(r){return r.json();})
        .then(function(d){
          var c = document.getElementById('notes-list'); c.innerHTML='';
          if (!d.results || !d.results.length) { c.innerHTML='<p style="color:var(--muted)">Ничего не найдено</p>'; return; }
          d.results.forEach(function(r){
            var div = document.createElement('div'); div.className='note-item';
            div.innerHTML = '<div class="actions"><button class="danger" onclick="event.stopPropagation();deleteNote(\\''+r.name+'\\')">🗑</button></div>'+
              '<div class="title">'+r.name+'</div><div class="meta">Релевантность: '+r.score+'</div>'+
              '<div style="margin-top:6px;color:var(--muted);font-size:13px">'+r.snippet+'</div>';
            div.onclick = function(){ location.href='/note/'+encodeURIComponent(r.name); };
            c.appendChild(div);
          });
        });
    }
    setInterval(loadStats, 30000);
    loadStats(); loadFeeds();
  </script>
""")

    SETTINGS_TPL = PAGE_TPL.replace("{% block content %}{% endblock %}", """
  <div class="container">
    <h2>Настройки</h2>
    <div class="row"><label>Тема</label>
      <select id="set-theme"><option value="dark">Тёмная</option><option value="light">Светлая</option></select>
    </div>
    <div class="row"><label>Путь к папке заметок</label>
      <input id="set-notes-dir" placeholder="/path/to/notes">
    </div>
    <div class="row"><label>Новостей в сводке (3-15)</label>
      <input type="number" id="set-news-count" min="3" max="15">
    </div>
    <div class="row"><label>Глубина ленты (5-20)</label>
      <input type="number" id="set-news-depth" min="5" max="20">
    </div>
    <div class="row"><label>TTL кэша (сек)</label>
      <input type="number" id="set-cache-ttl" min="60">
    </div>
    <div class="row"><label>Автообновление</label>
      <select id="set-auto-refresh"><option value="true">Включено</option><option value="false">Выключено</option></select>
    </div>
    <div class="row"><label>Интервал обновления (сек)</label>
      <input type="number" id="set-refresh-interval" min="60">
    </div>
    <h3 style="margin-top:24px">Лимиты длины текста</h3>
    <div class="row"><label>Загрузка лент, summary_max (100-10000)</label>
      <input type="number" id="set-summary-max" min="100" max="10000" step="50">
    </div>
    <div class="row"><label>Списки и сводки, digest_summary_max (50-2000)</label>
      <input type="number" id="set-digest-summary-max" min="50" max="2000" step="50">
    </div>
    <div class="row"><label>Детали и случайная, details_summary_max (100-10000)</label>
      <input type="number" id="set-details-summary-max" min="100" max="10000" step="50">
    </div>
    <div class="row"><label>Полная статья, full_article_max (500-20000)</label>
      <input type="number" id="set-full-article-max" min="500" max="20000" step="500">
    </div>
    <div class="row"><label>Использовать content:encoded (полный текст из ленты)</label>
      <select id="set-use-full-content"><option value="true">Да</option><option value="false">Нет</option></select>
    </div>
    <h3 style="margin-top:24px">Подключение к XiaoZhi Cloud</h3>
    <div class="row"><label>MCP endpoint (wss://...)</label>
      <input id="set-xiaozhi-endpoint" placeholder="wss://api.xiaozhi.me/mcp/?token=..." style="width:100%">
    </div>
    <div class="hint">Адрес из настроек кубика (XiaoZhi Cloud → MCP endpoint). Кнопка «Применить» горячо меняет endpoint без перезапуска:
      пишется в <code>xiaozhi_mcp_adapter/mcp_config.json</code> и перезагружается мост. При обновлении/перезапуске endpoint восстанавливается автоматически.</div>
    <div id="endpoint-status" class="hint" style="min-height:18px"></div>
    <div class="btn-group">
      <button onclick="applyEndpoint()">🔌 Применить эндпоинт</button>
      <button class="primary" onclick="saveSettings()">💾 Сохранить</button>
      <button onclick="resetCache()">🔄 Сбросить кэш RSS</button>
      <button onclick="restoreFeeds()">♻️ Восстановить предустановленные ленты</button>
      <button class="danger" onclick="resetAll()">⚠️ Сбросить все настройки</button>
    </div>
    <p style="margin-top:20px"><a href="/">← Назад</a></p>
  </div>
""").replace("{% block script %}{% endblock %}", """
  <script>
    function loadSettings() {
      fetch('/api/settings').then(function(r){return r.json();}).then(function(d){
        document.getElementById('set-theme').value = d.theme || 'dark';
        document.getElementById('set-notes-dir').value = d.notes_dir || '';
        document.getElementById('set-news-count').value = d.news_count || 7;
        document.getElementById('set-news-depth').value = d.news_depth || 10;
        document.getElementById('set-cache-ttl').value = d.cache_ttl || 600;
        document.getElementById('set-auto-refresh').value = String(d.auto_refresh);
        document.getElementById('set-refresh-interval').value = d.refresh_interval || 600;
        document.getElementById('set-summary-max').value = d.summary_max || 800;
        document.getElementById('set-digest-summary-max').value = d.digest_summary_max || 300;
        document.getElementById('set-details-summary-max').value = d.details_summary_max || 800;
        document.getElementById('set-full-article-max').value = d.full_article_max || 4000;
        document.getElementById('set-use-full-content').value = String(d.use_full_content !== false);
        document.getElementById('set-xiaozhi-endpoint').value = d.xiaozhi_endpoint || '';
      });
    }
    function saveSettings() {
      var data = {
        theme: document.getElementById('set-theme').value,
        notes_dir: document.getElementById('set-notes-dir').value,
        news_count: parseInt(document.getElementById('set-news-count').value),
        news_depth: parseInt(document.getElementById('set-news-depth').value),
        cache_ttl: parseInt(document.getElementById('set-cache-ttl').value),
        auto_refresh: document.getElementById('set-auto-refresh').value === 'true',
        refresh_interval: parseInt(document.getElementById('set-refresh-interval').value),
        summary_max: parseInt(document.getElementById('set-summary-max').value),
        digest_summary_max: parseInt(document.getElementById('set-digest-summary-max').value),
        details_summary_max: parseInt(document.getElementById('set-details-summary-max').value),
        full_article_max: parseInt(document.getElementById('set-full-article-max').value),
        use_full_content: document.getElementById('set-use-full-content').value === 'true',
        xiaozhi_endpoint: document.getElementById('set-xiaozhi-endpoint').value.trim(),
      };
      fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(data)}).then(function(r){return r.json();}).then(function(d){
          showToast(d.ok?'Сохранено. Эндпоинт прописан в адаптер, сбросьте кэш RSS при смене лимитов':'Ошибка', !d.ok);
        });
    }
    function applyEndpoint() {
      var ep = document.getElementById('set-xiaozhi-endpoint').value.trim();
      var st = document.getElementById('endpoint-status');
      st.textContent = 'Применяю…';
      fetch('/api/endpoint/apply', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({xiaozhi_endpoint: ep})}).then(function(r){return r.json();}).then(function(d){
          if (!d.ok) { st.textContent = '❌ ' + (d.error||'Ошибка'); showToast(d.error||'Ошибка', true); return; }
          var s = d.adapter || 'адаптер не отвечает';
          st.textContent = '✅ Эндпоинт применён. Мост: ' + s;
          showToast('Эндпоинт применён (' + s + ')');
        }).catch(function(){ st.textContent = '❌ Ошибка запроса'; });
    }
    function resetCache() {
      if (!confirm('Сбросить кэш RSS?')) return;
      fetch('/api/refresh', {method:'POST'}).then(function(r){return r.json();}).then(function(d){
        showToast(d.ok?'Кэш сброшен':'Ошибка', !d.ok);
      });
    }
    function restoreFeeds() {
      if (!confirm('Восстановить предустановленные ленты? Текущие будут заменены.')) return;
      fetch('/api/settings/restore-feeds', {method:'POST'}).then(function(r){return r.json();})
        .then(function(d){ showToast(d.ok?'Восстановлено':'Ошибка', !d.ok); });
    }
    function resetAll() {
      if (!confirm('Сбросить ВСЕ настройки?')) return;
      fetch('/api/settings/reset', {method:'POST'}).then(function(r){return r.json();})
        .then(function(d){ if (d.ok) { showToast('Сброшено'); loadSettings(); } });
    }
    loadSettings();
  </script>
""")

    COMMANDS_TPL = PAGE_TPL.replace("{% block content %}{% endblock %}", """
  <div class="container">
    <a href="/" class="back-link">← На главную</a>
    <h2>🎙 Голосовые команды</h2>
    <p class="hint">Говорите кубику XiaoZhi как-то из фраз ниже — он сам поймёт и вызовет нужный инструмент. Формулировки могут быть разными, это лишь примеры.</p>

    <h3 style="margin-top:24px">📰 Новости</h3>
    <div class="cmd-list">
      <div class="cmd"><div class="cmd-phrase">«Новости» / «Что нового?»</div><div class="cmd-desc">Главная сводка: 7 свежих новостей из разных источников. Каждая лента отдаёт до 10 последних, выбираются рандомные разноплановые. → <code>NEWS_BRIEFING</code></div></div>
      <div class="cmd"><div class="cmd-phrase">«Утренний дайджест» / «С добрым утром, что нового?»</div><div class="cmd-desc">По одной свежей новости из каждой активной категории. → <code>morning_digest</code></div></div>
      <div class="cmd"><div class="cmd-phrase">«Случайная новость»</div><div class="cmd-desc">Одна случайная новость из всех лент. → <code>get_random_news</code></div></div>
      <div class="cmd"><div class="cmd-phrase">«Новости про <i>технологии</i>» / «Найди новость про <i>спорт</i>»</div><div class="cmd-desc">Новости по теме/категории. → <code>get_latest_news</code> / <code>search_news</code></div></div>
      <div class="cmd"><div class="cmd-phrase">«Найди новост<i>у</i> про <i>Илон Маск</i>»</div><div class="cmd-desc">Поиск по заголовкам и описаниям. → <code>search_news(query)</code></div></div>
      <div class="cmd"><div class="cmd-phrase">«Расскажи подробнее про новость <i>номер 3</i>» / «Что там по <i>третьей</i>?»</div><div class="cmd-desc">Полный текст и ссылка на новость из последней сводки. → <code>get_news_details(index)</code></div></div>
      <div class="cmd"><div class="cmd-phrase">«Прочитай статью полностью»</div><div class="cmd-desc">Читает полную статью по ссылке из деталей новости. → <code>read_article(link)</code></div></div>
      <div class="cmd"><div class="cmd-phrase">«Какие есть категории?» / «Из чего ты собираешь новости?»</div><div class="cmd-desc">Список категорий и активных источников. → <code>list_categories</code></div></div>
    </div>

    <h3 style="margin-top:24px">📝 Заметки</h3>
    <div class="cmd-list">
      <div class="cmd"><div class="cmd-phrase">«Запиши: <i>купить молоко</i>»</div><div class="cmd-desc">Создать заметку голосом. → <code>add_note(title, content)</code></div></div>
      <div class="cmd"><div class="cmd-phrase">«Создай заметку <i>идей</i>: <i>сделать приложение…</i>»</div><div class="cmd-desc">Заметка с явным заголовком. → <code>add_note</code></div></div>
      <div class="cmd"><div class="cmd-phrase">«Найди заметку про <i>идеи</i>»</div><div class="cmd-desc">Поиск (нечёткий, по содержимому). → <code>search_notes</code> / <code>fuzzy_search_notes</code></div></div>
      <div class="cmd"><div class="cmd-phrase">«Прочитай заметку <i>идеи</i>»</div><div class="cmd-desc">Читает заметку целиком по имени. → <code>read_note_by_name</code></div></div>
      <div class="cmd"><div class="cmd-phrase">«Покажи все заметки» / «Список заметок»</div><div class="cmd-desc">Все заметки с датой. → <code>list_notes</code></div></div>
      <div class="cmd"><div class="cmd-phrase">«Удали заметку <i>идеи</i>»</div><div class="cmd-desc">Удаляет заметку по имени. → <code>delete_note</code></div></div>
    </div>

    <p class="hint" style="margin-top:24px">Колобок запоминает последние выданные новости — поэтому «подробнее про №3» работает после сводки. Ссылки и статус моста: <a href="/">главная</a>, настройки лент и лимитов: <a href="/settings">⚙️ настройки</a>.</p>
    <p style="margin-top:20px"><a href="/">← Назад</a></p>
  </div>
""")

    NOTE_VIEW_TPL = """
<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"><title>{{ title }} — XiaoZhi</title>
<style>""" + BASE_CSS + """</style></head>
<body class="{{ theme }}">
  <div class="container">
    <a href="/" class="back-link">← Назад к списку</a>
    <div class="note-view">{{ content_html }}</div>
  </div>
</body>
</html>
"""

    # ------------------------------------------------------------------
    # Роуты: страницы
    # ------------------------------------------------------------------
    @app.route("/")
    def index():
        theme = "light" if CONFIG.get("theme") == "light" else ""
        mcp_port = CONFIG.get("mcp_port", 8000)
        return render_template_string(
            INDEX_TPL,
            title="XiaoZhi News Manager",
            header_title="📰 XiaoZhi News Manager",
            theme=theme,
            theme_btn="☀️" if CONFIG.get("theme") == "light" else "🌙",
            mcp_url=f"http://localhost:{mcp_port}/sse",
            mcp_port=mcp_port,
        )

    @app.route("/settings")
    def settings_page():
        theme = "light" if CONFIG.get("theme") == "light" else ""
        return render_template_string(
            SETTINGS_TPL,
            title="Настройки — XiaoZhi",
            header_title="🔧 Настройки",
            theme=theme,
            theme_btn="☀️" if CONFIG.get("theme") == "light" else "🌙",
        )

    @app.route("/commands")
    def commands_page():
        theme = "light" if CONFIG.get("theme") == "light" else ""
        return render_template_string(
            COMMANDS_TPL,
            title="Голосовые команды — XiaoZhi",
            header_title="🎙 Голосовые команды",
            theme=theme,
            theme_btn="☀️" if CONFIG.get("theme") == "light" else "🌙",
        )

    @app.route("/note/<path:name>")
    def view_note(name):
        content = NOTES.read_note(name)
        if content is None:
            return "Заметка не найдена", 404
        # минимальная подсветка
        import html as _html
        safe = _html.escape(content)
        safe = safe.replace("\n", "<br>")
        title = name
        for line in content.splitlines():
            if line.strip().startswith("# "):
                title = line.strip()[2:]
                break
        theme = "light" if CONFIG.get("theme") == "light" else ""
        return render_template_string(
            NOTE_VIEW_TPL,
            title=title,
            theme=theme,
            content_html=safe,
        )

    # ------------------------------------------------------------------
    # API: новости/ленты
    # ------------------------------------------------------------------
    @app.route("/api/feeds")
    def api_feeds():
        return jsonify({"feeds": FEEDS.all()})

    @app.route("/api/feeds", methods=["POST"])
    def api_add_feed():
        data = request.get_json(force=True, silent=True) or {}
        url = data.get("url", "").strip()
        name = data.get("name", "").strip()
        cat = data.get("category", "").strip() or "разное"
        if not url:
            return jsonify({"ok": False, "error": "URL обязателен"}), 400
        ok = FEEDS.add_feed(url, name, cat)
        return jsonify({"ok": ok, "error": None if ok else "Лента уже есть в категории"})

    @app.route("/api/feeds", methods=["DELETE"])
    def api_delete_feed():
        data = request.get_json(force=True, silent=True) or {}
        FEEDS.delete_feed(data.get("category", ""), data.get("url", ""))
        return jsonify({"ok": True})

    @app.route("/api/feeds/category/<name>", methods=["DELETE"])
    def api_delete_cat(name):
        FEEDS.delete_category(name)
        return jsonify({"ok": True})

    @app.route("/api/feeds/toggle", methods=["POST"])
    def api_toggle_feed():
        data = request.get_json(force=True, silent=True) or {}
        FEEDS.toggle_feed(data.get("category", ""), data.get("url", ""), data.get("enable", True))
        return jsonify({"ok": True})

    @app.route("/api/feeds/toggle-category", methods=["POST"])
    def api_toggle_cat():
        data = request.get_json(force=True, silent=True) or {}
        FEEDS.toggle_category(data.get("category", ""), data.get("enable", True))
        return jsonify({"ok": True})

    @app.route("/api/test", methods=["POST"])
    def api_test_feed():
        data = request.get_json(force=True, silent=True) or {}
        url = data.get("url", "").strip()
        if not url:
            return jsonify({"ok": False, "error": "URL обязателен"})
        res = parse_feed(url, "test", depth=5)
        if isinstance(res, dict) and "error" in res:
            return jsonify({"ok": False, "error": res["error"]})
        return jsonify({"ok": True, "items": res})

    @app.route("/api/preview")
    def api_preview():
        url = request.args.get("url", "")
        if not url:
            return jsonify({"error": "URL обязателен"}), 400
        res = parse_feed(url, "preview", depth=5)
        if isinstance(res, dict) and "error" in res:
            return jsonify({"error": res["error"]})
        return jsonify({"items": res})

    @app.route("/api/stats")
    def api_stats():
        return jsonify(compute_stats())

    @app.route("/api/refresh", methods=["POST"])
    def api_refresh():
        threading.Thread(target=_force_refresh_all, daemon=True).start()
        return jsonify({"ok": True})

    @app.route("/api/news-briefing")
    def api_briefing():
        return jsonify({"text": build_news_digest(count=CONFIG.get("news_count", 7))})

    @app.route("/api/morning-digest")
    def api_morning():
        return jsonify({"text": build_morning_digest()})

    # ------------------------------------------------------------------
    # API: заметки
    # ------------------------------------------------------------------
    @app.route("/api/notes")
    def api_notes():
        return jsonify({"notes": NOTES.list_notes()})

    @app.route("/api/notes/content")
    def api_note_content():
        path = request.args.get("path", "")
        content = NOTES.read_note(path)
        if content is None:
            return jsonify({"error": "не найдена"}), 404
        return jsonify({"content": content})

    @app.route("/api/notes", methods=["POST"])
    def api_add_note():
        data = request.get_json(force=True, silent=True) or {}
        name = NOTES.add_note(data.get("title", ""), data.get("content", ""), source="веб")
        return jsonify({"ok": bool(name), "name": name})

    @app.route("/api/notes", methods=["PUT"])
    def api_update_note():
        data = request.get_json(force=True, silent=True) or {}
        ok = NOTES.update_note(data.get("path", ""), data.get("content", ""))
        return jsonify({"ok": ok})

    @app.route("/api/notes", methods=["DELETE"])
    def api_delete_note():
        data = request.get_json(force=True, silent=True) or {}
        ok = NOTES.delete_note(data.get("path", ""))
        return jsonify({"ok": ok})

    @app.route("/api/notes/search", methods=["POST"])
    def api_search_notes():
        data = request.get_json(force=True, silent=True) or {}
        return jsonify({"results": NOTES.search(data.get("query", ""))})

    # ------------------------------------------------------------------
    # API: настройки
    # ------------------------------------------------------------------
    @app.route("/api/settings")
    def api_settings():
        return jsonify(CONFIG.all())

    @app.route("/api/settings", methods=["POST"])
    def api_set_settings():
        data = request.get_json(force=True, silent=True) or {}
        CONFIG.update(data)
        if "xiaozhi_endpoint" in data:
            _sync_adapter_endpoint(data.get("xiaozhi_endpoint", ""))
        return jsonify({"ok": True})

    @app.route("/api/endpoint/apply", methods=["POST"])
    def api_apply_endpoint():
        """Горячая замена MCP endpoint: сохраняет, прописывает в адаптер,
        перезагружает мост и возвращает статус подключения."""
        data = request.get_json(force=True, silent=True) or {}
        ep = (data.get("xiaozhi_endpoint") or CONFIG.get("xiaozhi_endpoint", "") or "").strip()
        if not ep.startswith(("wss://", "ws://")):
            return jsonify({"ok": False, "error": "Некорректный endpoint (нужен wss://)"}), 400
        CONFIG.set("xiaozhi_endpoint", ep)
        synced = _sync_adapter_endpoint(ep)
        binding = None
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:8765/api/status", timeout=5) as r:
                st = json.load(r)
            for b in st.get("bindings", []):
                binding = b.get("status")
        except Exception:
            binding = None
        return jsonify({"ok": True, "synced": synced, "adapter": binding})

    @app.route("/api/settings/theme", methods=["POST"])
    def api_set_theme():
        cur = CONFIG.get("theme", "dark")
        new = "light" if cur == "dark" else "dark"
        CONFIG.set("theme", new)
        return jsonify({"ok": True, "theme": new})

    @app.route("/api/settings/reset", methods=["POST"])
    def api_reset_settings():
        CONFIG.reset()
        return jsonify({"ok": True})

    @app.route("/api/settings/restore-feeds", methods=["POST"])
    def api_restore_feeds():
        FEEDS.restore_defaults()
        return jsonify({"ok": True})

    return app


# ---------------------------------------------------------------------------
# Утренний дайджест
# ---------------------------------------------------------------------------
def build_morning_digest():
    """По одной свежей новости из каждой активной категории (до 12)."""
    cats = FEEDS.categories()
    out = []
    for cat in cats:
        items = collect_news(cat)
        if items:
            # берём первую
            out.append(items[0])
    if not out:
        return "Свежих новостей нет."
    lines = []
    digest_max = CONFIG.get("digest_summary_max", 300)
    for i, it in enumerate(out[:12], 1):
        title = it.get("title", "")
        summary = it.get("summary", "")
        if len(summary) > digest_max:
            summary = summary[:digest_max].rstrip() + "…"
        source = it.get("source", "")
        cat = it.get("category", "")
        lines.append(f"{i}. [{cat}] {title} — {summary} — {source}")
    lines.append(f"\nВсего {len(out[:12])} новостей из {len(cats)} категорий")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Чтение полной статьи
# ---------------------------------------------------------------------------
def read_full_article(link, max_chars=None):
    """Скачивает HTML, извлекает текст, обрезает до max_chars (или full_article_max из конфига)."""
    if max_chars is None:
        max_chars = CONFIG.get("full_article_max", 4000)
    try:
        req = urllib.request.Request(link, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        # пробуем utf-8
        try:
            html_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            html_text = raw.decode("cp1251", errors="ignore")
        # убираем скрипты/стили
        html_text = re.sub(r"<script[^>]*>.*?</script>", "", html_text, flags=re.S | re.I)
        html_text = re.sub(r"<style[^>]*>.*?</style>", "", html_text, flags=re.S | re.I)
        # вырезаем теги
        text = re.sub(r"<[^>]+>", " ", html_text)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"
        return text
    except Exception as e:
        return f"Не удалось прочитать статью: {e}"


# ---------------------------------------------------------------------------
# MCP-сервер
# ---------------------------------------------------------------------------
def create_mcp_server():
    """Создаёт FastMCP-сервер со всеми инструментами."""
    if not HAS_MCP:
        logging.error("mcp не установлен — MCP-сервер недоступен")
        return None

    mcp = FastMCP("xiaozhi-news")

    # --------------------------------------------------------------
    # НОВОСТИ
    # --------------------------------------------------------------
    @mcp.tool()
    def get_random_news(category: str = "") -> str:
        """Получить случайную новость. category: категория (опционально).
        Возвращает заголовок, описание и источник."""
        try:
            items = collect_news(category)
            if not items:
                return "Новостей нет. Попробуйте обновить ленты."
            import random
            it = random.choice(items)
            title = it.get("title", "")
            summary = it.get("summary", "")
            max_len = CONFIG.get("details_summary_max", 800)
            if len(summary) > max_len:
                summary = summary[:max_len].rstrip() + "…"
            source = it.get("source", "")
            return f"{title}\n\n{summary}\n\nИсточник: {source}"
        except Exception as e:
            return f"Ошибка: {e}"

    @mcp.tool()
    def get_latest_news(category: str = "", limit: int = 5) -> str:
        """Последние заголовки новостей по номерам.
        category: категория (опционально), limit: сколько заголовков."""
        try:
            items = collect_news(category)
            if not items:
                return "Новостей нет."
            items = items[:limit]
            with NEWS_MEMORY_LOCK:
                NEWS_MEMORY.clear()
                for it in items:
                    NEWS_MEMORY.append(dict(it))
            lines = []
            for i, it in enumerate(items, 1):
                lines.append(f"{i}. {it.get('title','')} — {it.get('source','')}")
            return "\n".join(lines)
        except Exception as e:
            return f"Ошибка: {e}"

    @mcp.tool()
    def NEWS_BRIEFING() -> str:
        """Сводка из 7 новостей из разных источников.
        Каждая лента отдаёт до 10 последних, выбираются 7 разноплановых.
        ГЛАВНЫЙ ИНСТРУМЕНТ для команды «новости»."""
        try:
            return build_news_digest(count=CONFIG.get("news_count", 7))
        except Exception as e:
            return f"Ошибка: {e}"

    @mcp.tool()
    def search_news(query: str) -> str:
        """Поиск новостей по заголовкам и описаниям. До 5 результатов."""
        try:
            q = (query or "").lower().strip()
            if not q:
                return "Пустой запрос."
            items = collect_news()
            results = []
            for it in items:
                title = it.get("title", "").lower()
                summary = it.get("summary", "").lower()
                if q in title or q in summary:
                    results.append(it)
                if len(results) >= 5:
                    break
            if not results:
                return f"По запросу «{query}» ничего не найдено."
            lines = []
            digest_max = CONFIG.get("digest_summary_max", 300)
            for i, it in enumerate(results, 1):
                title = it.get("title", "")
                summary = it.get("summary", "")
                if len(summary) > digest_max:
                    summary = summary[:digest_max].rstrip() + "…"
                source = it.get("source", "")
                lines.append(f"{i}. {title} — {summary} — {source}")
            return "\n".join(lines)
        except Exception as e:
            return f"Ошибка: {e}"

    @mcp.tool()
    def morning_digest() -> str:
        """Утренний дайджест: по одной свежей новости из каждой активной категории (до 12)."""
        try:
            return build_morning_digest()
        except Exception as e:
            return f"Ошибка: {e}"

    @mcp.tool()
    def get_news_details(index: int, category: str = "") -> str:
        """Подробности по номеру из последней сводки.
        index: номер новости (1-based). Возвращает полный текст, источник, категорию, ссылку."""
        try:
            item, err = NEWS_MEM.get_by_index(int(index))
            if err:
                return err
            title = item.get("title", "")
            summary = item.get("summary", "")
            source = item.get("source", "")
            cat = item.get("category", "")
            link = item.get("link", "")
            date = item.get("date", "")
            max_len = CONFIG.get("details_summary_max", 800)
            if len(summary) > max_len:
                summary = summary[:max_len].rstrip() + "…"
            # fallback: если описания мало — докачиваем полную статью
            if len(summary) < 200 and link:
                try:
                    full = read_full_article(link)
                    if full and not full.startswith("Не удалось прочитать статью"):
                        summary = full
                except Exception:
                    pass
            return (f"{title}\n\n{summary}\n\n"
                    f"Источник: {source}\n"
                    f"Категория: {cat}\n"
                    f"Дата: {date}\n"
                    f"Ссылка: {link}")
        except Exception as e:
            return f"Ошибка: {e}"

    @mcp.tool()
    def list_categories() -> str:
        """Список категорий + активные источники."""
        try:
            feeds = FEEDS.all()
            lines = []
            for cat in sorted(feeds.keys()):
                active = [f["name"] for f in feeds[cat] if f.get("enabled", True)]
                lines.append(f"• {cat} ({len(active)} активных): {', '.join(active)}")
            return "\n".join(lines)
        except Exception as e:
            return f"Ошибка: {e}"

    @mcp.tool()
    def read_article(link: str) -> str:
        """Прочитать полную статью по URL. Скачивает HTML, извлекает текст,
        обрезает до лимита full_article_max (по умолчанию 4000 символов)."""
        try:
            return read_full_article(link)
        except Exception as e:
            return f"Ошибка: {e}"

    # --------------------------------------------------------------
    # ЗАМЕТКИ
    # --------------------------------------------------------------
    @mcp.tool()
    def search_notes(query: str) -> str:
        """Поиск по всем .md файлам заметок.
        Релевантность: совпадение в имени +50, в заголовке +40,
        каждое слово в тексте +5 (макс +20), полная фраза +30.
        До 10 результатов с фрагментом до 300 символов."""
        try:
            results = NOTES.search(query)
            if not results:
                return f"По запросу «{query}» заметок не найдено."
            lines = []
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. {r['name']} (релевантность {r['score']})")
                lines.append(f"   {r['snippet']}")
            return "\n".join(lines)
        except Exception as e:
            return f"Ошибка: {e}"

    @mcp.tool()
    def read_note_by_name(filename: str) -> str:
        """Прочитать конкретную заметку целиком. Допускает частичное совпадение имени."""
        try:
            # сначала точное/частичное по имени
            name = NOTES.find_by_name(filename)
            if name:
                content = NOTES.read_note(name)
                if content:
                    return content
            # fallback: может передали полный путь
            content = NOTES.read_note(filename)
            if content:
                return content
            return f"Заметка «{filename}» не найдена."
        except Exception as e:
            return f"Ошибка: {e}"

    @mcp.tool()
    def list_notes() -> str:
        """Все заметки с датой и размером."""
        try:
            notes = NOTES.list_notes()
            if not notes:
                return "Заметок пока нет."
            lines = []
            for n in notes:
                lines.append(f"• {n['title']} ({n['date']}, {n['size']} байт) — файл: {n['name']}")
            return "\n".join(lines)
        except Exception as e:
            return f"Ошибка: {e}"

    @mcp.tool()
    def add_note(title: str, content: str) -> str:
        """Создать заметку. Автогенерация безопасного имени из заголовка.
        AI может голосом: «запиши что…»."""
        try:
            name = NOTES.add_note(title, content, source="голос")
            if name:
                return f"Заметка сохранена: {name}"
            return "Не удалось сохранить заметку."
        except Exception as e:
            return f"Ошибка: {e}"

    @mcp.tool()
    def delete_note(filename: str) -> str:
        """Удалить заметку. Допускает частичное совпадение имени."""
        try:
            name = NOTES.find_by_name(filename) or filename
            ok = NOTES.delete_note(name)
            if ok:
                return f"Заметка удалена: {name}"
            return f"Заметка «{filename}» не найдена."
        except Exception as e:
            return f"Ошибка: {e}"

    @mcp.tool()
    def fuzzy_search_notes(query: str, threshold: int = 60) -> str:
        """Нечёткий поиск заметок (rapidfuzz). threshold: порог совпадения 0-100.
        Если rapidfuzz не установлен — fallback на обычный поиск."""
        try:
            results = NOTES.fuzzy_search(query, threshold)
            if not results:
                return f"Ничего не найдено по запросу «{query}» с порогом {threshold}."
            lines = []
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. {r['name']} (сходство {r['score']}%)")
                lines.append(f"   {r['snippet']}")
            return "\n".join(lines)
        except Exception as e:
            return f"Ошибка: {e}"

    return mcp


def run_mcp_server():
    """Запускает MCP-сервер в daemon-потоке."""
    if not HAS_MCP:
        logging.error("MCP недоступен — mcp не установлен")
        return
    try:
        mcp = create_mcp_server()
        port = CONFIG.get("mcp_port", 8000)
        logging.info("MCP-сервер запускается на порту %d (sse)", port)
        # mcp>=1.9: host/port задаются через settings, не через run()
        try:
            mcp.settings.host = "0.0.0.0"
            mcp.settings.port = port
        except Exception:
            pass
        # FastMCP run (sse transport) — блокирующий вызов
        mcp.run(transport="sse")
    except Exception as e:
        logging.error("Ошибка MCP-сервера: %s\n%s", e, traceback.format_exc())


# ---------------------------------------------------------------------------
# Flask-запуск
# ---------------------------------------------------------------------------
def run_flask():
    if not HAS_FLASK:
        logging.error("Flask недоступен")
        return
    try:
        app = create_flask_app()
        port = CONFIG.get("web_port", 5000)
        logging.info("Flask запускается на порту %d", port)
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    except Exception as e:
        logging.error("Ошибка Flask: %s\n%s", e, traceback.format_exc())


# ---------------------------------------------------------------------------
# Фоновое автообновление RSS
# ---------------------------------------------------------------------------
def auto_refresh_loop():
    """Периодически принудительно парсит активные ленты."""
    while not _tray_stop["stop"]:
        try:
            interval = CONFIG.get("refresh_interval", 600)
            for _ in range(interval):
                if _tray_stop["stop"]:
                    return
                time.sleep(1)
            if _tray_pause["paused"]:
                continue
            if not CONFIG.get("auto_refresh", True):
                continue
            logging.info("Автообновление RSS…")
            for cat, feed in FEEDS.active_feeds():
                if _tray_stop["stop"]:
                    return
                try:
                    get_feed_cached(cat, feed, force=True)
                except Exception:
                    continue
            logging.info("Автообновление завершено")
        except Exception as e:
            logging.error("Ошибка автообновления: %s", e)
            time.sleep(60)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
def parse_args(argv):
    """Разбор флагов командной строки."""
    no_tray = False
    port_override = None
    for arg in argv[1:]:
        if arg in ("--no-tray", "--headless"):
            no_tray = True
        elif arg == "--port":
            pass  # значение берём из следующего аргумента
    # --port N
    for i, arg in enumerate(argv[1:], 1):
        if arg == "--port" and i + 1 < len(argv):
            try:
                port_override = int(argv[i + 1])
            except ValueError:
                pass
    if os.environ.get("DOCKER") == "true":
        no_tray = True
    return no_tray, port_override


def main():
    setup_logging()
    no_tray, port_override = parse_args(sys.argv)

    if port_override:
        CONFIG.set("web_port", port_override)

    # создаём папки
    try:
        STATIC_DIR.mkdir(parents=True, exist_ok=True)
        notes_dir_path()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logging.error("Не удалось создать папки: %s", e)

    # иконка
    if not ICON_PATH.exists():
        generate_icon(ICON_PATH)

    # запуск daemon-потоков
    threads = []

    if HAS_MCP:
        t_mcp = threading.Thread(target=run_mcp_server, daemon=True, name="mcp")
        t_mcp.start()
        threads.append(t_mcp)

    if HAS_FLASK:
        t_flask = threading.Thread(target=run_flask, daemon=True, name="flask")
        t_flask.start()
        threads.append(t_flask)

    if CONFIG.get("auto_refresh", True) and HAS_FEEDPARSER:
        t_refresh = threading.Thread(target=auto_refresh_loop, daemon=True, name="refresh")
        t_refresh.start()
        threads.append(t_refresh)

    # первичная загрузка лент в фоне
    if HAS_FEEDPARSER:
        def initial_load():
            try:
                logging.info("Первоначальная загрузка лент…")
                for cat, feed in FEEDS.active_feeds():
                    try:
                        get_feed_cached(cat, feed, force=True)
                    except Exception:
                        continue
                logging.info("Первоначальная загрузка завершена")
            except Exception as e:
                logging.error("Ошибка первоначальной загрузки: %s", e)
        threading.Thread(target=initial_load, daemon=True).start()

    # повторное применение MCP endpoint к адаптеру (автозащита от сброса при обновлении)
    def _reapply_endpoint():
        time.sleep(5)  # даём адаптеру подняться
        ep = CONFIG.get("xiaozhi_endpoint", "")
        if ep:
            if _sync_adapter_endpoint(ep):
                logging.info("MCP endpoint восстановлен в конфиге адаптера")
    threading.Thread(target=_reapply_endpoint, daemon=True).start()

    if no_tray or not HAS_TRAY:
        logging.info("Режим без трея. Flask в основном потоке (Ctrl+C для выхода).")
        try:
            if HAS_FLASK:
                # flask-поток уже daemon — основной поток ждём
                while not _tray_stop["stop"]:
                    time.sleep(1)
            else:
                while not _tray_stop["stop"]:
                    time.sleep(1)
        except KeyboardInterrupt:
            logging.info("Получен KeyboardInterrupt — выходим")
            _tray_stop["stop"] = True
    else:
        run_tray()

    logging.info("=== Завершение %s ===", APP_NAME)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error("Фатальная ошибка: %s\n%s", e, traceback.format_exc())
        sys.exit(1)
