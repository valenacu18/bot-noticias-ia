#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py - Noticias diarias de IA y Agentes de IA -> Discord (Webhook)

Flujo:
  1. Lee varios feeds RSS de fuentes sobre IA.
  2. Filtra las noticias recientes y prioriza las que hablan de agentes.
  3. Hace scraping del artículo completo (con headers de navegador real).
     Si el sitio bloquea (403, etc.), usa la descripción corta del RSS.
  4. Pide a Groq (llama-3.3-70b-versatile) un resumen en español con
     3 secciones: ¿De qué trata?, Puntos clave y ¿Por qué importa?
  5. Publica cada noticia como un Embed en Discord mediante un Webhook.

Variables de entorno obligatorias:
  DISCORD_WEBHOOK_URL, GROQ_API_KEY

Variables de entorno opcionales:
  MAX_AGE_HOURS (30)  -> antigüedad máxima de las noticias, en horas
  MAX_ARTICLES  (6)   -> máximo de noticias por ejecución
  MAX_PER_FEED  (2)   -> máximo de noticias por fuente
"""

import calendar
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup


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

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

MAX_AGE_HOURS = env_int("MAX_AGE_HOURS", 30)
MAX_ARTICLES = env_int("MAX_ARTICLES", 6)
MAX_PER_FEED = env_int("MAX_PER_FEED", 2)

PAUSE_SECONDS = 2          # pausa entre solicitudes para no saturar las APIs
REQUEST_TIMEOUT = 20       # segundos (feeds, scraping, Discord)
GROQ_TIMEOUT = 60          # segundos
MAX_RETRIES = 3            # reintentos ante errores 429 / 5xx
MAX_ARTICLE_CHARS = 4500   # texto máximo que se envía a Groq (cuida los tokens)
MIN_ARTICLE_CHARS = 500    # menos que esto = scraping inútil -> fallback al RSS

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

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

RSS_HEADERS = dict(
    BROWSER_HEADERS,
    Accept="application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
)

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

SYSTEM_PROMPT = """Eres un periodista tecnológico experto en Inteligencia Artificial y agentes de IA.
Tu tarea es leer una noticia (normalmente en inglés) y producir un resumen estructurado en ESPAÑOL claro y neutro.

Reglas:
- Responde ÚNICAMENTE con un objeto JSON válido, sin texto adicional ni bloques de markdown.
- Usa exactamente estas claves: "titulo", "de_que_trata", "puntos_clave", "por_que_importa".
- "titulo": el título traducido al español, máximo 120 caracteres.
- "de_que_trata": 2 a 3 oraciones que expliquen el tema central. Máximo 450 caracteres.
- "puntos_clave": lista de 3 a 5 textos, cada uno una frase corta (máximo 150 caracteres) con datos concretos (cifras, nombres, fechas) cuando existan.
- "por_que_importa": 1 a 2 oraciones sobre el impacto o la relevancia para el sector de la IA. Máximo 350 caracteres.
- No inventes información que no esté en el texto.
- Conserva nombres propios, marcas, modelos y términos técnicos habituales (LLM, agent, API, etc.).
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("noticias-ia")


class GroqFatalError(Exception):
    """Error de Groq que no se arregla reintentando (clave inválida, límite diario, etc.)."""


# ======================================================================
# UTILIDADES
# ======================================================================

def pause():
    """Pausa fija entre solicitudes para no saturar las APIs."""
    time.sleep(PAUSE_SECONDS)


def clean_text(text):
    """Colapsa espacios y saltos de línea."""
    return re.sub(r"\s+", " ", text or "").strip()


def truncate(text, limit):
    """Recorta un texto a 'limit' caracteres agregando '…' si hace falta."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def html_to_text(html):
    """Convierte un fragmento HTML (descripción del RSS) en texto plano."""
    if not html:
        return ""
    return clean_text(BeautifulSoup(html, "html.parser").get_text(" "))


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


def fecha_argentina():
    """Fecha de hoy en español (Argentina es UTC-3 todo el año, sin horario de verano)."""
    ahora = datetime.now(timezone(timedelta(hours=-3)))
    return f"{DIAS[ahora.weekday()]} {ahora.day} de {MESES[ahora.month - 1]} de {ahora.year}"


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

        summary = html_to_text(entry.get("summary") or entry.get("description") or "")
        if not summary and entry.get("content"):
            summary = html_to_text(entry["content"][0].get("value", ""))

        haystack = f"{title} {summary}".lower()
        articles.append({
            "source": feed["name"],
            "title": title,
            "link": link,
            "published": published,
            "summary": summary,
            "image": entry_image(entry),
            "score": sum(1 for keyword in AGENT_KEYWORDS if keyword in haystack),
        })
    return articles


def collect_candidates():
    """Lee todos los feeds y devuelve las noticias elegidas para esta ejecución."""
    all_articles = []
    seen = set()

    for feed in FEEDS:
        log.info("Leyendo feed: %s", feed["name"])
        try:
            articles = fetch_feed(feed)
        except Exception as exc:
            log.warning("  No se pudo leer el feed %s: %s", feed["name"], exc)
            articles = []

        nuevas = 0
        for article in articles:
            key = normalize_url(article["link"])
            if key in seen:
                continue
            seen.add(key)
            all_articles.append(article)
            nuevas += 1
        log.info("  %d noticias recientes", nuevas)
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

    log.info("Noticias seleccionadas: %d", len(selected))
    return selected


# ======================================================================
# 2) SCRAPING DEL ARTÍCULO COMPLETO
# ======================================================================

def scrape_article(url):
    """
    Descarga el artículo y devuelve (texto, imagen).
    Si el sitio bloquea (403, etc.) lanza una excepción; si el contenido
    es insuficiente (muro de pago, JavaScript) devuelve ("", imagen).
    En ambos casos el llamador usa la descripción del RSS como respaldo.
    """
    response = requests.get(url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")

    # Imagen de portada (Open Graph / Twitter Card)
    image = None
    meta = (
        soup.find("meta", attrs={"property": "og:image"})
        or soup.find("meta", attrs={"name": "twitter:image"})
    )
    if meta and meta.get("content"):
        candidate = urljoin(url, meta["content"])
        if is_http_url(candidate):
            image = candidate

    # Eliminar ruido (menús, scripts, publicidad, formularios...)
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "form", "nav", "footer", "aside", "button"]):
        tag.decompose()

    # Buscar el contenedor del cuerpo del artículo
    container = None
    for selector in ARTICLE_SELECTORS:
        candidate = soup.select_one(selector)
        if candidate is not None and len(candidate.get_text(strip=True)) >= MIN_ARTICLE_CHARS:
            container = candidate
            break
    if container is None:
        container = soup.body or soup

    paragraphs = []
    for p in container.find_all("p"):
        text = clean_text(p.get_text(" "))
        if len(text) >= 40:
            paragraphs.append(text)

    full_text = " ".join(paragraphs)
    if len(full_text) < MIN_ARTICLE_CHARS:
        return "", image
    if len(full_text) > MAX_ARTICLE_CHARS:
        full_text = full_text[:MAX_ARTICLE_CHARS].rsplit(" ", 1)[0]
    return full_text, image


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


def call_groq(messages):
    """Llama a la API de Groq (compatible con OpenAI) y devuelve el texto de la respuesta."""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 700,
        "response_format": {"type": "json_object"},
    }

    for attempt in range(1, MAX_RETRIES + 1):
        response = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=GROQ_TIMEOUT)
        status = response.status_code

        if status == 200:
            return response.json()["choices"][0]["message"]["content"]

        if status in (401, 403, 404):
            raise GroqFatalError(f"Groq HTTP {status}: {response.text[:300]}")

        if status == 429 or status >= 500:
            wait = retry_wait_seconds(response, attempt)
            if wait > 120:
                raise GroqFatalError(
                    f"Límite de Groq alcanzado (habría que esperar {int(wait)} s): {response.text[:200]}"
                )
            if attempt < MAX_RETRIES:
                log.warning(
                    "  Groq respondió %s. Reintentando en %.0f s (intento %d/%d)",
                    status, wait, attempt, MAX_RETRIES,
                )
                time.sleep(wait)
                continue

        raise RuntimeError(f"Groq HTTP {status}: {response.text[:300]}")

    raise RuntimeError("Groq: se agotaron los reintentos")


def normalize_summary(data, fallback_title):
    """Valida y limpia el JSON devuelto por el modelo."""
    if not isinstance(data, dict):
        raise ValueError("la respuesta no es un objeto JSON")

    titulo = clean_text(str(data.get("titulo") or fallback_title))
    de_que_trata = clean_text(str(data.get("de_que_trata") or ""))
    por_que_importa = clean_text(str(data.get("por_que_importa") or ""))

    puntos = data.get("puntos_clave") or []
    if isinstance(puntos, str):
        puntos = [puntos]
    puntos_limpios = []
    for punto in puntos:
        texto = clean_text(str(punto)).lstrip("-•*· ").strip()
        if texto:
            puntos_limpios.append(texto)

    if not (de_que_trata and por_que_importa and puntos_limpios):
        raise ValueError("el resumen vino incompleto")

    return {
        "titulo": titulo,
        "de_que_trata": de_que_trata,
        "puntos_clave": puntos_limpios,
        "por_que_importa": por_que_importa,
    }


def summarize_with_groq(article, text, from_rss):
    """Traduce y resume la noticia en español con 3 secciones."""
    tipo = (
        "extracto breve del RSS (no inventes detalles que no aparezcan)"
        if from_rss
        else "artículo completo"
    )
    user_prompt = (
        f"Fuente: {article['source']}\n"
        f"Título original: {article['title']}\n"
        f"Tipo de contenido: {tipo}\n\n"
        f"Texto:\n\"\"\"\n{text}\n\"\"\"\n\n"
        "Devuelve el resumen en español como objeto JSON."
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    last_error = None
    for attempt in (1, 2):
        try:
            raw = call_groq(messages).strip()
            # Por si el modelo envuelve el JSON en ```json ... ```
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
            return normalize_summary(json.loads(raw), article["title"])
        except (ValueError, KeyError) as exc:  # JSON inválido o incompleto
            last_error = exc
            log.warning("  Respuesta de Groq inválida (intento %d/2): %s", attempt, exc)
            pause()
    raise RuntimeError(f"Groq devolvió un resumen inválido: {last_error}")


# ======================================================================
# 4) PUBLICACIÓN EN DISCORD
# ======================================================================

def build_embed(article, summary, image, from_rss):
    """Arma el Embed de Discord respetando sus límites de longitud."""
    bullets = []
    used = 0
    for punto in summary["puntos_clave"][:5]:
        line = "• " + truncate(punto, 220)
        if used + len(line) + 1 > 1000:
            break
        bullets.append(line)
        used += len(line) + 1

    footer = "Resumen y traducción con IA · Groq (Llama 3.3 70B)"
    if from_rss:
        footer += " · Basado en el extracto del RSS"

    embed = {
        "title": truncate(summary["titulo"], 250),
        "url": article["link"],
        "description": f"[🔗 Leer artículo original]({article['link']})",
        "color": EMBED_COLOR,
        "author": {"name": truncate("📰 " + article["source"], 250)},
        "fields": [
            {"name": "1️⃣ ¿De qué trata?", "value": truncate(summary["de_que_trata"], 1000), "inline": False},
            {"name": "2️⃣ Puntos clave", "value": "\n".join(bullets), "inline": False},
            {"name": "3️⃣ ¿Por qué importa?", "value": truncate(summary["por_que_importa"], 1000), "inline": False},
        ],
        "footer": {"text": truncate(footer, 2000)},
    }
    if article["published"]:
        embed["timestamp"] = article["published"].isoformat()
    if image:
        embed["thumbnail"] = {"url": image}
    return embed


def post_to_discord(payload):
    """Publica un mensaje en el Webhook. Reintenta si Discord responde 429."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            log.error("  Error de red al publicar en Discord: %s", exc)
            return False

        if response.status_code in (200, 204):
            return True

        if response.status_code == 429:
            try:
                wait = float(response.json().get("retry_after", 5))
            except (ValueError, TypeError, AttributeError):
                wait = 5.0
            log.warning("  Discord pidió esperar %.1f s (intento %d/%d)", wait, attempt, MAX_RETRIES)
            time.sleep(min(wait, 30) + 0.5)
            continue

        log.error("  Discord respondió %s: %s", response.status_code, response.text[:300])
        return False
    return False


def send_message(content):
    """Envía un mensaje de texto simple (encabezado o aviso)."""
    return post_to_discord({
        "username": BOT_NAME,
        "content": content,
        "allowed_mentions": {"parse": []},
    })


# ======================================================================
# PROCESAMIENTO DE UNA NOTICIA
# ======================================================================

def process_article(article):
    """Scraping -> resumen con Groq -> publicación en Discord."""
    image = article["image"]
    from_rss = False

    # 1) Scraping con fallback al RSS
    try:
        text, scraped_image = scrape_article(article["link"])
        if scraped_image:
            image = scraped_image
    except Exception as exc:
        log.warning("  Scraping bloqueado o fallido (%s)", exc)
        text = ""
    pause()

    if text:
        log.info("  Scraping OK (%d caracteres)", len(text))
    else:
        text = article["summary"][:MAX_ARTICLE_CHARS]
        from_rss = True
        log.info("  Usando la descripción del RSS como respaldo (%d caracteres)", len(text))
        if len(text) < 40:
            raise ValueError("no hay contenido ni descripción suficiente para resumir")

    # 2) Resumen y traducción
    summary = summarize_with_groq(article, text, from_rss)
    log.info("  Resumen generado con Groq")
    pause()

    # 3) Publicación
    embed = build_embed(article, summary, image, from_rss)
    payload = {
        "username": BOT_NAME,
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    if not post_to_discord(payload):
        raise RuntimeError("Discord rechazó la publicación")
    log.info("  Publicado en Discord")
    pause()


# ======================================================================
# PROGRAMA PRINCIPAL
# ======================================================================

def main():
    log.info("Iniciando bot de noticias de IA")

    if not DISCORD_WEBHOOK_URL or not GROQ_API_KEY:
        log.error(
            "Faltan variables de entorno. Revisa que existan los secrets "
            "DISCORD_WEBHOOK_URL y GROQ_API_KEY y que el workflow los pase en 'env'."
        )
        return 1
    if "/api/webhooks/" not in DISCORD_WEBHOOK_URL:
        log.error("DISCORD_WEBHOOK_URL no parece una URL de webhook de Discord válida.")
        return 1

    log.info(
        "Configuración: ventana=%dh, máx. noticias=%d, máx. por fuente=%d",
        MAX_AGE_HOURS, MAX_ARTICLES, MAX_PER_FEED,
    )

    candidates = collect_candidates()

    if not candidates:
        log.info("No hay noticias nuevas en las últimas %d horas.", MAX_AGE_HOURS)
        enviado = send_message(
            f"📭 **{fecha_argentina()}**: hoy no hubo noticias nuevas de IA en las fuentes monitoreadas."
        )
        return 0 if enviado else 1

    # Encabezado del día (también valida que el webhook funcione antes de gastar tokens de Groq)
    encabezado = send_message(
        f"📰 **Noticias de IA y Agentes de IA · {fecha_argentina()}**\n"
        "Las novedades más relevantes, resumidas y traducidas al español 👇"
    )
    if not encabezado:
        log.error("No se pudo publicar en Discord. Revisa que el webhook sea correcto y siga existiendo.")
        return 1
    pause()

    ok = 0
    failed = 0
    for index, article in enumerate(candidates, start=1):
        log.info("Procesando [%d/%d] %s: %s", index, len(candidates), article["source"], article["title"])
        try:
            process_article(article)
            ok += 1
        except GroqFatalError as exc:
            log.error("Error irrecuperable de Groq, se detiene la ejecución: %s", exc)
            failed += len(candidates) - index + 1
            break
        except Exception as exc:
            failed += 1
            log.error("  No se pudo procesar esta noticia: %s", exc)

    log.info("Finalizado: %d publicadas, %d fallidas", ok, failed)
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
