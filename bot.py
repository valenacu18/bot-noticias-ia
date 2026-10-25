import os
import feedparser
import requests

# Lee la URL guardada de forma segura en los Secrets de GitHub
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Portales especializados en noticias de IA y Agentes
RSS_FEEDS = [
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "VentureBeat AI", "url": "https://feeds.feedburner.com/venturebeat/SggU"},
    {"name": "MIT Tech Review", "url": "https://www.technologyreview.com/feed/"}
]

def send_news():
    if not WEBHOOK_URL:
        print("Error: No se encontró la variable DISCORD_WEBHOOK_URL")
        return

    embeds = []
    
    # Recorre los feeds y saca los titulares más recientes
    for feed_info in RSS_FEEDS:
        feed = feedparser.parse(feed_info["url"])
        for entry in feed.entries[:2]:  # 2 noticias por fuente
            embeds.append({
                "title": entry.title,
                "description": f"📌 **Fuente:** {feed_info['name']}",
                "url": entry.link,
                "color": 3447003  # Color azul
            })

    if not embeds:
        print("No se encontraron noticias para enviar.")
        return

    # Formato de mensaje para Discord
    payload = {
        "content": "🤖 **¡Resumen Diario de Noticias sobre IA y Agentes!**",
        "embeds": embeds[:10]  # Discord permite hasta 10 tarjetas por mensaje
    }

    # Envia las noticias al canal mediante el Webhook
    response = requests.post(WEBHOOK_URL, json=payload)
    
    if response.status_code == 204:
        print("Noticias enviadas correctamente a Discord.")
    else:
        print(f"Error enviando mensaje: {response.status_code}")

if __name__ == "__main__":
    send_news()
