#!/usr/bin/env python3
"""
Krato Minerador v2 — preço REAL via Keepa.

Fluxo: Gemini gera IDEIAS (keywords, sem preço) -> Keepa dá o preço real
(média 90 dias, Amazon UK) + BSR -> calcula margem real -> filtra >= 60% ->
grava um relatório HTML no Airtable.

Roda no GitHub Actions (o Make é bloqueado pelo Keepa com 502; o Actions passa).

Secrets (env vars): KEEPA_KEY, GEMINI_API_KEY, AIRTABLE_TOKEN
"""
import os
import json
import time
import gzip
import html
import smtplib
import urllib.request
import urllib.parse
import urllib.error
from email.message import EmailMessage
from datetime import datetime, timezone, timedelta

# ---------- Config ----------
KEEPA_KEY = os.environ["KEEPA_KEY"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
AIRTABLE_TOKEN = os.environ["AIRTABLE_TOKEN"]

AIRTABLE_BASE = "appv3M1eSHNB7Voq4"
AIRTABLE_TABLE = "tblR2ZFwfJOFghveb"
F_DATA = "fld92cl3FO8zKPW6l"       # Data (DD/MM/YYYY)
F_SESSION = "fldG5W2UbWY3PGqGw"    # Sessao
F_RESULT = "fldNfGOrSKpAtPYCF"     # Resultado (HTML)
F_SESSID = "fldqadmkDBQPZiBOJ"     # Name / session id

# E-mail (Gmail SMTP com App Password) — só envia se as creds existirem
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "wellbuenorider@gmail.com")

N_IDEAS = int(os.environ.get("N_IDEAS", "8"))
BSR_MAX = int(os.environ.get("BSR_MAX", "20000"))     # BSR acima disso = vende pouco
PRICE_MIN = float(os.environ.get("PRICE_MIN", "18"))  # abaixo disso a taxa FIXA do FBA mata a margem
PRICE_MAX = float(os.environ.get("PRICE_MAX", "60"))  # descarta outlier caro
NET_FLOOR = float(os.environ.get("NET_FLOOR", "20"))  # piso de lucro LÍQUIDO (%) — regra Krato
KEEPA_DOMAIN = "2"  # amazon.co.uk

# Modelo de taxas Amazon UK FBA (aprox., empresa NÃO registrada no VAT).
# Usado p/ calcular o teto de custo (RFQ) que garante o líquido-alvo.
REFERRAL = 0.15      # comissão Amazon
FBA_FIXED = 3.00     # taxa FBA fixa (item médio) — £, não %
PPC_ALLOW = 0.15     # reserva p/ anúncio
RETURNS = 0.04       # devoluções
VAT_ON_FEES = 0.20   # IVA sobre as taxas Amazon (não recuperável sem registro)


def cost_ceiling_for_net(price, net_pct):
    """Custo landed máximo p/ atingir net_pct% de lucro líquido nesse preço."""
    referral = price * REFERRAL
    vat = (referral + FBA_FIXED) * VAT_ON_FEES
    ppc = price * PPC_ALLOW
    ret = price * RETURNS
    return round(price - (net_pct / 100.0) * price - referral - FBA_FIXED - vat - ppc - ret, 2)

# Clichês saturados que o agente NUNCA deve propor (mineração premium Krato)
EXCLUSION = [
    "bamboo hairbrush", "bamboo toothbrush holder", "mesh produce bags",
    "jade roller", "gua sha", "resistance bands", "silicone baby bibs",
    "dog snuffle mat", "bamboo bath caddy", "portable blender",
    "makeup remover pads", "salt lamp", "thermal label rolls", "qr stands",
    "barcode scanner", "tumbler", "mug", "candle", "reusable coffee cup",
]

BRAND = dict(navy="#1a1a2e", gold="#C8A96E", muted="#8a8a8a", body="#2c2c2c", bg="#eef0f2")


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "krato-miner", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode())


def _post(url, payload, timeout=60):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ---------- 1. Gemini: gerar ideias ----------
def gemini_ideas(n):
    sys = (
        "You are Krato's market-research agent for a UK platform. Output ONLY a raw JSON array, "
        "no markdown, no code fences. Produce EXACTLY 6 product objects, ONE for EACH slot in order: "
        "1) a BARCODE SCANNER (category hardware); "
        "2) a THERMAL LABEL PRINTER (category hardware); "
        "3) a LABEL product - barcode label rolls or jewelry hang/rat-tail tags (category packaging); "
        "4) a MODA COUNTRY / western-style clothing item - western plaid shirt, cowboy boots, "
        "western belt, cowboy hat (category country_fashion); "
        "5) a SEMIJOIA / costume jewelry piece - gold plated hoop earrings, layered necklace, "
        "stainless steel ring (category jewelry); "
        "6) a FASHION ACCESSORY - leather belt, scarf, hair accessory, sunglasses (category country_fashion). "
        "Each object keys: name_en, name_pt (Brazilian Portuguese), emoji (one emoji), "
        "category (hardware | packaging | country_fashion | jewelry), "
        "keyword (a COMMON Amazon.co.uk search phrase of 2-4 words that returns many real listings), "
        "est_cost_gbp (rough number), country (string), supplier (string or 'A confirmar'), "
        "reason (string, max 120 chars). MARKET RESEARCH - real Amazon UK prices, demand + images. "
        "Output ONLY the JSON array."
    )
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.5-flash:generateContent?key=" + GEMINI_KEY)
    payload = {
        "system_instruction": {"parts": [{"text": sys}]},
        "contents": [{"role": "user", "parts": [{"text": "Generate the ideas now as a raw JSON array."}]}],
        "generationConfig": {"temperature": 1.0, "maxOutputTokens": 2048,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    d = _post(url, payload)
    txt = d["candidates"][0]["content"]["parts"][0]["text"]
    txt = txt.replace("```json", "").replace("```", "").strip()
    return json.loads(txt)


# ---------- 2. Keepa: preço real + BSR ----------
_keepa_tokens = [999]  # cache do último tokensLeft conhecido


def _keepa_pace(cost):
    """Espera se faltam tokens (plano 1 token/min)."""
    if _keepa_tokens[0] < cost + 2:
        wait = max(1, cost + 2 - _keepa_tokens[0]) * 61
        print(f"  [pacing] tokens baixos ({_keepa_tokens[0]}), aguardando {wait}s...")
        time.sleep(wait)
        _keepa_tokens[0] += wait // 60


def keepa_search_asins(keyword, n=5):
    _keepa_pace(10)
    term = urllib.parse.quote_plus(keyword)
    url = f"https://api.keepa.com/search?key={KEEPA_KEY}&domain={KEEPA_DOMAIN}&type=product&asins-only=1&term={term}"
    d = _get(url)
    if "tokensLeft" in d:
        _keepa_tokens[0] = d["tokensLeft"]
    return (d.get("asinList") or [])[:n]


def keepa_products(asins):
    """Um único /product com vários ASINs (1 token cada). Retorna preço+BSR de cada."""
    if not asins:
        return []
    _keepa_pace(len(asins) + 1)
    csv = ",".join(asins)
    url = f"https://api.keepa.com/product?key={KEEPA_KEY}&domain={KEEPA_DOMAIN}&stats=90&history=0&asin={csv}"
    d = _get(url)
    if "tokensLeft" in d:
        _keepa_tokens[0] = d["tokensLeft"]
    out = []
    for p in (d.get("products") or []):
        st = p.get("stats") or {}
        avg90 = st.get("avg90") or []

        def val(i):
            return avg90[i] if len(avg90) > i and avg90[i] not in (None, -1) else None
        price = val(1) or val(0)   # New price senão Amazon price (pence)
        img_id = (p.get("imagesCSV") or "").split(",")[0].strip()
        image = f"https://images-na.ssl-images-amazon.com/images/I/{img_id}" if img_id else ""
        out.append({
            "asin": p.get("asin"),
            "price_gbp": round(price / 100, 2) if price else None,
            "bsr": val(3),
            "title": p.get("title", ""),
            "image": image,
        })
    return out


# ---------- 3. Montar relatório ----------
def enrich(idea):
    kw = idea.get("keyword", "").strip()
    out = dict(idea)
    if not kw:
        out["status"] = "sem keyword"
        return out
    try:
        asins = keepa_search_asins(kw, 3)
    except Exception as e:
        out["status"] = f"erro busca: {e}"
        return out
    if not asins:
        out["status"] = "nao encontrado na Amazon UK"
        return out
    try:
        prods = keepa_products(asins)
    except Exception as e:
        out["status"] = f"erro preco: {e}"
        return out
    # Entre os top resultados, escolhe o que VENDE mais (menor BSR) dentro da
    # faixa de preço razoável — evita a listagem outlier cara/atípica.
    cands = [p for p in prods if p["price_gbp"] and PRICE_MIN <= p["price_gbp"] <= PRICE_MAX and p["bsr"]]
    if not cands:
        out["status"] = "sem match vendavel na faixa"
        return out
    best = min(cands, key=lambda p: p["bsr"])
    out.update(best)
    venda = best["price_gbp"]
    out["cost_20"] = cost_ceiling_for_net(venda, 20)  # teto custo p/ 20% líquido (piso)
    out["cost_30"] = cost_ceiling_for_net(venda, 30)  # teto custo p/ 30% líquido (meta)
    out["status"] = "ok"
    return out


def card_html(x):
    e = lambda s: html.escape(str(s))
    bsr = x.get("bsr")
    hot = " 🔥" if bsr and bsr < 2000 else ""  # BSR baixo = vende muito
    amz = "https://www.amazon.co.uk/s?k=" + urllib.parse.quote_plus(x.get("name_en", ""))
    return f"""
<table width="100%" cellpadding="14" cellspacing="0" border="0" bgcolor="#ffffff"
 style="background-color:#ffffff;border:1px solid #eeeeee;margin-bottom:12px;font-family:sans-serif">
<tr><td>
  <img src="{e(x.get('image',''))}" alt="" width="100" style="float:right;margin:0 0 8px 12px;border:1px solid #eeeeee;background:#ffffff">
  <span style="background-color:{BRAND['gold']};color:#fff;font-size:10px;font-weight:bold;padding:2px 6px">{e(CAT_LABEL.get(x.get('category'), x.get('category','')))}</span><br>
  <span style="font-size:16px;font-weight:bold;color:{BRAND['navy']}">{e(x.get('name_en'))}{hot}</span>
  <span style="font-size:13px;color:{BRAND['muted']}"> {e(x.get('name_pt'))}</span> {e(x.get('emoji',''))}
  <br><br>
  <span style="color:{BRAND['muted']}">Preço REAL Amazon UK (Keepa 90d):</span> <b style="color:{BRAND['navy']};font-size:15px">£{e(x.get('price_gbp'))}</b>
  &nbsp;·&nbsp; <span style="color:{BRAND['muted']}">Demanda (BSR):</span> <b>{e(bsr or 'n/d')}</b>
  <br>
  <span style="color:{BRAND['gold']};font-weight:bold">🎯 Alvo de sourcing (RFQ): custo ≤ £{e(x.get('cost_20'))} p/ 20% líq · ≤ £{e(x.get('cost_30'))} p/ 30% líq</span>
  <span style="color:{BRAND['muted']};font-size:11px"> (já descontadas taxas Amazon + PPC)</span>
  <br>
  <span style="color:{BRAND['muted']};font-size:12px">Origem sugerida: {e(x.get('country'))} &nbsp;·&nbsp; Fornecedor: {e(x.get('supplier'))}</span>
  <br><span style="color:{BRAND['muted']};font-size:11px">Match Keepa: {e((x.get('title') or '')[:90])}</span>
  <br><br>
  <a href="{amz}" style="background-color:{BRAND['gold']};color:#fff;font-weight:bold;padding:8px 16px;text-decoration:none;font-size:12px">Ver na Amazon UK →</a>
</td></tr></table>"""


CAT_LABEL = {"country_fashion": "Moda Country", "jewelry": "Semijoia/Acessorio",
             "packaging": "Insumo/Embalagem", "hardware": "Hardware POS"}


def build_report(rows, date_str):
    # MODO PESQUISA: mostra o panorama real (preço + demanda) das 4 categorias
    # da plataforma p/ analisarmos. Sem filtro duro — queremos ver o mercado.
    passed = [r for r in rows if r.get("status") == "ok" and r.get("price_gbp") and r.get("bsr")]
    cat_order = {"country_fashion": 0, "jewelry": 1, "packaging": 2, "hardware": 3}
    passed.sort(key=lambda r: (cat_order.get(r.get("category"), 9), r.get("bsr") or 9e18))
    skipped = [r for r in rows if r not in passed]
    cards = "".join(card_html(r) for r in passed) or \
        f"<p style='color:{BRAND['muted']}'>Nenhum produto verificado bateu a margem real hoje.</p>"
    note = ""
    if skipped:
        items = "; ".join(f"{html.escape(r.get('name_en','?'))} ({html.escape(r.get('status','?'))})" for r in skipped)
        note = f"<p style='color:{BRAND['muted']};font-size:11px'>Descartados/não verificados: {items}</p>"
    return f"""<html><body style="background:{BRAND['bg']};padding:16px;font-family:sans-serif">
<table width="100%" style="max-width:640px" cellpadding="0" cellspacing="0" border="0" align="center"><tr><td>
<table width="100%" bgcolor="{BRAND['navy']}" style="background-color:{BRAND['navy']}" cellpadding="20"><tr><td>
  <span style="color:{BRAND['gold']};font-size:20px;font-weight:bold">Krato — Pesquisa de Mercado (Keepa)</span><br>
  <span style="color:{BRAND['muted']};font-size:13px">{date_str} · {len(passed)} produtos · Moda Country · Semijoia · Insumos · Hardware</span><br>
  <span style="color:{BRAND['gold']};font-size:11px">Preço + demanda = Amazon UK real (Keepa). Para análise de catálogo e sourcing.</span>
</td></tr></table>
<br>{cards}{note}
<p style="color:{BRAND['muted']};font-size:11px;border-left:3px solid {BRAND['gold']};padding-left:8px">KRATO GLOBAL · Well Bueno Limited · Uso interno · Confidencial</p>
</td></tr></table></body></html>"""


# ---------- 4. Airtable ----------
def airtable_write(date_str, sessid, html_report):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{AIRTABLE_TABLE}"
    payload = {"fields": {F_DATA: date_str, F_SESSION: "V2-KEEPA",
                          F_RESULT: html_report, F_SESSID: sessid}, "typecast": True}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# ---------- 5. E-mail (visual, com imagens) ----------
def send_email(subject, html_report):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        print("  [email] sem creds Gmail - pulando envio (grava so no Airtable)")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    msg.set_content("Seu leitor nao suporta HTML. Veja o relatorio no Airtable.")
    msg.add_alternative(html_report, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg)
    print("  [email] enviado para", EMAIL_TO)


def main():
    now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=1)))  # ~UK
    date_str = now.strftime("%d/%m/%Y")
    sessid = now.strftime("%Y%m%d%H%M") + "-V2"
    print(f"== Krato Miner v2 == {date_str} | {N_IDEAS} ideias | BSR max {BSR_MAX} | faixa £{PRICE_MIN}-{PRICE_MAX}")

    ideas = gemini_ideas(N_IDEAS)
    print(f"Gemini propôs {len(ideas)} ideias:", [i.get("name_en") for i in ideas])

    rows = []
    for i, idea in enumerate(ideas, 1):
        r = enrich(idea)
        print(f"  {i}. {r.get('name_en')}: {r.get('status')}"
              + (f" | venda REAL £{r.get('price_gbp')} | BSR {r.get('bsr')} | custo<=£{r.get('cost_20')}(20% liq) / £{r.get('cost_30')}(30%)" if r.get("status") == "ok" else ""))
        rows.append(r)

    report = build_report(rows, date_str)
    res = airtable_write(date_str, sessid, report)
    print("Airtable record:", res.get("id"))
    send_email(f"Krato - Pesquisa de Mercado (Keepa) | {date_str}", report)
    print("DONE")


if __name__ == "__main__":
    main()
