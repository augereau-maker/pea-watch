"""PEA Watch — surveille la stratégie buy & hold du PEA et envoie des alertes mail.

Une exécution par jour ouvré (vers 16h, heure de Paris) suffit :
  python pea_watch.py              -> calcule, écrit out/index.html, envoie les mails dus
  python pea_watch.py --dry-run    -> idem sans envoyer (affiche les mails dans la console)
  python pea_watch.py --force-monthly --dry-run   -> simule le mail d'achat du mois
  python pea_watch.py --test-mail  -> envoie un mail de test

Mail (variables d'environnement) : SMTP_USER, SMTP_PASS, MAIL_TO
  optionnel : SMTP_HOST (smtp.gmail.com), SMTP_PORT (465)
Positions : PEA_POSITIONS='{"DCAM.PA": 120, "CL2.PA": 15}' remplace les "qty" du config.
HIDE_AMOUNTS=1 masque les montants en euros sur la page (utile si elle est publique).

Aucune vente/achat n'est passé : l'outil applique des règles fixées à l'avance
et te dit quoi faire ; c'est toi qui passes les ordres sur BoursoBank.
"""
import argparse
import datetime as dt
import html
import json
import math
import os
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

HERE = Path(__file__).resolve().parent
PARIS = ZoneInfo("Europe/Paris")


# ---------------------------------------------------------------- données

def closes(ticker, period):
    s = yf.Ticker(ticker).history(period=period, auto_adjust=False)["Close"].dropna()
    if s.empty:
        raise RuntimeError(f"Pas de données Yahoo pour {ticker}")
    s.index = s.index.tz_convert(PARIS).date
    return s


def dip_multiplier(dd, levels):
    """Multiplicateur d'apport pour une baisse dd (négative) depuis le plus haut."""
    m = 1
    for lv in sorted(levels, key=lambda x: x["drawdown"], reverse=True):
        if dd <= lv["drawdown"]:
            m = lv["multiplier"]
    return m


def contribution_date(year, month, day):
    """Premier jour ouvré (lun-ven) à partir du jour d'apport du mois."""
    d = dt.date(year, month, min(day, 28))
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def next_contribution(today, day):
    d = contribution_date(today.year, today.month, day)
    if d < today:
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        d = contribution_date(y, m, day)
    return d


def limit_price(price, margin):
    p = price * (1 + margin)
    return round(p, 3 if p < 10 else 2)


def buy_plan(assets, amount):
    """Répartit l'argent neuf vers les lignes sous leur cible (rééquilibrage sans vente)."""
    total_after = sum(a["value"] for a in assets) + amount
    gaps = {a["ticker"]: max(0.0, a["target"] * total_after - a["value"]) for a in assets}
    gap_sum = sum(gaps.values()) or 1.0
    plan = []
    for a in assets:
        euros = amount * gaps[a["ticker"]] / gap_sum
        qty = math.floor(euros / a["limit"]) if a["limit"] > 0 else 0
        if qty > 0:
            plan.append({**a, "buy_qty": qty, "buy_eur": qty * a["limit"]})
    return plan


def rebalance_plan(assets):
    """Ordres pour revenir pile sur les cibles (ventes comprises, sans impôt dans le PEA)."""
    total = sum(a["value"] for a in assets)
    plan = []
    for a in assets:
        diff = a["target"] * total - a["value"]
        qty = int(diff / a["price"]) if a["price"] > 0 else 0
        if qty:
            plan.append({**a, "trade_qty": qty, "trade_eur": qty * a["price"]})
    return plan


# ---------------------------------------------------------------- analyse

def analyse(cfg, today, force_monthly=False):
    sig = closes(cfg["signal_ticker"], "max")
    ath = float(sig.max())
    ath_date = sig.idxmax()
    last, prev = float(sig.iloc[-1]), float(sig.iloc[-2])
    dd = last / ath - 1
    dd_prev = prev / float(sig.iloc[:-1].max()) - 1
    levels = cfg["dip_levels"]
    mult, mult_prev = dip_multiplier(dd, levels), dip_multiplier(dd_prev, levels)
    # Yahoo publie parfois la clôture du jour avec retard : on accepte la veille ouvrée.
    # Chaque franchissement n'est donc signalé qu'une fois (lancement 1×/jour ouvré).
    prev_bday = today - dt.timedelta(days=3 if today.weekday() == 0 else 1)
    fresh = sig.index[-1] >= prev_bday

    positions = json.loads(os.environ["PEA_POSITIONS"]) if os.environ.get("PEA_POSITIONS") else {}
    assets = []
    for a in cfg["assets"]:
        s = closes(a["ticker"], "1mo")
        qty = float(positions.get(a["ticker"], a.get("qty", 0)))
        price, price_prev = float(s.iloc[-1]), float(s.iloc[-2])
        assets.append({
            "ticker": a["ticker"], "name": a["name"], "target": a["target"], "qty": qty,
            "price": price, "price_prev": price_prev, "value": qty * price,
            "value_prev": qty * price_prev, "limit": limit_price(price, cfg["limit_margin"]),
            "asof": s.index[-1],
        })
    total = sum(a["value"] for a in assets)
    total_prev = sum(a["value_prev"] for a in assets)
    band = cfg["band"]
    for a in assets:
        a["weight"] = a["value"] / total if total else 0.0
        w_prev = a["value_prev"] / total_prev if total_prev else 0.0
        a["out"] = total > 0 and abs(a["weight"] - a["target"]) > band
        a["out_prev"] = total_prev > 0 and abs(w_prev - a["target"]) > band

    base = cfg["monthly_contribution"]
    amount = base * mult + cfg.get("cash_in_pea", 0)
    contrib_day = next_contribution(today, cfg["contribution_day"])
    is_contrib_day = force_monthly or contrib_day == today

    return {
        "today": today, "fresh": fresh, "signal": sig, "ath": ath, "ath_date": ath_date,
        "last": last, "dd": dd, "dd_prev": dd_prev, "mult": mult, "mult_prev": mult_prev,
        "assets": assets, "total": total, "base": base, "amount": amount,
        "contrib_day": contrib_day, "is_contrib_day": is_contrib_day,
        "plan": buy_plan(assets, amount),
        "rebalance": rebalance_plan(assets) if any(a["out"] for a in assets) else [],
        "january": today.month == 1,
    }


# ---------------------------------------------------------------- mails

def eur(x, hide=False):
    return "••• €" if hide else f"{x:,.2f} €".replace(",", " ").replace(".", ",")


def pct(x):
    return f"{x * 100:+.1f} %".replace(".", ",")


def plan_lines(r):
    lines = [f"- {p['buy_qty']} × {p['ticker']} ({p['name']}) — ordre à cours limité {p['limit']} € "
             f"≈ {eur(p['buy_eur'])}" for p in r["plan"]]
    return "\n".join(lines) or "- (montant trop faible pour acheter une part)"


def rebalance_lines(r):
    return "\n".join(
        f"- {'ACHETER' if p['trade_qty'] > 0 else 'VENDRE'} {abs(p['trade_qty'])} × {p['ticker']} "
        f"≈ {eur(abs(p['trade_eur']))} (poids {p['weight'] * 100:.0f} % → cible {p['target'] * 100:.0f} %)"
        for p in r["rebalance"])


def due_mails(r):
    mails = []
    window = "Passe les ordres entre 15h30 et 17h30 (Bourse US ouverte, écarts serrés), jamais au marché."
    dd_txt = f"Baisse du MSCI World depuis son plus haut : {pct(r['dd'])} (plus haut {r['ath']:.2f} € le {r['ath_date']:%d/%m/%Y})."

    if r["is_contrib_day"]:
        extra = f" (apport de base {eur(r['base'])} × {r['mult']} car le marché est en baisse)" if r["mult"] > 1 else ""
        body = (f"C'est le jour de l'apport mensuel.\n\n{dd_txt}\n"
                f"Montant à investir : {eur(r['amount'])}{extra}.\n\n"
                f"Ordres à passer :\n{plan_lines(r)}\n\n{window}\n"
                "Ensuite, mets à jour tes quantités (config.json ou PEA_POSITIONS).")
        if r["january"] and r["rebalance"]:
            body += f"\n\nRééquilibrage annuel (janvier) :\n{rebalance_lines(r)}"
        mails.append((f"PEA : achat du mois — {eur(r['amount'])}", body))

    if r["fresh"] and r["mult"] > r["mult_prev"]:
        body = (f"Nouveau palier de baisse franchi aujourd'hui.\n\n{dd_txt}\n\n"
                f"Règle : l'apport mensuel passe à × {r['mult']} ({eur(r['base'] * r['mult'])}).\n"
                "Si tu as de l'épargne prévue pour les krachs (Livret A, LDDS), c'est le moment "
                "de la verser sur le PEA puis d'acheter. Exemple pour "
                f"{eur(r['base'] * r['mult'])} :\n{plan_lines({**r, 'plan': buy_plan(r['assets'], r['base'] * r['mult'])})}\n\n"
                f"{window}\nNe vends rien.")
        mails.append((f"PEA : krach {pct(r['dd'])} — apport × {r['mult']}", body))

    newly_out = [a for a in r["assets"] if a["out"] and not a["out_prev"]]
    if r["fresh"] and newly_out:
        body = ("Une ligne est sortie de sa fourchette cible "
                f"(± {r['band'] * 100:.0f} points) :\n{rebalance_lines(r)}\n\n"
                "Option douce : ne rien vendre et orienter les prochains apports vers la ligne en retard.\n"
                f"{window}")
        mails.append(("PEA : rééquilibrage à faire", body))
    return mails


def send(subject, body):
    user, pwd, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"), os.environ.get("MAIL_TO")
    if not (user and pwd and to):
        raise RuntimeError("SMTP_USER, SMTP_PASS et MAIL_TO doivent être définis pour envoyer un mail")
    msg = MIMEText(body + "\n\n— PEA Watch (règles automatiques, pas un conseil financier)", "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    host, port = os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port) as s:
        s.login(user, pwd)
        s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())


# ---------------------------------------------------------------- page

def sparkline(sig, ath, levels, w=640, h=180):
    s = sig[sig.index >= sig.index[-1] - dt.timedelta(days=730)]
    lo = min(float(s.min()), ath * (1 + min(l["drawdown"] for l in levels)))
    hi = ath
    def y(v):
        return h - 6 - (v - lo) / (hi - lo) * (h - 12)
    n = len(s)
    pts = " ".join(f"{i / (n - 1) * w:.1f},{y(float(v)):.1f}" for i, v in enumerate(s))
    guides = "".join(
        f'<line x1="0" x2="{w}" y1="{y(ath * (1 + l["drawdown"])):.1f}" y2="{y(ath * (1 + l["drawdown"])):.1f}" class="g"/>'
        f'<text x="4" y="{y(ath * (1 + l["drawdown"])) - 3:.1f}" class="gl">{l["drawdown"] * 100:.0f} % → ×{l["multiplier"]}</text>'
        for l in levels)
    return (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" aria-label="Cours sur 2 ans">'
            f'<line x1="0" x2="{w}" y1="{y(ath):.1f}" y2="{y(ath):.1f}" class="ath"/>{guides}'
            f'<polyline points="{pts}" class="line"/></svg>')


def render(r, cfg, hide):
    st = "ok" if r["mult"] == 1 else "warn" if r["mult"] == 2 else "bad"
    verdict = ("Marché proche de son plus haut : apport normal." if r["mult"] == 1
               else f"Marché en baisse : apport × {r['mult']} ce mois-ci.")
    rows = "".join(
        f"<tr><td><b>{html.escape(a['ticker'])}</b><br><small>{html.escape(a['name'])}</small></td>"
        f"<td>{a['price']:.3f} €</td><td>{'•••' if hide else format(a['qty'], 'g')}</td>"
        f"<td>{eur(a['value'], hide)}</td>"
        f"<td class='{'bad' if a['out'] else ''}'>{a['weight'] * 100:.1f} %</td>"
        f"<td>{a['target'] * 100:.0f} %</td></tr>" for a in r["assets"])
    plan = "".join(
        f"<li><b>{p['buy_qty'] if not hide else '•••'} × {html.escape(p['ticker'])}</b> à cours limité {p['limit']} €"
        f" <small>≈ {eur(p['buy_eur'], hide)}</small></li>" for p in r["plan"]) or "<li>Montant trop faible.</li>"
    reb = ""
    if r["rebalance"]:
        items = "".join(
            f"<li>{'Acheter' if p['trade_qty'] > 0 else 'Vendre'} {'•••' if hide else abs(p['trade_qty'])} × "
            f"{html.escape(p['ticker'])}</li>" for p in r["rebalance"])
        reb = f'<section class="card bad-b"><h2>Rééquilibrage</h2><p>Une ligne sort de sa fourchette (± {cfg["band"] * 100:.0f} pts).</p><ul>{items}</ul></section>'
    stale = "" if r["fresh"] else '<p class="muted">Bourse fermée aujourd\'hui : dernières cotations affichées.</p>'
    gen = dt.datetime.now(PARIS).strftime("%d/%m/%Y %H:%M")
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>PEA Watch</title>
<style>
:root{{--bg:#f6f5f2;--card:#fff;--fg:#1d1d1b;--muted:#6b6a66;--line:#e4e2dc;--ok:#2f7d4f;--warn:#b7791f;--bad:#c0392b;--acc:#2b5fb4}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--card:#1f1f1d;--fg:#ecebe7;--muted:#9b9a95;--line:#33322f;--ok:#5cbf83;--warn:#e0a84a;--bad:#ec6f61;--acc:#7aa7ef}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:720px;margin:0 auto;padding:20px 16px 40px}}h1{{font-size:20px;margin:0 0 4px}}h2{{font-size:15px;margin:0 0 8px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:14px 0}}
.big{{font-size:40px;font-weight:700;letter-spacing:-1px}}.ok{{color:var(--ok)}}.warn{{color:var(--warn)}}.bad{{color:var(--bad)}}
.bad-b{{border-color:var(--bad)}}.muted,small{{color:var(--muted)}}
svg{{width:100%;height:180px;display:block}}.line{{fill:none;stroke:var(--acc);stroke-width:1.6}}
.ath{{stroke:var(--fg);stroke-dasharray:4 4;opacity:.5}}.g{{stroke:var(--bad);stroke-dasharray:2 4;opacity:.5}}.gl{{fill:var(--muted);font-size:10px}}
table{{width:100%;border-collapse:collapse;font-size:14px}}td,th{{padding:6px 4px;border-bottom:1px solid var(--line);text-align:right}}
td:first-child,th:first-child{{text-align:left}}th{{color:var(--muted);font-weight:500}}ul{{margin:0;padding-left:18px}}
.wrap{{overflow-x:auto}}
</style></head><body><main>
<h1>PEA Watch</h1><p class="muted">Mis à jour le {gen} · signal : {html.escape(cfg['signal_name'])}</p>{stale}
<section class="card"><h2>Baisse depuis le plus haut</h2>
<div class="big {st}">{pct(r['dd'])}</div>
<p><b>{verdict}</b><br><small>Plus haut {r['ath']:.2f} € le {r['ath_date']:%d/%m/%Y} · dernier {r['last']:.2f} €</small></p>
{sparkline(r['signal'], r['ath'], cfg['dip_levels'])}<small>2 ans · pointillés : paliers d'apport renforcé</small></section>
<section class="card"><h2>Prochain apport : {r['contrib_day']:%d/%m/%Y} — {eur(r['amount'], hide)}</h2>
<ul>{plan}</ul><p><small>Ordres à cours limité entre 15h30 et 17h30. Pas de vente pendant une baisse.</small></p></section>
{reb}
<section class="card"><h2>Portefeuille — {eur(r['total'], hide)}</h2><div class="wrap"><table>
<tr><th>Ligne</th><th>Cours</th><th>Parts</th><th>Valeur</th><th>Poids</th><th>Cible</th></tr>{rows}</table></div></section>
<p class="muted"><small>Règles automatiques définies à l'avance, pas un conseil en investissement. Données Yahoo Finance, possiblement retardées.</small></p>
</main></body></html>"""


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=HERE / "config.json")
    ap.add_argument("--out", default=HERE / "out" / "index.html")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-monthly", action="store_true")
    ap.add_argument("--test-mail", action="store_true")
    args = ap.parse_args()

    if args.test_mail:
        send("PEA Watch : mail de test", "Si tu lis ceci, l'envoi des alertes fonctionne.")
        print("Mail de test envoyé.")
        return

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    today = dt.datetime.now(PARIS).date()
    r = analyse(cfg, today, args.force_monthly)
    r["band"] = cfg["band"]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(r, cfg, os.environ.get("HIDE_AMOUNTS") == "1"), encoding="utf-8")
    print(f"Baisse {pct(r['dd'])} · multiplicateur ×{r['mult']} · page : {out}")

    for subject, body in due_mails(r):
        if args.dry_run:
            print(f"\n=== MAIL (non envoyé) : {subject}\n{body}")
        else:
            send(subject, body)
            print(f"Mail envoyé : {subject}")


if __name__ == "__main__":
    sys.exit(main())
