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
import matplotlib.pyplot as plt
import io
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
    # POVIJEST TRGOVANJA - za pamćenje završenih pozicija
    izvrsi_upit('''
        CREATE TABLE IF NOT EXISTS povijest_trgovanja (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vrijeme TEXT,
            ticker TEXT,
            kolicina REAL,
            cijena_prodaje REAL,
            realizirani_pl REAL
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

def prodaj_poziciju(ticker, zeljena_kolicina_str="SVE"):
    pozicije = izvrsi_upit("SELECT id, kolicina, cijena_ulaza FROM pozicije WHERE ticker = ? AND status = 'OTVORENO'", (ticker,), fetchall=True)
    if not pozicije:
        return False, f"❌ Nemaš otvorenih pozicija za *{ticker}*."
        
    ukupna_kolicina = sum(p[1] for p in pozicije)
    
    if zeljena_kolicina_str == "SVE":
        zeljena_kolicina = ukupna_kolicina
    else:
        try:
            zeljena_kolicina = float(str(zeljena_kolicina_str).replace(',', '.'))
        except ValueError:
            return False, "❌ Količina mora biti broj (npr. 0.5) ili SVE."
            
    if zeljena_kolicina > ukupna_kolicina:
        return False, f"❌ Nemaš toliko dionica. Tvoja ukupna količina za {ticker} je {ukupna_kolicina:.4f} kom."
        
    tren_cijena = dohvati_live_cijanu(ticker)
    if tren_cijena == 0:
        return False, f"❌ Trenutno ne mogu dohvatiti live cijenu za {ticker}. Pokušaj kasnije."
        
    preostalo_za_prodati = zeljena_kolicina
    zarada_od_prodaje = 0
    ostvareni_pl = 0
    
    for poz in pozicije:
        p_id, p_kol, p_ulaz = poz
        if preostalo_za_prodati <= 0: break
            
        prodajem_iz_ove = min(p_kol, preostalo_za_prodati)
        vr_prodaje = prodajem_iz_ove * tren_cijena
        vr_ulaganja = prodajem_iz_ove * p_ulaz
        
        zarada_od_prodaje += vr_prodaje
        ostvareni_pl += (vr_prodaje - vr_ulaganja)
        
        nova_kol = p_kol - prodajem_iz_ove
        if nova_kol <= 0.00001:
            izvrsi_upit("UPDATE pozicije SET status = 'ZATVORENO', kolicina = 0 WHERE id = ?", (p_id,))
        else:
            izvrsi_upit("UPDATE pozicije SET kolicina = ? WHERE id = ?", (nova_kol, p_id))
            
        preostalo_za_prodati -= prodajem_iz_ove
        
    novi_b = get_budzet() + zarada_od_prodaje
    update_budzet(novi_b)
    
    vrijeme = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    izvrsi_upit("INSERT INTO povijest_trgovanja (vrijeme, ticker, kolicina, cijena_prodaje, realizirani_pl) VALUES (?, ?, ?, ?, ?)", 
                (vrijeme, ticker, zeljena_kolicina, tren_cijena, ostvareni_pl))
    
    emoti = "🟢" if ostvareni_pl >= 0 else "🔴"
    return True, (
        f"🤝 *ZATVORENA POZICIJA ({ticker})*\n\n"
        f"• Prodano komada: {zeljena_kolicina:.4f}\n"
        f"• Cijena izvršenja: {tren_cijena:.2f} $\n"
        f"• Vrijednost isplate: {zarada_od_prodaje:.2f} €\n"
        f"• Uknjižen P/L: {emoti} *{ostvareni_pl:.2f} €*\n\n"
        f"💳 *Novi budžet:* {novi_b:.2f} €"
    )

def yap():
    pass

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

def posalji_sliku_telegram(slika_bajtovi, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try: 
        requests.post(url, data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "Markdown"}, files={"photo": slika_bajtovi}, timeout=15)
    except Exception as e: 
        logging.error(f"Telegram greška pri slanju slike: {e}")

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
                            
                            podaci_za_pie = []
                            labele_za_pie = []
                            
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
                                
                                if trenutna_vr > 0:
                                    podaci_za_pie.append(trenutna_vr)
                                    labele_za_pie.append(p_ticker)
                                
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
                            
                            if podaci_za_pie:
                                try:
                                    plt.figure(figsize=(6, 6))
                                    plt.pie(podaci_za_pie, labels=labele_za_pie, autopct='%1.1f%%', startangle=140, colors=plt.cm.Paired.colors)
                                    plt.title("Udio u portfelju (€)")
                                    buf = io.BytesIO()
                                    plt.savefig(buf, format='png')
                                    buf.seek(0)
                                    plt.close()
                                    posalji_sliku_telegram(buf, poruka)
                                except Exception as e:
                                    logging.error(f"Grafikon error: {e}")
                                    posalji_telegram_poruku(poruka)
                            else:
                                posalji_telegram_poruku(poruka)

                    elif tekst_poruke.startswith("/BUDZET"):
                        try:
                            novi_iznos = float(tekst_poruke.split()[1])
                            update_budzet(novi_iznos)
                            posalji_telegram_poruku(f"✅ Proračun trajno ažuriran u bazi na *{novi_iznos:.2f} €*.")
                        except:
                            posalji_telegram_poruku("❌ Krivi format. Napiši npr. `/budzet 1500`.")
                            
                    elif tekst_poruke.startswith("PRODAJ ") or tekst_poruke.startswith("/PRODAJ "):
                        dijelovi = tekst_poruke.split()
                        if len(dijelovi) < 2:
                            posalji_telegram_poruku("❌ *Uputa za prodaju:*\nPošalji `prodaj TICKER` za prodaju cijele pozicije (npr. `prodaj AAPL`).\nIli `prodaj TICKER KOLICINA` za djelomičnu (npr. `prodaj AAPL 0.5`).")
                        else:
                            p_ticker = dijelovi[1]
                            z_kol = dijelovi[2] if len(dijelovi) > 2 else "SVE"
                            uspjeh, txt = prodaj_poziciju(p_ticker, z_kol)
                            posalji_telegram_poruku(txt)
                            
                    elif tekst_poruke == "/PANIC" or tekst_poruke == "/OCISTI_CRVENO":
                        pozicije = izvrsi_upit("SELECT ticker, kolicina, cijena_ulaza FROM pozicije WHERE status = 'OTVORENO'", fetchall=True)
                        if not pozicije:
                            posalji_telegram_poruku("💼 Tvoj portfelj je trenutno prazan. Nema panike!")
                            continue
                            
                        suma_po_tickeru = {}
                        for p in pozicije:
                            t = p[0]
                            k = p[1]
                            cijena = p[2]
                            ulog = k * cijena
                            if t not in suma_po_tickeru: suma_po_tickeru[t] = {"kol": 0, "ulog": 0}
                            suma_po_tickeru[t]["kol"] += k
                            suma_po_tickeru[t]["ulog"] += ulog
                            
                        prodano_tekst = "🚨 *PANIC BUTTON AKTIVIRAN* 🚨\nZatvorene gubitaške pozicije:\n\n"
                        barem_jedna = False
                        
                        for t, podaci in suma_po_tickeru.items():
                            tren_cijena = dohvati_live_cijanu(t)
                            if tren_cijena == 0: continue
                            
                            trenutna_vr = podaci["kol"] * tren_cijena
                            razlika = trenutna_vr - podaci["ulog"]
                            if razlika < 0:
                                uspjeh, result_txt = prodaj_poziciju(t, "SVE")
                                if uspjeh:
                                    barem_jedna = True
                                    prodano_tekst += f"🔻 {t}: prodano {podaci['kol']:.4f} kom, gubitak: {razlika:.2f} €\n"
                        
                        if barem_jedna:
                            prodano_tekst += f"\n💳 *Novi budžet:* {get_budzet():.2f} €"
                            posalji_telegram_poruku(prodano_tekst)
                        else:
                            posalji_telegram_poruku("✅ Nema 'crvenih' pozicija (gubitaka) u portfelju.")
                            
                    elif tekst_poruke == "/POVIJEST" or tekst_poruke == "/DNEVNIK":
                        povijest = izvrsi_upit("SELECT vrijeme, ticker, kolicina, cijena_prodaje, realizirani_pl FROM povijest_trgovanja ORDER BY id DESC LIMIT 15", fetchall=True)
                        if not povijest:
                            posalji_telegram_poruku("📭 Nema zabilježenih prošlih trgovanja.")
                        else:
                            txt = "📓 *ZADNJA TRGOVANJA (15 novijih):*\n\n"
                            for p in povijest:
                                datum, ticker, kol, cijena, rpl = p
                                e = "🟢" if rpl >= 0 else "🔴"
                                txt += f"• `{datum[:16]}` | {ticker} ({kol:.2f} kom) | PL: {e} {rpl:.2f} €\n"
                            posalji_telegram_poruku(txt)
                            
                    elif tekst_poruke == "/PROFIT":
                        tekuci_mjesec = datetime.now().strftime("%Y-%m")
                        povijest = izvrsi_upit("SELECT realizirani_pl FROM povijest_trgovanja WHERE vrijeme LIKE ?", (tekuci_mjesec + "%",), fetchall=True)
                        ukupni_profit = sum([float(p[0]) for p in povijest]) if povijest else 0.0
                        e = "🟢" if ukupni_profit >= 0 else "🔴"
                        posalji_telegram_poruku(f"📊 *Mjesečni P/L ({tekuci_mjesec}):*\n{e} *{ukupni_profit:.2f} €*")
                            
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
                                    posalji_telegram_poruku(f"❌ *Odbijeno!* Nemate dovoljno budžeta. (Traženo: {ulog:.2f} €, Raspoloživo: {budzet_stanje:.2f} €)"
