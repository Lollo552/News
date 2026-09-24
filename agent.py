"""
News Agent -> Telegram (attualità, crypto o qualsiasi tema)
Legge i feed RSS delle fonti in config.json, trova le notizie nuove
e te le manda su Telegram. Opzionale: traduzione in italiano e voto
di importanza con Claude (se imposti il secret ANTHROPIC_API_KEY).
"""
import html
import json
import os
import time
from pathlib import Path

import feedparser
import requests

CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "config.json"))
SEEN_FILE = Path("seen.json")
MAX_SEEN = 3000          # quante notizie "già viste" ricordare
MAX_PER_RUN = 15         # massimo messaggi per esecuzione (anti-spam)

TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
CLAUDE_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# "true" = manda solo le notizie importanti, "false" = manda tutto
SOLO_IMPORTANTI = os.environ.get("SOLO_IMPORTANTI", "true").lower() == "true"
MIN_IMPORTANZA = int(os.environ.get("MIN_IMPORTANZA", "3"))  # da 1 a 5, usato con Claude

# La SEC blocca le richieste senza un User-Agent con un contatto
UA = "NewsAgent/1.0 (contatto: newsagent@example.com)"

def carica_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def leggi_feed(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    return feedparser.parse(r.content)


def id_notizia(entry):
    return entry.get("id") or entry.get("link") or entry.get("title", "")


def e_calda(titolo, parole):
    t = f" {titolo.lower()} "
    return any(p.lower() in t for p in parole)


def valuta_con_claude(notizie, config):
    """Traduce i titoli e dà un voto 1-5. Se qualcosa va storto, lascia tutto com'è."""
    elenco = "\n".join(f"{i}. [{n['fonte']}] {n['titolo']}" for i, n in enumerate(notizie, 1))
    prompt = (
        f"Sei il caporedattore di un servizio di notizie su: {config['tema']}. "
        "Per ogni notizia qui sotto: scrivi il titolo in italiano (traducilo se serve) "
        "e dai un voto di importanza da 1 a 5. "
        f"Criteri: {config['criteri']}\n"
        "Se due notizie parlano dello stesso fatto, dai il voto pieno solo alla prima e 1 alle altre.\n"
        "Rispondi SOLO con un array JSON, senza altro testo, nel formato: "
        '[{"n":1,"titolo_it":"...","importanza":3,"motivo":"massimo 12 parole"}]\n\n'
        + elenco
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 3000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        testo = "".join(b.get("text", "") for b in r.json()["content"])
        testo = testo.replace("```json", "").replace("```", "").strip()
        for v in json.loads(testo):
            n = notizie[int(v["n"]) - 1]
            n["titolo_it"] = v.get("titolo_it")
            n["importanza"] = int(v.get("importanza", 0))
            n["motivo"] = v.get("motivo", "")
    except Exception as e:
        print(f"Claude non disponibile, invio senza traduzione: {e}")
    return notizie


def manda_telegram(testo):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={
            "chat_id": TG_CHAT,
            "text": testo,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    if not r.ok:
        print(f"Errore Telegram: {r.status_code} {r.text}")


def formatta(n):
    fuoco = "🔥 " if n.get("importanza", 0) >= 4 or n.get("calda") else ""
    righe = [f"{fuoco}<b>{html.escape(n.get('titolo_it') or n['titolo'])}</b>"]
    if n.get("titolo_it"):
        righe.append(f"<i>{html.escape(n['titolo'])}</i>")
    if n.get("importanza"):
        righe.append(f"Importanza: {n['importanza']}/5 · {html.escape(n.get('motivo', ''))}")
    righe.append(f"📰 {html.escape(n['fonte'])}")
    if n.get("link"):
        righe.append(f'<a href="{html.escape(n["link"])}">Apri la notizia</a>')
    return "\n".join(righe)


def main():
    config = carica_json(CONFIG_FILE, {})
    feeds = config.get("feeds", [])
    parole = config.get("parole_calde", [])
    primo_avvio = not SEEN_FILE.exists()
    visti = carica_json(SEEN_FILE, [])
    visti_set = set(visti)
    nuove = []

    for feed in feeds:
        try:
            dati = leggi_feed(feed["url"])
        except Exception as e:
            print(f"Feed non raggiungibile: {feed['nome']} -> {e}")
            continue
        for entry in dati.entries[:25]:
            nid = id_notizia(entry)
            if not nid or nid in visti_set:
                continue
            visti.append(nid)
            visti_set.add(nid)
            titolo = entry.get("title", "").strip()
            nuove.append({
                "fonte": feed["nome"],
                "titolo": titolo,
                "link": entry.get("link", ""),
                "calda": e_calda(titolo, parole),
            })

    SEEN_FILE.write_text(json.dumps(visti[-MAX_SEEN:], ensure_ascii=False), encoding="utf-8")

    if primo_avvio:
        manda_telegram(f"✅ Agent attivo su: {config.get('tema', 'notizie')}. Seguo {len(feeds)} fonti: "
                       "da ora ti scrivo quando esce qualcosa di nuovo.")
        print(f"Primo avvio: {len(nuove)} notizie segnate come già viste.")
        return

    if not nuove:
        print("Nessuna notizia nuova.")
        return

    if CLAUDE_KEY:
        nuove = valuta_con_claude(nuove, config)
        if SOLO_IMPORTANTI:
            nuove = [n for n in nuove if n.get("importanza", 5) >= MIN_IMPORTANZA]
    elif SOLO_IMPORTANTI:
        nuove = [n for n in nuove if n["calda"]]

    nuove.sort(key=lambda n: (n.get("importanza", 0), n["calda"]), reverse=True)
    for n in nuove[:MAX_PER_RUN]:
        manda_telegram(formatta(n))
        time.sleep(1)
    print(f"Inviate {min(len(nuove), MAX_PER_RUN)} notizie.")


if __name__ == "__main__":
    main()
