import os
import feedparser
import requests
from bs4 import BeautifulSoup

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

RSS_FEEDS = [
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "MIT Tech Review", "url": "https://www.technologyreview.com/feed/"}
]

def fetch_full_article_text(url):
    """Entra al enlace de la noticia y extrae el texto del cuerpo del artículo."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            paragraphs = soup.find_all('p')
            full_text = " ".join([p.get_text() for p in paragraphs if len(p.get_text()) > 30])
            return full_text[:3500]
    except Exception as e:
        print(f"Error al extraer artículo de {url}: {e}")
    return ""

def summarize_with_ai(title, full_text):
    """Utiliza Groq para analizar y resumir el contenido completo del artículo."""
    if not GROQ_API_KEY or not full_text:
        return "No se pudo extraer el contenido completo para resumir."

    prompt = f"""Eres un experto analista en Inteligencia Artificial. Lee el siguiente artículo completo y haz un resumen detallado y explicativo en ESPAÑOL.

Estructura el resumen de la siguiente manera:
1. **¿De qué trata?**: Explica de forma clara el avance, noticia o agente de IA mencionado.
2. **Puntos clave**: Agrega 2 o 3 viñetas con los detalles más importantes.
3. **¿Por qué importa?**: Explica brevemente su impacto.

Título: {title}
Texto del artículo: {full_text}

Resumen en español:"""

    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 450
            },
            timeout=20
        )
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"Error en API de Groq: {e}")

    return "Error al generar el resumen con la IA."

def send_news():
    if not WEBHOOK_URL:
        print("Error: Falta DISCORD_WEBHOOK_URL")
        return

    requests.post(WEBHOOK_URL, json={"content": "🤖 **¡Resumen Diario Detallado de Noticias sobre IA y Agentes!**\n*Procesado, extraído y resumido con IA:*"})

    for feed_info in RSS_FEEDS:
        feed = feedparser.parse(feed_info["url"])
        for entry in feed.entries[:2]:
            title = entry.title
            link = entry.link

            print(f"Extrayendo y procesando: {title}")
            full_text = fetch_full_article_text(link)
            ai_summary = summarize_with_ai(title, full_text)

            embed = {
                "title": f"📰 {title[:200]}",
                "description": f"{ai_summary}\n\n🔗 [Leer nota completa original en {feed_info['name']}]({link})",
                "color": 3447003
            }

            requests.post(WEBHOOK_URL, json={"embeds": [embed]})

if __name__ == "__main__":
    send_news()
