import os
import time
import requests
import sqlite3
import threading
import logging
from datetime import datetime, timedelta
from nltk.sentiment.vader import SentimentIntensityAnalyzer
import nltk
import yfinance as yf
from flask import Flask

# Postavljanje profesionalnog logiranja
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Preuzimanje leksikona za sentiment
try:
    nltk.data.find('sentiment/vader_lexicon.zip')
except LookupError:
    nltk.download('vader_lexicon', quiet=True)

# =====================================================================
# ⚙️ KONFIGURACIJA
# =====================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8964167822:AAGh7YASZWqK5mUGA3oYOGFyLuxhpJeh_D0")
CHAT_ID = os.environ.get("CHAT_ID", "8361333990")
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "YZNS80AY1A5OQVL4")

PORTFELJ = ["AAPL", "TSLA", "NVDA", "MSFT"]
BAZA_PUTANJA = "trgovanje_povijest.db" # 3. Prijedlog - prilagođeno kako na Androidu (Pydroid) ne bi bacao PermissionError za /data/

baza_lock = threading.Lock()
aktivan_signal = None  # Globalni spremnik za zadnji signal koji čeka A/B/C potvrdu

# Cache rječnici
cache_stita = {}        
cache_sentiment = {}    
cache_prosjeka = {}     

# =====================================================================
# 🗄️ BAZA PODATAKA (Poboljšana verzija s portfeljem i budžetom)
# =====================================================================
def izvrsi_upit(upit, parametri=(), fetch=False, fetchall=False):
    """Sigurna centralizirana funkcija za rad s bazom zaštićena lokotom"""
    with baza_lock:
        conn = sqlite3.connect(BAZA_PUTANJA, timeout=30)
        cursor = conn.cursor()
        cursor.execute(upit, parametri)
        if fetch:
            rezultat = cursor.fetchone()
        elif fetchall:
            rezultat = cursor.fetchall()
        else:
            rezultat = None
            conn.commit()
        conn.close()
        return rezultat

def inicijaliziraj_bazu():
    # Tablica signala i odluka AI-a
    izvrsi_upit('''
        CREATE TABLE IF NOT EXISTS signali (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vrijeme TEXT,
            ticker TEXT,
            cijena_kod_signala REAL,
            vjerojatnost_modela REAL,
            tip_signala TEXT,
            status_provjere TEXT DEFAULT 'CEKANJE'
        )
    ''')
    # OTVARANJE POZICIJA - nova tablica koju ste predložili
    izvrsi_upit('''
        CREATE TABLE IF NOT EXISTS pozicije (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vrijeme_ulaza TEXT,
            ticker TEXT,
            kolicina REAL,
            cijena_ulaza REAL,
            status TEXT DEFAULT 'OTVORENO'
        )
    ''')
    # KONFIGURACIJA - za trajno spremanje budžeta
    izvrsi_upit('''
        CREATE TABLE IF NOT EXISTS konfiguracija (
            kljuc TEXT PRIMARY KEY,
            vrijednost REAL
        )
    ''')
    
    # Ako budžet nije definiran u bazi, postavi na 1000 kao početni kapital
    if not izvrsi_upit("SELECT vrijednost FROM konfiguracija WHERE kljuc = 'BUDZET'", fetch=True):
        izvrsi_upit("INSERT INTO konfiguracija (kljuc, vrijednost) VALUES ('BUDZET', 1000.0)")

def get_budzet():
    res = izvrsi_upit("SELECT vrijednost FROM konfiguracija WHERE kljuc = 'BUDZET'", fetch=True)
    return res[0] if res else 1000.0

def update_budzet(iznos):
    izvrsi_upit("UPDATE konfiguracija SET vrijednost = ? WHERE kljuc = 'BUDZET'", (iznos,))

def zapamti_signal(ticker, cijena, vjerojatnost, tip):
    izvrsi_upit('''
        INSERT INTO signali (vrijeme, ticker, cijena_kod_signala, vjerojatnost_modela, tip_signala)
        VALUES (?, ?, ?, ?, ?)
    ''', (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ticker, cijena, vjerojatnost, tip))

# =====================================================================
# 📱 TELEGRAM KOMUNIKACIJA
# =====================================================================
def posalji_telegram_poruku(tekst):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try: 
        requests.post(url, json={"chat_id": CHAT_ID, "text": tekst, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e: 
        logging.error(f"Telegram greška pri slanju: {e}")

def pozadinski_telegram_slusac():
    global aktivan_signal
    zadnji_update_id = 0
    logging.info("Pozadinski Telegram radnik pokrenut...")
    
    # 1. Prijedlog - praznimo stare poruke prije glavne petlje kako bot ne bi reagirao na zaostale komande
    try:
        poc_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        poc_res = requests.get(poc_url, timeout=10).json()
        for update in poc_res.get("result", []):
            zadnji_update_id = update.get("update_id", zadnji_update_id)
        if zadnji_update_id > 0:
            logging.info(f"Očišćen red čekanja starih poruka. Zadnji ID: {zadnji_update_id}")
    except Exception as e:
        logging.warning(f"Neuspješno početno čišćenje poruka: {e}")
    
    while True:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        payload = {"offset": zadnji_update_id + 1, "timeout": 10}
        try:
            res = requests.get(url, json=payload, timeout=15).json()
            for update in res.get("result", []):
                zadnji_update_id = update.get("update_id", zadnji_update_id)
                poruka = update.get("message", {})
                tekst_poruke = str(poruka.get("text", "")).strip().upper()
                chat_id_posiljatelja = str(poruka.get("chat", {}).get("id", ""))
                
                if chat_id_posiljatelja == str(CHAT_ID):
                    
                    # 1. NAREDBE (Commands)
                    if tekst_poruke == "/STANJE":
                        trenutni_b = get_budzet()
                        status_burze = "OTVORENA 🟢" if je_li_burza_otvorena() else "ZATVORENA 🔴"
                        posalji_telegram_poruku(f"📊 *STATUS AI:* \n💰 Budžet: *{trenutni_b:.2f} €*\n📈 Burza: {status_burze}")
                    
                    elif tekst_poruke == "/PORTFELJ":
                        pozicije = izvrsi_upit("SELECT id, ticker, kolicina, cijena_ulaza FROM pozicije WHERE status = 'OTVORENO'", fetchall=True)
                        if not pozicije:
                            posalji_telegram_poruku("💼 *Vaš portfelj je trenutno prazan.*\nNema otvorenih pozicija.")
                        else:
                            poruka = "💼 *VAŠ PORTFELJ (Otvorene pozicije):*\n\n"
                            ukupna_vrijednost = 0
                            ukupni_ulozak = 0
                            for poz in pozicije:
                                p_id, p_ticker, p_kol, p_cijena_ulaza = poz
                                tren_cijena = dohvati_live_cijanu(p_ticker)
                                if tren_cijena == 0:
                                    tren_cijena = p_cijena_ulaza # Fallback
                                
                                ulog = p_kol * p_cijena_ulaza
                                trenutna_vr = p_kol * tren_cijena
                                razlika = trenutna_vr - ulog
                                postotak = (razlika / ulog) * 100 if ulog > 0 else 0
                                
                                ukupni_ulozak += ulog
                                ukupna_vrijednost += trenutna_vr
                                
                                emotikon = "🟢" if razlika >= 0 else "🔴"
                                poruka += f"📦 *{p_ticker}* ({p_kol:.4f} kom)\n"
                                poruka += f"  • Ulaz: {p_cijena_ulaza:.2f} $ | Trenutno: {tren_cijena:.2f} $\n"
                                poruka += f"  • P/L: {emotikon} *{razlika:.2f} €* ({postotak:+.2f}%)\n\n"
                            
                            tot_razlika = ukupna_vrijednost - ukupni_ulozak
                            tot_emoti = "🟢" if tot_razlika >= 0 else "🔴"
                            poruka += f"========================\n"
                            poruka += f"💵 *Ukupno uloženo:* {ukupni_ulozak:.2f} €\n"
                            poruka += f"📊 *Trenutna vrijednost:* {ukupna_vrijednost:.2f} €\n"
                            poruka += f"⚖️ *Ukupni P/L:* {tot_emoti} *{tot_razlika:.2f} €*"
                            
                            posalji_telegram_poruku(poruka)

                    elif tekst_poruke.startswith("/BUDZET"):
                        try:
                            novi_iznos = float(tekst_poruke.split()[1])
                            update_budzet(novi_iznos)
                            posalji_telegram_poruku(f"✅ Proračun trajno ažuriran u bazi na *{novi_iznos:.2f} €*.")
                        except:
                            posalji_telegram_poruku("❌ Krivi format. Napiši npr. `/budzet 1500`.")
                            
                    # 2. OBRADA SIGNALA KUPNJE (A, B, C)
                    elif tekst_poruke in ["A", "B", "C"]:
                        if aktivan_signal:
                            ticker, cijena, preporuka, timestamp = aktivan_signal
                            
                            # Određivanje uloga ovisno o odgovoru
                            ulog = preporuka if tekst_poruke == "A" else (preporuka * 0.5 if tekst_poruke == "B" else 0)
                            
                            if ulog > 0:
                                kolicina = ulog / cijena
                                budzet_stanje = get_budzet()
                                
                                if budzet_stanje >= ulog:
                                    update_budzet(budzet_stanje - ulog)  # Smanji novčanik
                                    vrijeme = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                    # Pospremi poziciju u bazu
                                    izvrsi_upit("INSERT INTO pozicije (vrijeme_ulaza, ticker, kolicina, cijena_ulaza) VALUES (?, ?, ?, ?)", 
                                                (vrijeme, ticker, kolicina, cijena))
                                    
                                    posalji_telegram_poruku(
                                        f"✅ *Nalog Izvršen ({tekst_poruke})!*\n"
                                        f"📦 Kupljeno: `{ticker}` ({kolicina:.4f} dionica)\n"
                                        f"💰 Cijena nabave: {cijena} $\n"
                                        f"💶 Uloženo: {ulog:.2f} €\n"
                                        f"💳 Preostali budžet: {budzet_stanje - ulog:.2f} €"
                                    )
                                else:
                                    posalji_telegram_poruku(f"❌ *Odbijeno!* Nemate dovoljno budžeta. (Traženo: {ulog:.2f} €, Raspoloživo: {budzet_stanje:.2f} €)")
                            else:
                                posalji_telegram_poruku("⏭ *Signal preskočen.* Čekam sljedeću priliku.")
                            
                            # Očisti signal bez obzira na ishod (A, B ili C)
                            aktivan_signal = None
                        else:
                            posalji_telegram_poruku("ℹ️ Trenutno nema aktivnog signala na ekranu za odabir.")

        except Exception as e:
            time.sleep(2)  

# =====================================================================
# ⏰ BURZA VRIJEME
# =====================================================================
def je_li_burza_otvorena():
    sada = datetime.now()
    if sada.weekday() >= 5: return False
    pocetak = datetime.strptime("15:30", "%H:%M").time()
    kraj = datetime.strptime("22:00", "%H:%M").time()
    return pocetak <= sada.time() <= kraj

# =====================================================================
# 📈 PAMETNI MODULI ZA BURZU (YFinance + Alpha Vantage)
# =====================================================================
def dohvati_live_cijanu(ticker):
    try:
        dionica = yf.Ticker(ticker)
        trenutna_cijena = dionica.info.get('regularMarketPrice') or dionica.info.get('currentPrice')
        
        if not trenutna_cijena:
            hist = dionica.history(period="1d")
            if not hist.empty:
                trenutna_cijena = hist['Close'].iloc[-1]
                
        return float(trenutna_cijena) if trenutna_cijena else 0.0
    except: return 0.0

def dohvati_prosjek_5_dana(ticker):
    sada = datetime.now()
    if ticker in cache_prosjeka:
        vrijeme_kesa, vrijednost = cache_prosjeka[ticker]
        if sada - vrijeme_kesa < timedelta(hours=12): return vrijednost
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        if not hist.empty:
            prosjek = hist['Close'].mean()
            if prosjek > 0: cache_prosjeka[ticker] = (sada, float(prosjek))
            return float(prosjek)
        return 0.0
    except: return cache_prosjeka.get(ticker, (0, 0.0))[1]

def provjeri_anti_hype_stit(ticker):
    sada = datetime.now()
    if ticker in cache_stita:
        vrijeme_kesa, vrijednost = cache_stita[ticker]
        if sada - vrijeme_kesa < timedelta(hours=24): return vrijednost
    try:
        info = yf.Ticker(ticker).info
        margin = info.get("profitMargins", 0)
        debt_eq = info.get("debtToEquity", 0)
        debt_eq_norm = debt_eq / 100.0 if debt_eq and debt_eq > 10 else debt_eq
        
        ishod = not (margin and margin < -0.20 or (debt_eq_norm and debt_eq_norm > 3.0))
        cache_stita[ticker] = (sada, ishod)
        return ishod
    except: return cache_stita.get(ticker, (0, True))[1]

def analiziraj_sentiment_vijesti(ticker):
    sada = datetime.now()
    if ticker in cache_sentiment:
        vrijeme_kesa, vrijednost = cache_sentiment[ticker]
        if sada - vrijeme_kesa < timedelta(minutes=15): return vrijednost
    try:
        url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&tickers={ticker}&apikey={ALPHA_VANTAGE_KEY}"
        res = requests.get(url, timeout=10).json()
        feed = res.get("feed", [])
        if not feed: return 0.0
        sia = SentimentIntensityAnalyzer()
        ukupni_score = sum([sia.polarity_scores(a.get("title", ""))["compound"] for a in feed[:5]])
        prosjek_sentiment = ukupni_score / min(len(feed), 5)
        cache_sentiment[ticker] = (sada, prosjek_sentiment)
        return prosjek_sentiment
    except: return cache_sentiment.get(ticker, (0, 0.0))[1]

def dohvati_faktor_ucenja(ticker):
    redovi = izvrsi_upit(
        "SELECT status_provjere FROM signali WHERE ticker = ? AND status_provjere IN ('PROMAŠAJ', 'POGODAK') ORDER BY id DESC LIMIT 5",
        (ticker,), fetchall=True
    )
    zadnji_rezultati = [red[0] for red in redovi]
    return max(0.70, 1.0 - (zadnji_rezultati.count("PROMAŠAJ") * 0.05))

def analiziraj_stare_signale_i_uci():
    redovi = izvrsi_upit("SELECT id, vrijeme, ticker, cijena_kod_signala, tip_signala FROM signali WHERE status_provjere = 'CEKANJE'", fetchall=True)
    if not redovi: return
    
    for red in redovi:
        id_signala, vrijeme_str, ticker, cijena_tada, tip = red
        if datetime.now() > datetime.strptime(vrijeme_str, "%Y-%m-%d %H:%M:%S") + timedelta(days=3):
            trenutna_cijena = dohvati_live_cijanu(ticker)
            if trenutna_cijena == 0: continue
            ishod = "POGODAK" if (tip == "KUPNJA" and trenutna_cijena > cijena_tada) or (tip == "PANIKA" and trenutna_cijena < cijena_tada) else "PROMAŠAJ"
            izvrsi_upit("UPDATE signali SET status_provjere = ? WHERE id = ?", (ishod, id_signala))

def izracunaj_kelly_ulog(vjerojatnost, trenutni_budzet):
    kelly_postotak = vjerojatnost - (1 - vjerojatnost)
    if kelly_postotak <= 0: return 0.0, 0.0
    konacni_postotak = min(kelly_postotak, 0.20) # max 20% limit
    return konacni_postotak, trenutni_budzet * konacni_postotak

# =====================================================================
# 🌐 WEB SERVER (Za UptimeRobot & Render)
# =====================================================================
app = Flask(__name__)

@app.route('/')
def home():
    return "AI Stražar je online i radi bez prestanka! 📈"

def run_flask():
    # Render.com dinamično dodjeljuje PORT
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)

# =====================================================================
# 🚀 GLAVNI POGON
# =====================================================================
if __name__ == "__main__":
    inicijaliziraj_bazu()
    
    # NOVO: Pokretanje Flask web servera za Render.com
    flask_nit = threading.Thread(target=run_flask, daemon=True)
    flask_nit.start()
    
    telegram_nit = threading.Thread(target=pozadinski_telegram_slusac, daemon=True)
    telegram_nit.start()
    
    logging.info("Glavni pogon za burzu upaljen.")
    posalji_telegram_poruku("🤖 *AI Stražar v6.0 (Full Interactivity) je online!*\n\nSustav obogaćen bazom za pozicije i budžet.\nKada primite signal, odgovorite na Telegram poruku sa *A*, *B* ili *C*.")
    
    memorija_stanja = {dionica: "NEUTRALNO" for dionica in PORTFELJ}
    brojac_krugova = 0
    
    while True:
        brojac_krugova += 1
        
        if not je_li_burza_otvorena():
            logging.info("Burza zatvorena. Sljedeća provjera za 30 min.")
            time.sleep(1800)
            continue
            
        if brojac_krugova % 50 == 0:
            try: analiziraj_stare_signale_i_uci()
            except Exception as e: logging.error(f"Učenje greška: {e}")
            
        for dionica in PORTFELJ:
            
            # SIGURNOSNI MEHANIZAM ČEKANJA ODLUKA KORISNIKA
            if aktivan_signal:
                _, _, _, vrijeme_signala = aktivan_signal
                # Ako korisnik ne odgovori 15 minuta (900 sekundi), automatski poništi signal
                if time.time() - vrijeme_signala > 900:
                    posalji_telegram_poruku("⏳ *Signal je istekao!* Predugo se čekalo na odluku korisnika, nastavljam analizu portfelja.")
                    aktivan_signal = None
                else:
                    logging.info("Čekam odluku korisnika (A/B/C) na Telegramu...")
                    time.sleep(15)
                    continue  # Preskoči daljnju obradu do odluke
            
            cijena = dohvati_live_cijanu(dionica)
            if cijena == 0: 
                time.sleep(2)
                continue
                
            prosjek = dohvati_prosjek_5_dana(dionica)
            if not provjeri_anti_hype_stit(dionica): continue
            sentiment = analiziraj_sentiment_vijesti(dionica)
            
            vjerojatnost = 0.50
            razlozi = []
            trenutno_stanje = "NEUTRALNO"
            
            if cijena > prosjek and prosjek > 0:
                vjerojatnost += 0.12
                razlozi.append("Cijena drži stabilan rast iznad prosjeka.")
            if sentiment > 0.15:
                vjerojatnost += 0.13
                razlozi.append(f"AI sentiment vijesti je pozitivan ({sentiment:+.2f}).")
                
            faktor = dohvati_faktor_ucenja(dionica)
            if faktor < 1.0:
                vjerojatnost *= faktor
                razlozi.append(f"⚠️ Pouzdanost smanjena zbog nedavnih grešaka modela (-{int((1-faktor)*100)}%).")
                
            if vjerojatnost >= 0.62: trenutno_stanje = "KUPNJA"
            elif cijena < prosjek and sentiment < -0.20 and prosjek > 0: trenutno_stanje = "PANIKA"
            
            if trenutno_stanje != memorija_stanja[dionica]:
                memorija_stanja[dionica] = trenutno_stanje
                
                if trenutno_stanje in ["KUPNJA", "PANIKA"]:
                    zapamti_signal(dionica, cijena, vjerojatnost, trenutno_stanje)
                
                if trenutno_stanje == "KUPNJA":
                    trenutni_budzet = get_budzet()
                    postotak, novac = izracunaj_kelly_ulog(vjerojatnost, trenutni_budzet)
                    
                    if novac > 0.5: # Minimalni prag da uopće traži kupnju
                        # POSTAVLJANJE SIGNALA NA ČEKANJE ZA KORISNIKA
                        aktivan_signal = (dionica, cijena, novac, time.time())
                        
                        razlozi_tekst = "\n".join([f"• {r}" for r in razlozi])
                        poruka = (
                            f"📈 *AI SIGNAL NA ČEKANJU ({dionica})*\n\n"
                            f"{razlozi_tekst}\n\n"
                            f"• Cijena: *{cijena:.2f} $*\n"
                            f"• Pouzdanost: *{vjerojatnost*100:.0f}%*\n"
                            f"🧮 *Preporučeni max. ulog:* {novac:.2f} €\n"
                            f"💳 *Raspoloživo na računu:* {trenutni_budzet:.2f} €\n\n"
                            f"💡 *Odgovori slovom u chat:*\n"
                            f"🅰️ *A* - Kupi agresivno (Uloži {novac:.2f} €)\n"
                            f"🅱️ *B* - Kupi konzervativno (Uloži {novac*0.5:.2f} €)\n"
                            f"❌ *C* - Preskoči priliku"
                        )
                        posalji_telegram_poruku(poruka)
                        break  # Prekini trenutnu for-petlju zbog čekanja odluke
                        
                elif trenutno_stanje == "PANIKA":
                    posalji_telegram_poruku(f"🚨 *ALARM ZA PRODAJU ({dionica}):* Cijena pada na *{cijena:.2f} $*. Trend i vijesti su loši!")
            
            time.sleep(15)  # 2. Prijedlog - Bitno je ostaviti 15s pauze unutar for-petlje kako bi se izbjegao "429 Too Many Requests" IP blok od strane Yahoo API-ja
            
        time.sleep(10) # Manja pauza prije sljedećeg portfeljnog ciklusa
