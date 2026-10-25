import os
import feedparser
import requests

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

RSS_FEEDS = [
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "MIT Tech Review", "url": "https://www.technologyreview.com/feed/"}
]

def summarize_with_ai(title, summary):
    if not GROQ_API_KEY:
        return summary or title

    prompt = f"""Actúa como un experto en Inteligencia Artificial. Traduce y haz un resumen directo y claro en español (máximo 3 oraciones) de la siguiente noticia sobre IA, explicando de qué trata y por qué importa.

Título: {title}
Contenido: {summary}

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
                "max_tokens": 250
            },
            timeout=15
        )
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"Error conectando con la IA: {e}")

    return summary or title

def send_news():
    if not WEBHOOK_URL:
        print("Error: Falta DISCORD_WEBHOOK_URL")
        return

    embeds = []
    for feed_info in RSS_FEEDS:
        feed = feedparser.parse(feed_info["url"])
        for entry in feed.entries[:2]: # 2 noticias por fuente
            title = entry.title
            raw_summary = getattr(entry, 'summary', '')

            # Generar resumen automático y traducción con IA
            ai_summary = summarize_with_ai(title, raw_summary)

            embeds.append({
                "title": f"🤖 {title[:200]}",
                "description": f"{ai_summary}\n\n[Leer nota completa en {feed_info['name']}]({entry.link})",
                "color": 3447003
            })

    if not embeds:
        print("No hay noticias nuevas.")
        return

    payload = {
        "content": "📰 **Resumen Diario de IA y Agentes (Generado y Traducido por IA)**",
        "embeds": embeds[:6]
    }

    res = requests.post(WEBHOOK_URL, json=payload)
    if res.status_code == 204:
        print("¡Resumen enviado con éxito a Discord!")
    else:
        print(f"Error al enviar a Discord: {res.status_code}")

if __name__ == "__main__":
    send_news()
