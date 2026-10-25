#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py (v2) - Noticias diarias de IA y Agentes de IA -> Discord (Webhook)

POR QUÉ LA VERSIÓN ANTERIOR SOLO MOSTRABA ENLACES O COPETES CORTOS
  Los servidores de GitHub Actions usan IPs de centros de datos, y muchos
  portales (TechCrunch, MIT Tech Review, etc.) las bloquean con 403 o con
  un "desafío" anti-bots, sin importar el User-Agent. Cuando eso ocurría, el
  bot caía al texto corto del RSS y el resumen quedaba pobre.

ESTRATEGIA NUEVA: EL TEXTO SE BUSCA EN CAPAS (nunca queda sin contenido)
  1. Contenido completo dentro del propio RSS (content:encoded). No se puede
     bloquear porque no hay scraping.
  2. Descarga directa de la página con 3 perfiles de navegador reales
     (Chrome, Safari, Firefox) y extracción con trafilatura. Si falla,
     BeautifulSoup (JSON-LD articleBody + párrafos del artículo).
  3. Lector externo Jina Reader (r.jina.ai), que descarga la página desde
     SU infraestructura, y por eso suele esquivar el bloqueo a GitHub.
  4. Último recurso: título + descripción del RSS.
  Siempre se elige el texto más largo disponible y SIEMPRE se envía a Groq
  para generar el resumen en español. Nunca se publica un simple enlace.

VARIABLES DE ENTORNO
  Obligatorias : DISCORD_WEBHOOK_URL, GROQ_API_KEY
  Opcionales   : MAX_AGE_HOURS (30), MAX_ARTICLES (6), MAX_PER_FEED (2),
                 JINA_API_KEY (sin clave el lector gratuito tiene límites),
                 USE_READER_FALLBACK (1), GROQ_FALLBACK_MODEL,
                 LOG_PREVIEW_CHARS (700), PAUSE_SECONDS (2)
"""

import calendar
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

try:
    import trafilatura
except ImportError:  # el bot sigue funcionando (con BeautifulSoup) si falta trafilatura
    trafilatura = None


# ======================================================================
# CONFIGURACIÓN
# ======================================================================

def env_int(name, default):
    """Lee un entero de una variable de entorno; si falta o es inválido, usa el valor por defecto."""
    valor = os.getenv(name, "").strip()
    try:
        return int(valor) if valor else default
    except ValueError:
        return default


DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
JINA_API_KEY = os.getenv("JINA_API_KEY", "").strip()

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
# Modelo de respaldo (otro cupo de uso en Groq) por si el principal agota su límite.
GROQ_FALLBACK_MODEL = os.getenv("GROQ_FALLBACK_MODEL", "llama-3.1-8b-instant").strip()

READER_URL = "https://r.jina.ai/"
USE_READER_FALLBACK = os.getenv("USE_READER_FALLBACK", "1").strip().lower() not in ("0", "false", "no", "")

MAX_AGE_HOURS = env_int("MAX_AGE_HOURS", 30)
MAX_ARTICLES = env_int("MAX_ARTICLES", 6)
MAX_PER_FEED = env_int("MAX_PER_FEED", 2)

PAUSE_SECONDS = env_int("PAUSE_SECONDS", 2)   # pausa entre solicitudes
REQUEST_TIMEOUT = 25                           # segundos (feeds, scraping, Discord)
READER_TIMEOUT = 60                            # el lector externo puede tardar más
GROQ_TIMEOUT = 90
MAX_RETRIES = 3                                # reintentos ante 429 / 5xx
MAX_WAIT_SECONDS = 90                          # si Groq pide esperar más, se cambia de modelo
LOW_TOKENS_THRESHOLD = 4500                    # si quedan menos tokens/min, se espera al reinicio

RSS_FULL_ENOUGH_CHARS = 2500   # si el RSS ya trae tanto texto, no hace falta scraping
GOOD_ENOUGH_CHARS = 1200       # texto "suficientemente completo" para un buen resumen
MAX_ARTICLE_CHARS = 6000       # máximo de texto enviado a Groq (cuida el límite de tokens)
MIN_USABLE_CHARS = 30          # por debajo de esto el texto no sirve

PREVIEW_CHARS = env_int("LOG_PREVIEW_CHARS", 700)

BOT_NAME = "Noticias IA"
EMBED_COLOR = 0x5865F2

FEEDS = [
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "MIT Technology Review", "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed"},
    {"name": "VentureBeat AI", "url": "https://venturebeat.com/category/ai/feed/"},
    {"name": "The Verge AI", "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"},
    {"name": "Ars Technica AI", "url": "https://arstechnica.com/ai/feed/"},
    {"name": "OpenAI News", "url": "https://openai.com/news/rss.xml"},
    {"name": "Hugging Face Blog", "url": "https://huggingface.co/blog/feed.xml"},
]

# Las noticias que mencionan estas palabras se priorizan (foco en agentes de IA).
AGENT_KEYWORDS = (
    "agent", "agentic", "agente", "autonomous", "autónom",
    "multi-agent", "mcp", "tool use", "tool-use",
)

# Contenedores donde suele estar el cuerpo del artículo (de más a menos específico).
ARTICLE_SELECTORS = [
    "[itemprop='articleBody']",
    ".entry-content",
    ".post-content",
    ".article-content",
    ".article-body",
    ".wp-block-post-content",
    "article",
    "main",
]

BLOCK_TAGS = [
    "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "tr", "section", "article", "br",
]

# Líneas "de relleno" típicas que no aportan al resumen.
BOILERPLATE_RE = re.compile(
    r"^(the post .{0,200}appeared first on|appeared first on|read more\b|continue reading|"
    r"subscribe\b|sign up\b|follow us\b|advertisement$|related articles?:?$|share this\b|"
    r"click here\b|leer m[aá]s\b)",
    re.IGNORECASE,
)

# Textos típicos de páginas anti-bots que llegan con HTTP 200 y NO son el artículo.
BLOCK_MARKERS = (
    "just a moment", "enable javascript and cookies", "verify you are human",
    "access denied", "attention required", "checking your browser",
    "captcha", "unusual traffic", "are you a robot", "request blocked",
)

# Perfiles de navegador reales. Si un portal bloquea uno, se prueba el siguiente.
HEADER_PROFILES = [
    {
        "name": "Chrome / Windows",
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Referer": "https://www.google.com/",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-User": "?1",
            "Sec-Ch-Ua": '"Not(A:Brand";v="99", "Google Chrome";v="144", "Chromium";v="144"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        },
    },
    {
        "name": "Safari / macOS",
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                "(KHTML, like Gecko) Version/18.6 Safari/605.1.15"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Referer": "https://www.google.com/",
        },
    },
    {
        "name": "Firefox / Linux",
        "headers": {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "Referer": "https://duckduckgo.com/",
            "Upgrade-Insecure-Requests": "1",
        },
    },
]

RSS_HEADERS = {
    "User-Agent": HEADER_PROFILES[0]["headers"]["User-Agent"],
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

SPANISH_WORDS = {
    "el", "la", "los", "las", "de", "del", "que", "en", "y", "un", "una",
    "con", "para", "por", "se", "su", "es", "al", "como", "más", "sus",
}
ENGLISH_WORDS = {
    "the", "of", "and", "to", "is", "in", "that", "for", "with", "on",
    "are", "as", "by", "this", "it", "from", "was", "has",
}

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

SYSTEM_PROMPT = """Eres un editor de noticias especializado en Inteligencia Artificial y agentes de IA.
Recibirás el texto de una noticia (normalmente en inglés) y debes entregar SIEMPRE un resumen explicativo en ESPAÑOL, aunque el texto esté en inglés, sea corto o esté incompleto.

REGLAS OBLIGATORIAS:
1. Responde ÚNICAMENTE con un objeto JSON válido (sin markdown, sin texto antes ni después).
2. TODO el contenido debe estar en español neutro. Traduce todo. Solo conserva en su idioma original los nombres propios, marcas, modelos y términos técnicos habituales (LLM, API, agent, open source, etc.).
3. Usa exactamente estas claves: "titulo", "de_que_trata", "puntos_clave", "por_que_es_relevante".
4. "titulo": título periodístico en español, máximo 110 caracteres.
5. "de_que_trata": explicación clara de 4 a 6 oraciones: qué ocurrió, quién está involucrado, cuándo o dónde y el contexto necesario para entenderlo sin leer el artículo. Si solo dispones de un extracto breve, escribe 2 a 3 oraciones con lo que haya.
6. "puntos_clave": lista de 2 a 3 textos. Cada uno es una oración completa y concreta (cifras, nombres, fechas, resultados) de hasta 220 caracteres.
7. "por_que_es_relevante": 2 a 3 oraciones sobre el impacto para la IA o los agentes de IA: industria, tecnología, riesgos u oportunidades.
8. No inventes datos que no estén en el texto. Pero NUNCA dejes un campo vacío ni respondas que falta información: resume y contextualiza lo disponible.
9. Redacta con tus propias palabras; no copies frases largas del original.
"""


class GroqAuthError(Exception):
    """La clave de Groq es inválida o no tiene permiso: no tiene sentido seguir."""


class GroqUnavailable(Exception):
    """Este modelo no puede responder ahora (límite alcanzado, caído, no existe)."""


# ======================================================================
# UTILIDADES
# ======================================================================

def log(message=""):
    """Imprime y fuerza el volcado inmediato para ver los logs en vivo en GitHub Actions."""
    print(message, flush=True)


def pause():
    """Pausa fija entre solicitudes para no saturar las APIs."""
    time.sleep(PAUSE_SECONDS)


def clean_text(text):
    """Colapsa espacios y saltos de línea en un solo espacio."""
    return re.sub(r"\s+", " ", text or "").strip()


def truncate(text, limit):
    """Recorta un texto a 'limit' caracteres agregando '…' si hace falta."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def preview(text, limit=None):
    """Vista previa de una sola línea para los logs."""
    limit = PREVIEW_CHARS if limit is None else limit
    flat = clean_text(text)
    if len(flat) <= limit:
        return flat
    return flat[:limit] + f"… [+{len(flat) - limit} caracteres más]"


def html_to_text(html):
    """Convierte HTML en texto plano conservando los saltos de párrafo."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_after("\n")
    lines = [clean_text(line) for line in soup.get_text().split("\n")]
    return "\n".join(line for line in lines if line)


def clean_article_text(text, min_line=0):
    """Quita líneas vacías, repetidas, muy cortas (menús) o de relleno."""
    lines = []
    seen = set()
    for raw in (text or "").splitlines():
        line = clean_text(raw)
        if not line or len(line) < min_line:
            continue
        if BOILERPLATE_RE.search(line):
            continue
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return "\n".join(lines)


def limit_text(text, max_chars):
    """Corta el texto en un límite de párrafo, oración o palabra (sin partir palabras)."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for separator in ("\n", ". "):
        index = cut.rfind(separator)
        if index > max_chars * 0.6:
            return cut[: index + 1].strip()
    return cut.rsplit(" ", 1)[0].strip()


def is_http_url(url):
    return isinstance(url, str) and url.lower().startswith(("http://", "https://"))


def normalize_url(url):
    """Clave para detectar noticias repetidas entre feeds."""
    parsed = urlparse(url)
    return parsed.netloc.lower() + parsed.path.rstrip("/")


def entry_datetime(entry):
    """Fecha de publicación de una entrada RSS (UTC) o None si no tiene."""
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def entry_image(entry):
    """Intenta obtener una imagen de la entrada RSS."""
    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key, []) or []:
            url = media.get("url")
            if is_http_url(url):
                return url
    for enclosure in entry.get("enclosures", []) or []:
        if str(enclosure.get("type", "")).startswith("image") and is_http_url(enclosure.get("href")):
            return enclosure["href"]
    html = entry.get("summary", "") or ""
    for content in entry.get("content", []) or []:
        html += content.get("value", "")
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html)
    if match and is_http_url(match.group(1)):
        return match.group(1)
    return None


def entry_texts(entry):
    """Devuelve (texto_más_largo_del_RSS, descripción_corta)."""
    pieces = []
    for content in entry.get("content", []) or []:
        pieces.append(html_to_text(content.get("value", "")))
    summary = html_to_text(entry.get("summary") or entry.get("description") or "")
    pieces.append(summary)
    longest = max(pieces, key=len) if pieces else ""
    return longest, summary


def fecha_argentina():
    """Fecha de hoy en español (Argentina es UTC-3 todo el año, sin horario de verano)."""
    ahora = datetime.now(timezone(timedelta(hours=-3)))
    return f"{DIAS[ahora.weekday()]} {ahora.day} de {MESES[ahora.month - 1]} de {ahora.year}"


def parse_duration(value):
    """Convierte duraciones de Groq ('7.66s', '2m59.56s', '150ms') a segundos."""
    if not value:
        return 0.0
    total = 0.0
    for number, unit in re.findall(r"([\d.]+)(ms|h|m|s)", str(value)):
        try:
            amount = float(number)
        except ValueError:
            continue
        total += {"ms": amount / 1000, "s": amount, "m": amount * 60, "h": amount * 3600}[unit]
    return total


def looks_blocked(text):
    """Detecta páginas anti-bots (Cloudflare, captchas) que llegaron como si fueran el artículo."""
    lowered = (text or "").lower()
    return len(lowered) < 2000 and any(marker in lowered for marker in BLOCK_MARKERS)


def looks_spanish(text):
    """Heurística simple: detecta si el modelo respondió en inglés por error."""
    words = re.findall(r"[a-záéíóúñü]+", (text or "").lower())
    if len(words) < 8:
        return True
    spanish = sum(1 for word in words if word in SPANISH_WORDS)
    english = sum(1 for word in words if word in ENGLISH_WORDS)
    return spanish >= english


# ======================================================================
# 1) LECTURA DE FEEDS RSS
# ======================================================================

def fetch_feed(feed):
    """Descarga un feed RSS y devuelve la lista de noticias recientes."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)

    response = requests.get(feed["url"], headers=RSS_HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    if not parsed.entries:
        raise ValueError("el feed no devolvió entradas")

    articles = []
    for entry in parsed.entries:
        link = entry.get("link", "")
        title = clean_text(entry.get("title", ""))
        if not is_http_url(link) or not title:
            continue

        published = entry_datetime(entry)
        if published is not None and published < cutoff:
            continue

        rss_text_raw, summary = entry_texts(entry)
        rss_text = clean_article_text(rss_text_raw)
        haystack = f"{title} {summary}".lower()
        articles.append({
            "source": feed["name"],
            "title": title,
            "link": link,
            "published": published,
            "summary": clean_article_text(summary),
            "rss_text": rss_text,
            "image": entry_image(entry),
            "score": sum(1 for keyword in AGENT_KEYWORDS if keyword in haystack),
        })
    return articles


def collect_candidates():
    """Lee todos los feeds y devuelve las noticias elegidas para esta ejecución."""
    log("📡 PASO 1: leyendo feeds RSS")
    all_articles = []
    seen = set()

    for feed in FEEDS:
        log(f"   • {feed['name']}")
        try:
            articles = fetch_feed(feed)
        except Exception as exc:
            log(f"     ⚠️ No se pudo leer este feed: {exc}")
            articles = []

        nuevas = 0
        con_texto_largo = 0
        for article in articles:
            key = normalize_url(article["link"])
            if key in seen:
                continue
            seen.add(key)
            all_articles.append(article)
            nuevas += 1
            if len(article["rss_text"]) >= RSS_FULL_ENOUGH_CHARS:
                con_texto_largo += 1
        log(f"     {nuevas} noticias recientes ({con_texto_largo} con el artículo completo dentro del RSS)")
        pause()

    # Primero las que hablan de agentes; a igualdad, las más nuevas.
    all_articles.sort(key=lambda a: (a["score"], a["published"] or EPOCH), reverse=True)

    selected = []
    per_source = {}
    for article in all_articles:
        if per_source.get(article["source"], 0) >= MAX_PER_FEED:
            continue
        selected.append(article)
        per_source[article["source"]] = per_source.get(article["source"], 0) + 1
        if len(selected) >= MAX_ARTICLES:
            break

    log(f"   ➜ Noticias seleccionadas para hoy: {len(selected)}")
    return selected


# ======================================================================
# 2) EXTRACCIÓN DEL TEXTO (en capas)
# ======================================================================

def download_html(url):
    """Descarga la página probando varios perfiles de navegador. Devuelve bytes o lanza error."""
    session = requests.Session()
    last_error = "sin intentos"
    total = len(HEADER_PROFILES)
    for index, profile in enumerate(HEADER_PROFILES, start=1):
        try:
            response = session.get(url, headers=profile["headers"], timeout=REQUEST_TIMEOUT, allow_redirects=True)
        except requests.RequestException as exc:
            last_error = f"error de red: {exc}"
            log(f"      · Intento {index}/{total} ({profile['name']}): {last_error}")
            pause()
            continue

        size = len(response.content)
        log(f"      · Intento {index}/{total} ({profile['name']}): HTTP {response.status_code}, {size} bytes")
        if response.status_code == 200 and size > 0:
            return response.content
        last_error = f"HTTP {response.status_code}"
        if response.status_code in (404, 410):
            break
        pause()
    raise RuntimeError(f"página bloqueada o inaccesible ({last_error})")


def page_image(soup, base_url):
    """Imagen de portada de la página (Open Graph / Twitter Card)."""
    for attrs in ({"property": "og:image"}, {"name": "twitter:image"}, {"property": "twitter:image"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            candidate = urljoin(base_url, meta["content"])
            if is_http_url(candidate):
                return candidate
    return None


def find_article_body(node):
    """Busca recursivamente la clave 'articleBody' dentro de datos JSON-LD."""
    if isinstance(node, dict):
        body = node.get("articleBody")
        if isinstance(body, str) and body.strip():
            return body
        for value in node.values():
            found = find_article_body(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = find_article_body(item)
            if found:
                return found
    return ""


def extract_with_trafilatura(html_bytes, url):
    """Extracción del texto principal con trafilatura (si está instalada)."""
    if trafilatura is None:
        return ""
    try:
        text = trafilatura.extract(
            html_bytes,
            url=url,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )
    except Exception as exc:
        log(f"      · trafilatura falló: {exc}")
        return ""
    return clean_article_text(text or "")


def extract_with_bs4(soup):
    """Respaldo con BeautifulSoup: JSON-LD (articleBody) y párrafos del artículo."""
    candidates = []

    # 1) Muchos portales incluyen el cuerpo completo en datos estructurados JSON-LD.
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        body = find_article_body(data)
        if body:
            candidates.append(clean_article_text(html_to_text(body)))

    # 2) Párrafos dentro del contenedor del artículo.
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "form", "nav", "footer", "aside", "button"]):
        tag.decompose()

    container = None
    for selector in ARTICLE_SELECTORS:
        found = soup.select_one(selector)
        if found is not None and len(found.get_text(strip=True)) >= 400:
            container = found
            break
    if container is None:
        container = soup.body or soup

    paragraphs = []
    for p in container.find_all("p"):
        text = clean_text(p.get_text(" "))
        text = re.sub(r"\s+([.,;:!?])", r"\1", text)
        paragraphs.append(text)
    candidates.append(clean_article_text("\n".join(paragraphs), min_line=40))

    return max(candidates, key=len) if candidates else ""


def scrape_direct(url):
    """Descarga la página y extrae el artículo. Devuelve (texto, imagen, método)."""
    html_bytes = download_html(url)
    soup = BeautifulSoup(html_bytes, "html.parser")
    image = page_image(soup, url)

    text = extract_with_trafilatura(html_bytes, url)
    method = "trafilatura"
    log(f"      · trafilatura extrajo {len(text)} caracteres")

    if len(text) < GOOD_ENOUGH_CHARS:
        bs_text = extract_with_bs4(soup)
        log(f"      · BeautifulSoup extrajo {len(bs_text)} caracteres")
        if len(bs_text) > len(text):
            text, method = bs_text, "BeautifulSoup"
    return text, image, method


def markdown_to_text(markdown):
    """Limpia el markdown devuelto por el lector externo."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", markdown)          # imágenes
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)           # enlaces -> solo el texto
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)  # encabezados
    text = re.sub(r"(\*\*|__|`)", "", text)                         # negritas y código
    return clean_article_text(text, min_line=50)


def fetch_via_reader(url):
    """Pide el artículo a Jina Reader, que lo descarga desde su propia infraestructura."""
    headers = {
        "Accept": "application/json",
        "X-Return-Format": "markdown",
        "User-Agent": HEADER_PROFILES[0]["headers"]["User-Agent"],
    }
    if JINA_API_KEY:
        headers["Authorization"] = f"Bearer {JINA_API_KEY}"

    response = requests.get(READER_URL + url, headers=headers, timeout=READER_TIMEOUT)
    log(f"      · Lector externo: HTTP {response.status_code}, {len(response.content)} bytes")
    response.raise_for_status()

    try:
        payload = response.json()
        content = (payload.get("data") or {}).get("content", "") or ""
    except ValueError:
        content = response.text
    return markdown_to_text(content)


def get_article_text(article):
    """
    Obtiene el mejor texto posible de la noticia probando varias capas.
    Siempre devuelve algo utilizable (como mínimo título + descripción).
    """
    log("🔎 PASO 2: obteniendo el texto de la noticia")
    candidates = []   # lista de (método, texto)
    image = article["image"]

    # Capa 1: contenido que ya viene dentro del RSS
    rss_text = article["rss_text"]
    log(f"   📡 Texto disponible en el RSS: {len(rss_text)} caracteres")
    if rss_text:
        candidates.append(("RSS (contenido del feed)", rss_text))

    def best():
        return max(candidates, key=lambda item: len(item[1])) if candidates else ("", "")

    if len(rss_text) >= RSS_FULL_ENOUGH_CHARS:
        log("   ✅ El RSS ya trae el artículo completo: no hace falta hacer scraping.")
    else:
        # Capa 2: descarga directa + trafilatura / BeautifulSoup
        log("   🌐 Scraping directo de la página:")
        try:
            text, scraped_image, method = scrape_direct(article["link"])
            log(f"   → Resultado del scraping ({method}): {len(text)} caracteres")
            if looks_blocked(text):
                log("   ⚠️ El texto obtenido es una página anti-bots, no el artículo. Se descarta.")
                text = ""
            if text:
                candidates.append((f"scraping directo ({method})", text))
            if scraped_image:
                image = scraped_image
        except Exception as exc:
            log(f"   ⚠️ Scraping directo falló: {exc}")
        pause()

        # Capa 3: lector externo
        if len(best()[1]) < GOOD_ENOUGH_CHARS and USE_READER_FALLBACK:
            log("   🛟 Texto insuficiente. Probando el lector externo (Jina Reader):")
            try:
                text = fetch_via_reader(article["link"])
                log(f"   → Resultado del lector externo: {len(text)} caracteres")
                if looks_blocked(text):
                    log("   ⚠️ El lector devolvió una página anti-bots, no el artículo. Se descarta.")
                    text = ""
                if text:
                    candidates.append(("lector externo (Jina Reader)", text))
            except Exception as exc:
                log(f"   ⚠️ El lector externo falló: {exc}")
            pause()

    method, text = best()

    # Capa 4: último recurso
    if len(text) < MIN_USABLE_CHARS:
        text = clean_text(f"{article['title']}. {article['summary']}")
        method = "solo título + descripción del RSS"
        log("   ⚠️ No se pudo obtener más texto: se usarán solo el título y la descripción.")

    return {"text": text, "method": method, "image": image}


# ======================================================================
# 3) RESUMEN Y TRADUCCIÓN CON GROQ
# ======================================================================

def retry_wait_seconds(response, attempt):
    """Segundos a esperar antes de reintentar (usa el header Retry-After si existe)."""
    header = response.headers.get("retry-after")
    try:
        return float(header) + 1
    except (TypeError, ValueError):
        return 10.0 * attempt


def throttle_if_needed(headers):
    """Si quedan pocos tokens por minuto, espera al reinicio para evitar errores 429."""
    try:
        remaining = int(headers.get("x-ratelimit-remaining-tokens"))
    except (TypeError, ValueError):
        return
    if remaining < LOW_TOKENS_THRESHOLD:
        wait = min(parse_duration(headers.get("x-ratelimit-reset-tokens")) + 1, 60)
        if wait > 1:
            log(f"   ⏳ Quedan {remaining} tokens/min en Groq: esperando {wait:.0f} s para no exceder el límite.")
            time.sleep(wait)


def call_groq(model, messages):
    """Llama a la API de Groq (compatible con OpenAI) y devuelve el texto de la respuesta."""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 1100,
        "response_format": {"type": "json_object"},
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=GROQ_TIMEOUT)
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise GroqUnavailable(f"error de red con Groq: {exc}")
            log(f"   ⚠️ Error de red con Groq ({exc}). Reintentando en 10 s ({attempt}/{MAX_RETRIES})")
            time.sleep(10)
            continue

        status = response.status_code

        if status == 200:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            log(
                f"   📊 Tokens usados: entrada={usage.get('prompt_tokens', '?')}, "
                f"salida={usage.get('completion_tokens', '?')}, total={usage.get('total_tokens', '?')}"
            )
            throttle_if_needed(response.headers)
            return content

        if status in (401, 403):
            raise GroqAuthError(f"Groq HTTP {status}: {response.text[:300]}")
        if status == 404:
            raise GroqUnavailable(f"el modelo '{model}' no existe o fue retirado: {response.text[:200]}")
        if status == 400:
            raise ValueError(f"Groq HTTP 400: {response.text[:300]}")

        if status == 429 or status >= 500:
            wait = retry_wait_seconds(response, attempt)
            if status == 429 and wait > MAX_WAIT_SECONDS:
                raise GroqUnavailable(f"límite alcanzado en '{model}' (habría que esperar {int(wait)} s)")
            if attempt == MAX_RETRIES:
                raise GroqUnavailable(f"Groq HTTP {status} tras {MAX_RETRIES} intentos")
            log(f"   ⏳ Groq respondió HTTP {status}. Reintentando en {wait:.0f} s ({attempt}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        raise GroqUnavailable(f"Groq HTTP {status}: {response.text[:300]}")

    raise GroqUnavailable("se agotaron los reintentos con Groq")


def normalize_summary(data, fallback_title):
    """Valida y limpia el JSON devuelto por el modelo."""
    if not isinstance(data, dict):
        raise ValueError("la respuesta no es un objeto JSON")

    titulo = clean_text(str(data.get("titulo") or fallback_title))
    de_que_trata = clean_text(str(data.get("de_que_trata") or ""))
    por_que = clean_text(str(data.get("por_que_es_relevante") or data.get("por_que_importa") or ""))

    puntos = data.get("puntos_clave") or []
    if isinstance(puntos, str):
        puntos = [puntos]
    puntos_limpios = []
    for punto in puntos:
        texto = clean_text(str(punto)).lstrip("-•*· ").strip()
        if texto:
            puntos_limpios.append(texto)
    puntos_limpios = puntos_limpios[:3]

    if not de_que_trata:
        raise ValueError("falta 'de_que_trata'")
    if not puntos_limpios:
        raise ValueError("faltan 'puntos_clave'")
    if not por_que:
        raise ValueError("falta 'por_que_es_relevante'")
    if not looks_spanish(f"{de_que_trata} {por_que}"):
        raise ValueError("el resumen no está en español")

    return {
        "titulo": titulo,
        "de_que_trata": de_que_trata,
        "puntos_clave": puntos_limpios,
        "por_que_es_relevante": por_que,
    }


def summarize_with_groq(article, text, method):
    """Traduce y resume SIEMPRE en español. Prueba el modelo principal y, si hace falta, el de respaldo."""
    log("🤖 PASO 3: resumiendo y traduciendo con Groq")

    calidad = (
        "artículo completo"
        if len(text) >= GOOD_ENOUGH_CHARS
        else "extracto breve (resume solo lo disponible, sin inventar, pero completa las 3 secciones)"
    )
    base_prompt = (
        f"Fuente: {article['source']}\n"
        f"Título original: {article['title']}\n"
        f"Calidad del texto: {calidad}\n\n"
        f"TEXTO DE LA NOTICIA:\n\"\"\"\n{text}\n\"\"\"\n\n"
        "Devuelve el JSON en español con las claves titulo, de_que_trata, puntos_clave y por_que_es_relevante."
    )
    retry_note = (
        "\n\nATENCIÓN: tu respuesta anterior no cumplió el formato. "
        "Responde SOLO con JSON válido y con TODO el texto en español."
    )

    models = [GROQ_MODEL]
    if GROQ_FALLBACK_MODEL and GROQ_FALLBACK_MODEL != GROQ_MODEL:
        models.append(GROQ_FALLBACK_MODEL)

    last_error = None
    for model in models:
        for attempt in (1, 2):
            log(f"   → Modelo: {model} (intento {attempt}/2)")
            prompt = base_prompt if attempt == 1 else base_prompt + retry_note
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
            try:
                raw = call_groq(model, messages).strip()
                log("   📥 RESPUESTA CRUDA DE GROQ:")
                for line in truncate(raw, 3000).splitlines():
                    log(f"      {line}")
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
                summary = normalize_summary(json.loads(raw), article["title"])
                summary["model"] = model
                return summary
            except GroqAuthError:
                raise
            except GroqUnavailable as exc:
                last_error = exc
                log(f"   ⚠️ {model} no disponible: {exc}")
                break  # pasa al siguiente modelo
            except (ValueError, KeyError) as exc:  # JSON inválido, incompleto o en inglés
                last_error = exc
                log(f"   ⚠️ Respuesta inválida de {model}: {exc}")
                pause()

    raise RuntimeError(f"Groq no pudo generar el resumen: {last_error}")


# ======================================================================
# 4) PUBLICACIÓN EN DISCORD
# ======================================================================

def build_embed(article, summary, image, method):
    """Arma el Embed de Discord con las 3 secciones, respetando sus límites."""
    link = article["link"].replace(")", "%29")
    bullets = "\n".join("• " + truncate(punto, 300) for punto in summary["puntos_clave"])

    description = (
        "**1️⃣ ¿De qué trata la noticia?**\n"
        f"{truncate(summary['de_que_trata'], 1400)}\n\n"
        "**2️⃣ Puntos clave**\n"
        f"{bullets}\n\n"
        "**3️⃣ ¿Por qué es relevante para la IA?**\n"
        f"{truncate(summary['por_que_es_relevante'], 800)}\n\n"
        f"[🔗 Leer el artículo original]({link})"
    )

    embed = {
        "title": truncate(summary["titulo"], 250),
        "url": article["link"],
        "description": truncate(description, 4000),
        "color": EMBED_COLOR,
        "author": {"name": truncate("📰 " + article["source"], 250)},
        "footer": {"text": truncate(f"Resumen en español con IA · {summary['model']} · Texto base: {method}", 2000)},
    }
    if article["published"]:
        embed["timestamp"] = article["published"].isoformat()
    if image:
        embed["thumbnail"] = {"url": image}
    return embed


def validate_webhook():
    """Comprueba que el webhook exista (GET no publica nada en el canal)."""
    try:
        response = requests.get(DISCORD_WEBHOOK_URL, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        log(f"❌ No se pudo contactar con Discord: {exc}")
        return False
    if response.status_code == 200:
        try:
            name = response.json().get("name", "?")
        except ValueError:
            name = "?"
        log(f"✅ Webhook de Discord válido (nombre: {name})")
        return True
    log(f"❌ Discord respondió HTTP {response.status_code}. El webhook es inválido o fue borrado: {response.text[:200]}")
    return False


def post_to_discord(payload):
    """Publica un mensaje en el Webhook. Reintenta si Discord responde 429."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            log(f"   ❌ Error de red al publicar en Discord: {exc}")
            return False

        if response.status_code in (200, 204):
            log(f"   📨 Discord: publicado (HTTP {response.status_code})")
            return True

        if response.status_code == 429:
            try:
                wait = float(response.json().get("retry_after", 5))
            except (ValueError, TypeError, AttributeError):
                wait = 5.0
            log(f"   ⏳ Discord pidió esperar {wait:.1f} s ({attempt}/{MAX_RETRIES})")
            time.sleep(min(wait, 30) + 0.5)
            continue

        log(f"   ❌ Discord respondió HTTP {response.status_code}: {response.text[:300]}")
        return False
    return False


def send_message(content):
    """Envía un mensaje de texto simple (encabezado o aviso)."""
    return post_to_discord({
        "username": BOT_NAME,
        "content": content,
        "allowed_mentions": {"parse": []},
    })


def write_step_summary(rows):
    """Escribe una tabla-resumen en la página de la ejecución de GitHub Actions."""
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path or not rows:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("## 📰 Resultado de la ejecución\n\n")
            handle.write("| # | Fuente | Noticia | Texto obtenido por | Caracteres | Modelo | Estado |\n")
            handle.write("|---|---|---|---|---|---|---|\n")
            for index, row in enumerate(rows, start=1):
                title = truncate(row["title"], 70).replace("|", "\\|")
                status = str(row["status"]).replace("|", "\\|")
                handle.write(
                    f"| {index} | {row['source']} | {title} | {row['method']} | "
                    f"{row['chars']} | {row['model']} | {status} |\n"
                )
    except OSError as exc:
        log(f"⚠️ No se pudo escribir el resumen de la ejecución: {exc}")


# ======================================================================
# PROCESAMIENTO DE UNA NOTICIA
# ======================================================================

def process_article(article, index, total, state):
    """Texto -> resumen con Groq -> publicación en Discord."""
    log()
    log("=" * 70)
    log(f"📰 [{index}/{total}] {article['source']}")
    log(f"   Título : {article['title']}")
    log(f"   URL    : {article['link']}")
    if article["published"]:
        log(f"   Fecha  : {article['published'].strftime('%Y-%m-%d %H:%M')} UTC")
    log("-" * 70)

    info = get_article_text(article)
    text = limit_text(info["text"], MAX_ARTICLE_CHARS)
    log(f"   ✅ TEXTO ELEGIDO → método: {info['method']} | {len(info['text'])} caracteres "
        f"(se envían {len(text)} a Groq)")
    log("   📄 VISTA PREVIA DEL TEXTO EXTRAÍDO:")
    log(f"      {preview(text)}")
    log("-" * 70)

    summary = summarize_with_groq(article, text, info["method"])
    log(f"   ✅ Resumen generado con {summary['model']}: «{truncate(summary['titulo'], 100)}»")
    pause()

    log("📨 PASO 4: publicando en Discord")
    if not state["header_sent"]:
        if not send_message(
            f"📰 **Noticias de IA y Agentes de IA · {fecha_argentina()}**\n"
            "Las novedades más relevantes, resumidas y traducidas al español 👇"
        ):
            raise RuntimeError("Discord rechazó el mensaje de encabezado")
        state["header_sent"] = True
        pause()

    embed = build_embed(article, summary, info["image"], info["method"])
    payload = {
        "username": BOT_NAME,
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    if not post_to_discord(payload):
        raise RuntimeError("Discord rechazó la publicación")
    pause()

    return {
        "source": article["source"],
        "title": article["title"],
        "method": info["method"],
        "chars": len(info["text"]),
        "model": summary["model"],
        "status": "✅ publicada",
    }


# ======================================================================
# PROGRAMA PRINCIPAL
# ======================================================================

def main():
    log("=" * 70)
    log("🤖 BOT DE NOTICIAS DE IA → DISCORD (v2)")
    log("=" * 70)
    log(f"Python {sys.version.split()[0]} | trafilatura: "
        f"{getattr(trafilatura, '__version__', 'instalada') if trafilatura else 'NO instalada (se usará BeautifulSoup)'}")

    log(f"DISCORD_WEBHOOK_URL: {'configurada ✅' if DISCORD_WEBHOOK_URL else 'FALTA ❌'}")
    log(f"GROQ_API_KEY       : {'configurada ✅' if GROQ_API_KEY else 'FALTA ❌'}")
    log(f"JINA_API_KEY       : {'configurada ✅' if JINA_API_KEY else 'no configurada (opcional)'}")

    if not DISCORD_WEBHOOK_URL or not GROQ_API_KEY:
        log("❌ Faltan variables de entorno. Revisa los secrets DISCORD_WEBHOOK_URL y GROQ_API_KEY "
            "y que el workflow los pase en la sección 'env'.")
        return 1
    if "/api/webhooks/" not in DISCORD_WEBHOOK_URL:
        log("❌ DISCORD_WEBHOOK_URL no parece una URL de webhook de Discord válida.")
        return 1

    log(f"Configuración: ventana={MAX_AGE_HOURS}h | máx. noticias={MAX_ARTICLES} | "
        f"máx. por fuente={MAX_PER_FEED} | modelo={GROQ_MODEL} | respaldo={GROQ_FALLBACK_MODEL or 'ninguno'} | "
        f"lector externo={'sí' if USE_READER_FALLBACK else 'no'}")
    log()

    if not validate_webhook():
        return 1
    log()

    candidates = collect_candidates()

    if not candidates:
        log(f"📭 No hay noticias nuevas en las últimas {MAX_AGE_HOURS} horas.")
        enviado = send_message(
            f"📭 **{fecha_argentina()}**: hoy no hubo noticias nuevas de IA en las fuentes monitoreadas."
        )
        return 0 if enviado else 1

    state = {"header_sent": False}
    rows = []
    ok = 0
    failed = 0

    for index, article in enumerate(candidates, start=1):
        try:
            rows.append(process_article(article, index, len(candidates), state))
            ok += 1
        except GroqAuthError as exc:
            log(f"❌ Error de autenticación con Groq. Revisa GROQ_API_KEY. Detalle: {exc}")
            for pending in candidates[index - 1:]:
                rows.append({
                    "source": pending["source"], "title": pending["title"], "method": "-",
                    "chars": 0, "model": "-", "status": "❌ error de clave de Groq",
                })
            failed += len(candidates) - index + 1
            break
        except Exception as exc:
            failed += 1
            log(f"   ❌ No se pudo procesar esta noticia: {exc}")
            rows.append({
                "source": article["source"], "title": article["title"], "method": "-",
                "chars": 0, "model": "-", "status": f"❌ {truncate(str(exc), 80)}",
            })

    log()
    log("=" * 70)
    log(f"🏁 FINALIZADO: {ok} publicadas, {failed} fallidas")
    log("=" * 70)
    write_step_summary(rows)
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
