import json
import logging
import os
import uuid
from dataclasses import asdict
from datetime import datetime

from flask import (Flask, flash, jsonify, redirect, render_template,
                   request, send_from_directory, url_for)
from werkzeug.utils import secure_filename

from config import Config
import parsers
import core
import emailer
import payments
import webhooks
from parsers import Facture

app = Flask(__name__)
app.config.from_object(Config)
app.secret_key = Config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = Config.MAX_ATTACH_MB * 1024 * 1024

os.makedirs(Config.LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(Config.LOG_FILE, encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("relances")

for d in (Config.UPLOAD_DIR, Config.JOBS_DIR, Config.FACTURES_DIR, Config.DATA_DIR):
    os.makedirs(d, exist_ok=True)

TEMPLATES_FILE = os.path.join(Config.DATA_DIR, "templates_messages.json")


# ═════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════

def _hydrate(job_relances, refresh_contacts=True):
    """
    Recharge les relances depuis job.json.
    
    NOUVELLE LOGIQUE :
    - factures_filtrees = TOUTES les factures (on ne filtre plus par période)
    - montant_filtre = solde_du (TOUTES les dettes)
    - La décision de relancer est calculée par core.doit_relancer()
    """
    contacts = core.charger_contacts(Config.CONTACTS_FILE) if refresh_contacts else {}
    hist = core.charger_historique(Config.HISTORY_FILE)

    relances = []
    for r in job_relances:
        d = {k: v for k, v in r.items() if k in core.Relance.__dataclass_fields__}

        # ─── Factures : toutes, sans filtrage ───
        factures = [Facture(**f) for f in r.get("factures", [])]
        d["factures"] = factures
        d["factures_filtrees"] = list(factures)   # ⭐ TOUTES les factures

        # ─── Avoirs : recalcul forcé si absent ───
        avoirs_raw = r.get("avoirs")
        if not avoirs_raw:
            avoirs = [f for f in factures if f.solde_du < -0.005]
        else:
            avoirs = [Facture(**f) for f in avoirs_raw]
        d["avoirs"] = avoirs

        # ─── Recalcul totaux ───
        total_avoirs = round(sum(abs(f.solde_du) for f in avoirs), 2)
        d["total_avoirs"] = total_avoirs

        dettes_reelles = round(sum(f.solde_du for f in factures if f.solde_du > 0.005), 2)
        if dettes_reelles > 0:
            d["solde_du"] = dettes_reelles

        d["solde_net"] = round(d.get("solde_du", 0) - total_avoirs, 2)

        # ⭐ Montant à relancer = TOUTES les dettes
        d["montant_filtre"] = d["solde_du"]

        # ─── Refresh contacts.csv (fréquence, template, etc.) ───
        if refresh_contacts:
            code = r["code"]
            if code in contacts:
                c = contacts[code]
                freq = (c.get("frequence") or "").strip().lower()
                if freq not in core.FREQUENCES:
                    freq = ""
                d["frequence"] = freq
                d["frequence_label"] = core.FREQUENCES[freq]["label"]
                d["template_id"] = c.get("template_id", "") or ""
                d["est_nouveau"] = False
                if c.get("nom"):       d["nom"] = c["nom"]
                if c.get("email"):     d["email"] = c["email"]
                if c.get("telephone"): d["telephone"] = c["telephone"]
                d["remarque"] = c.get("remarque", "")
            else:
                d["est_nouveau"] = True
                d["frequence"] = ""
                d["frequence_label"] = core.FREQUENCES[""]["label"]

        rel = core.Relance(**d)

        # ⭐ Calcul du planning + décision
        core.calculer_planning(rel, hist)
        rel.doit_relancer = core.doit_relancer(rel, hist)

        # Motif lisible
        if rel.doit_relancer:
            delai = core.FREQUENCES[rel.frequence]["delai"]
            rel.motif_relance = f"Délai {delai}j écoulé depuis la 1ère facture impayée"
        elif not rel.frequence:
            rel.motif_relance = "Pas de fréquence définie"
        else:
            rel.motif_relance = f"Dans {rel.jours_restants} jour(s)"

        relances.append(rel)

    return relances


def _charger_job(job_id):
    p = os.path.join(Config.JOBS_DIR, job_id, "job.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _dernier_job():
    if not os.path.isdir(Config.JOBS_DIR):
        return None
    jobs = []
    for jid in os.listdir(Config.JOBS_DIR):
        p = os.path.join(Config.JOBS_DIR, jid, "job.json")
        if os.path.exists(p):
            jobs.append((os.path.getmtime(p), jid))
    if not jobs:
        return None
    jobs.sort(reverse=True)
    return _charger_job(jobs[0][1])


def _job_courant():
    session = core.charger_session_active(Config)
    if session:
        return _charger_job(session["job_id"])
    return _dernier_job()


def _list_joints(code):
    d = os.path.join(Config.FACTURES_DIR, code)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.lower().endswith(".pdf"))


def _nouveaux_codes(relances):
    contacts = core.charger_contacts(Config.CONTACTS_FILE)
    return core.detecter_nouveaux_contacts([r.code for r in relances], contacts)


def _relances_avec_planning(relances):
    hist = core.charger_historique(Config.HISTORY_FILE)
    for r in relances:
        core.calculer_planning(r, hist)
    return relances


# ═════════════════════════════════════════════════════════════════════
# DASHBOARD
# ═════════════════════════════════════════════════════════════════════
@app.route("/")
def dashboard():
    hist = core.charger_historique(Config.HISTORY_FILE)
    session = core.charger_session_active(Config)
    job = _job_courant()

    relances = _hydrate(job["relances"]) if job else []
    relances = _relances_avec_planning(relances)

    nouveaux = _nouveaux_codes(relances)
    stats = core.stats_globales(relances, hist)
    recap_count = len(core.clients_pour_recap(relances))

    # ─── Stats avoirs ───
    nb_clients_avec_avoirs = sum(1 for r in relances if r.avoirs)
    total_avoirs_global = sum(r.total_avoirs for r in relances)

    par_niveau = {}
    for r in relances:
        par_niveau.setdefault(r.niveau_label, {"count": 0, "montant": 0.0})
        par_niveau[r.niveau_label]["count"] += 1
        par_niveau[r.niveau_label]["montant"] += r.montant_filtre

    par_niveau_json = [
        {"label": k, "count": v["count"], "montant": round(v["montant"], 2)}
        for k, v in par_niveau.items()
    ]
    labels7, values7 = core.series_7j(hist)

    est_vendredi = datetime.now().weekday() == 4
    jours_avant_vendredi = (4 - datetime.now().weekday()) % 7

    return render_template("dashboard.html", cfg=Config, stats=stats,
                           par_niveau=par_niveau, dernier=job,
                           relances=relances[:15], hist=hist,
                           par_niveau_json=par_niveau_json,
                           days_json=labels7, values_json=values7,
                           session=session, nouveaux=nouveaux,
                           recap_count=recap_count,
                           nb_clients_avec_avoirs=nb_clients_avec_avoirs,
                           total_avoirs_global=total_avoirs_global,
                           est_vendredi=est_vendredi,
                           jours_avant_vendredi=jours_avant_vendredi,
                           stripe_actif=payments.stripe_active(Config))


# ═════════════════════════════════════════════════════════════════════
# DEBUG — pour voir ce qui est détecté
# ═════════════════════════════════════════════════════════════════════
@app.route("/debug")
def debug():
    """Page de debug : montre tout ce qui est détecté dans le job courant."""
    job = _job_courant()
    if not job:
        flash("Aucun job.", "error")
        return redirect(url_for("dashboard"))
    relances = _hydrate(job["relances"])
    return render_template("debug.html", cfg=Config, job=job, relances=relances)


@app.route("/api/debug/job")
def api_debug_job():
    """Retourne le contenu du job courant en JSON (pour inspection)."""
    job = _job_courant()
    if not job:
        return jsonify({"ok": False, "error": "no job"}), 404
    return jsonify({
        "ok": True,
        "job_id": job.get("id"),
        "nb_relances": len(job.get("relances", [])),
        "premier_client": job.get("relances", [{}])[0] if job.get("relances") else {},
    })


@app.route("/api/debug/avoirs")
def api_debug_avoirs():
    """Liste tous les clients avec leurs avoirs détectés."""
    job = _job_courant()
    if not job:
        return jsonify({"ok": False}), 404
    relances = _hydrate(job["relances"])
    return jsonify({
        "ok": True,
        "clients_avec_avoirs": [
            {
                "code": r.code,
                "nom": r.nom,
                "nb_avoirs": len(r.avoirs),
                "total_avoirs": r.total_avoirs,
                "solde_du": r.solde_du,
                "solde_net": r.solde_net,
                "avoirs_detail": [
                    {"numero": a.numero, "date": a.date,
                     "montant": a.solde_du, "ttc": a.ttc}
                    for a in r.avoirs
                ],
            }
            for r in relances if r.avoirs
        ],
    })


# ═════════════════════════════════════════════════════════════════════
# ANALYSE
# ═════════════════════════════════════════════════════════════════════
@app.route("/analyser", methods=["POST"])
def analyser():
    f_rel = request.files.get("releve")
    f_bal = request.files.get("balance")
    if not f_rel or not f_bal:
        flash("Merci de fournir les deux PDF.", "error")
        return redirect(url_for("index"))

    job_id = uuid.uuid4().hex[:12]
    job_dir = os.path.join(Config.JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    p_rel = os.path.join(job_dir, "releve.pdf")
    p_bal = os.path.join(job_dir, "balance.pdf")
    f_rel.save(p_rel)
    f_bal.save(p_bal)

    try:
        releves = parsers.parse_releve(p_rel)
        balances = parsers.parse_balance_agee(p_bal)
    except Exception as e:
        log.exception("Parsing")
        flash(f"Erreur de lecture PDF : {e}", "error")
        return redirect(url_for("index"))

    contacts = core.charger_contacts(Config.CONTACTS_FILE)
    seuils = core.seuils_from_config(Config)
    hist = core.charger_historique(Config.HISTORY_FILE)
    relances = core.croiser(releves, balances, contacts, seuils, hist)

    # ─── Stats au moment de l'analyse ───
    nb_avoirs = sum(1 for r in relances if r.avoirs)
    total_avoirs = sum(r.total_avoirs for r in relances)
    log.info("Analyse : %d clients · %d avec avoirs · total avoirs %.2f €",
             len(relances), nb_avoirs, total_avoirs)

    job = {"id": job_id,
           "created_at": datetime.now().isoformat(timespec="seconds"),
           "relances": [asdict(r) for r in relances]}
    with open(os.path.join(job_dir, "job.json"), "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2, default=str)

    core.sauver_session_active(Config, {
        "job_id": job_id,
        "demarre_le": datetime.now().isoformat(timespec="seconds"),
        "nb_relances": len(relances),
        "total_du": round(sum(r.montant_filtre for r in relances), 2),
        "nb_avoirs": nb_avoirs,
        "total_avoirs": total_avoirs,
    })

    nouveaux = core.detecter_nouveaux_contacts([r.code for r in relances], contacts)
    msg = f"✅ Session démarrée : {len(relances)} clients"
    if nb_avoirs:
        msg += f" · {nb_avoirs} avec avoirs"
    if nouveaux:
        msg += f" · 🆕 {len(nouveaux)} nouveau(x)"
    flash(msg, "ok")

    if Config.AUTO_SEND:
        return _envoyer(job_id)
    return redirect(url_for("preview", job_id=job_id))


# ═════════════════════════════════════════════════════════════════════
# PREVIEW
# ═════════════════════════════════════════════════════════════════════
@app.route("/preview/<job_id>")
def preview(job_id):
    job = _charger_job(job_id)
    if not job:
        flash("Job introuvable.", "error")
        return redirect(url_for("dashboard"))
    relances = _hydrate(job["relances"])
    relances = _relances_avec_planning(relances)
    nouveaux = _nouveaux_codes(relances)

    lien_global = request.args.get("lien", "0") == "1"
    templates = core.charger_templates(TEMPLATES_FILE)

    liens = {}
    if payments.stripe_active(Config):
        for r in relances:
            if payments.doit_avoir_lien(r, Config, forcer_global=lien_global,
                                        forcer_client=r.forcer_lien):
                liens[r.code] = payments.creer_lien_paiement(r, Config)

    apercus = {}
    for r in relances:
        tpl, _ = emailer._template_pour(r, templates, "relance")
        sujet, corps = emailer.rendre_message(
            r, Config, tpl, type_mail="relance",
            avec_lien=(r.code in liens),
            forcer_global=lien_global,
            forcer_client=r.forcer_lien,
        )
        apercus[r.code] = {"sujet": sujet, "corps": corps}

    joints = {r.code: _list_joints(r.code) for r in relances}
    hist = core.charger_historique(Config.HISTORY_FILE)

    return render_template("preview.html", job=job, relances=relances,
                           apercus=apercus, joints=joints, hist=hist,
                           liens=liens, lien_global=lien_global,
                           nouveaux=nouveaux, nouveaux_set=set(nouveaux),
                           frequences=core.FREQUENCES,
                           freq_colors=core.FREQUENCE_COLORS,
                           templates=templates,
                           stripe_actif=payments.stripe_active(Config),
                           cfg=Config)


# ═════════════════════════════════════════════════════════════════════
# ENVOI
# ═════════════════════════════════════════════════════════════════════
@app.route("/envoyer/<job_id>", methods=["POST"])
def envoyer(job_id):
    return _envoyer(job_id)


def _envoyer(job_id, type_mail="relance"):
    job = _charger_job(job_id)
    if not job:
        flash("Job introuvable.", "error")
        return redirect(url_for("dashboard"))

    selection = request.form.getlist("codes")
    lien_global = request.form.get("lien_global") == "1"
    liens_clients = set(request.form.getlist("lien_clients"))

    template_overrides = {}
    for key in request.form.keys():
        if key.startswith("tpl_"):
            code = key[4:]
            val = request.form.get(key, "").strip()
            if val:
                template_overrides[code] = val

    toutes = _hydrate(job["relances"])
    relances = [r for r in toutes if not selection or r.code in selection]
    for r in relances:
        r.forcer_lien = r.code in liens_clients

    resultats = emailer.envoyer_relances(
        relances, Config, Config.FACTURES_DIR,
        avec_lien_global=lien_global, type_mail=type_mail,
        templates_path=TEMPLATES_FILE,
        template_overrides=template_overrides,
    )

    for res in resultats:
        if res["statut"] in ("sent", "dry-run"):
            if type_mail == "recap_hebdo":
                core.marquer_recap_hebdo(Config.HISTORY_FILE, res["code"],
                                          res["montant"], res["email"])
            else:
                core.marquer_envoye(Config.HISTORY_FILE, res["code"],
                                    res.get("niveau", ""), res["montant"],
                                    res["email"], statut="envoye",
                                    type_mail="relance",
                                    template_id=res.get("template_id", ""))

    return render_template("result.html", job=job, resultats=resultats,
                           type_mail=type_mail, cfg=Config)


# ═════════════════════════════════════════════════════════════════════
# RÉCAP HEBDO — FIX AVOIRS
# ═════════════════════════════════════════════════════════════════════
@app.route("/recap-hebdo")
def recap_hebdo_preview():
    job = _job_courant()
    if not job:
        flash("Aucune analyse. Importez d'abord des PDF.", "error")
        return redirect(url_for("dashboard"))

    relances = _hydrate(job["relances"])
    relances = _relances_avec_planning(relances)
    relances = core.clients_pour_recap(relances)

    # ─── Stats avoirs ───
    nb_avoirs = sum(1 for r in relances if r.avoirs)
    total_avoirs = sum(r.total_avoirs for r in relances)

    templates = core.charger_templates(TEMPLATES_FILE)
    tpl_id = ""
    for tid, t in templates.items():
        if t.get("type") == "recap_hebdo":
            tpl_id = tid
            break

    apercus = {}
    for r in relances:
        t, _ = emailer._template_pour(r, templates, "recap_hebdo")
        sujet, corps = emailer.rendre_message(r, Config, t, type_mail="recap_hebdo")
        apercus[r.code] = {"sujet": sujet, "corps": corps}

    est_vendredi = datetime.now().weekday() == 4
    jours_avant_vendredi = (4 - datetime.now().weekday()) % 7

    return render_template("recap_hebdo.html",
                           cfg=Config, job=job,
                           relances=relances, apercus=apercus,
                           templates=templates, tpl_recap_id=tpl_id,
                           est_vendredi=est_vendredi,
                           jours_avant_vendredi=jours_avant_vendredi,
                           nb_avoirs=nb_avoirs, total_avoirs=total_avoirs)


@app.route("/recap-hebdo/envoyer", methods=["POST"])
def recap_hebdo_envoyer():
    job = _job_courant()
    if not job:
        flash("Aucune analyse.", "error")
        return redirect(url_for("dashboard"))

    selection = request.form.getlist("codes")
    tpl_id = request.form.get("template_id", "").strip()

    template_overrides = {}
    if tpl_id:
        for code in selection:
            template_overrides[code] = tpl_id

    toutes = _hydrate(job["relances"])
    relances = [r for r in toutes
                if (not selection or r.code in selection)
                and abs(r.solde_net) > 0.5]

    resultats = emailer.envoyer_relances(
        relances, Config, Config.FACTURES_DIR,
        avec_lien_global=False, type_mail="recap_hebdo",
        templates_path=TEMPLATES_FILE,
        template_overrides=template_overrides,
    )

    for res in resultats:
        if res["statut"] in ("sent", "dry-run"):
            core.marquer_recap_hebdo(Config.HISTORY_FILE, res["code"],
                                      res["montant"], res["email"])

    return render_template("result.html", job=job, resultats=resultats,
                           type_mail="recap_hebdo", cfg=Config)


# ═════════════════════════════════════════════════════════════════════
# TEST SMTP
# ═════════════════════════════════════════════════════════════════════
@app.route("/test-smtp", methods=["POST"])
def test_smtp():
    destinataire = request.form.get("email", "").strip()
    if not destinataire:
        flash("Merci de fournir une adresse email.", "error")
        return redirect(url_for("index"))
    ok, msg = emailer.tester_smtp(Config)
    if not ok:
        flash(msg, "error")
        return redirect(url_for("index"))
    ok, msg = emailer.envoyer_mail_test(Config, destinataire)
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("index"))


# ═════════════════════════════════════════════════════════════════════
# TEMPLATES
# ═════════════════════════════════════════════════════════════════════
@app.route("/templates")
def templates_liste():
    templates = core.charger_templates(TEMPLATES_FILE)
    return render_template("templates_messages.html",
                           cfg=Config, templates=templates,
                           vars_dispo=_variables_dispo())


@app.route("/templates/nouveau", methods=["POST"])
def templates_nouveau():
    nom = request.form.get("nom", "").strip() or "Nouveau template"
    type_mail = request.form.get("type", "relance").strip()
    niveau = request.form.get("niveau", "").strip()
    sujet = request.form.get("sujet", "").strip()
    corps = request.form.get("corps", "").strip()
    tid = core.creer_template(TEMPLATES_FILE, nom, type_mail, niveau, sujet, corps)
    flash(f"✅ Template « {nom} » créé.", "ok")
    return redirect(url_for("templates_editer", tid=tid))


@app.route("/templates/<tid>")
def templates_editer(tid):
    templates = core.charger_templates(TEMPLATES_FILE)
    if tid not in templates:
        flash("Template introuvable.", "error")
        return redirect(url_for("templates_liste"))
    return render_template("template_edit.html",
                           cfg=Config, tid=tid, tpl=templates[tid],
                           vars_dispo=_variables_dispo(),
                           frequences=core.FREQUENCES)


@app.route("/templates/<tid>/enregistrer", methods=["POST"])
def templates_enregistrer(tid):
    ok = core.modifier_template(
        TEMPLATES_FILE, tid,
        nom=request.form.get("nom", ""),
        type=request.form.get("type", "relance"),
        niveau=request.form.get("niveau", ""),
        sujet=request.form.get("sujet", ""),
        corps=request.form.get("corps", ""),
    )
    flash("💾 Template enregistré." if ok else "Erreur.", "ok" if ok else "error")
    return redirect(url_for("templates_editer", tid=tid))


@app.route("/templates/<tid>/apercu", methods=["POST"])
def templates_apercu(tid):
    templates = core.charger_templates(TEMPLATES_FILE)
    if tid not in templates:
        return jsonify({"ok": False}), 404
    tpl = templates[tid]

    from parsers import Facture as F
    demo = core.Relance(
        code="9999", nom="Client Démo SARL",
        email="demo@example.com", telephone="01 23 45 67 89",
        solde_du=1250.50, total_avoirs=200.00, solde_net=1050.50,
        montant_filtre=856.30,
        niveau="rappel_1", niveau_label="Rappel amiable",
        frequence="hebdo", frequence_label="Chaque semaine",
        factures=[
            F(numero="640001", date="10/09/2026", echeance="25/09/2026",
              ht=400.0, tva=80.0, ttc=480.0, solde_du=480.0),
            F(numero="640002", date="12/09/2026", echeance="27/09/2026",
              ht=313.58, tva=62.72, ttc=376.30, solde_du=376.30),
        ],
        avoirs=[
            F(numero="AV001", date="15/09/2026", echeance="15/09/2026",
              ht=-166.67, tva=-33.33, ttc=-200.0, solde_du=-200.0),
        ],
    )
    demo.factures_filtrees = list(demo.factures)
    sujet, corps = emailer.rendre_message(
        demo, Config, tpl, type_mail=tpl.get("type", "relance"))
    return jsonify({"ok": True, "sujet": sujet, "corps": corps})


@app.route("/templates/<tid>/dupliquer", methods=["POST"])
def templates_dupliquer(tid):
    nouveau = core.dupliquer_template(TEMPLATES_FILE, tid)
    if nouveau:
        flash("📋 Template dupliqué.", "ok")
        return redirect(url_for("templates_editer", tid=nouveau))
    flash("Erreur.", "error")
    return redirect(url_for("templates_liste"))


@app.route("/templates/<tid>/supprimer", methods=["POST"])
def templates_supprimer(tid):
    ok = core.supprimer_template(TEMPLATES_FILE, tid)
    flash("🗑️ Supprimé." if ok else "Impossible.", "ok" if ok else "error")
    return redirect(url_for("templates_liste"))


def _variables_dispo():
    return [
        ("{{nom}}", "Nom du client"),
        ("{{code}}", "Code client"),
        ("{{email}}", "Email du client"),
        ("{{telephone}}", "Téléphone"),
        ("{{solde_du}}", "Total dettes"),
        ("{{solde_net}}", "Solde net (dettes - avoirs)"),
        ("{{total_avoirs}}", "Total des avoirs"),
        ("{{montant_filtre}}", "Montant filtré par fréquence"),
        ("{{montant_total}}", "Montant à afficher"),
        ("{{nb_factures}}", "Nombre de factures"),
        ("{{nb_avoirs}}", "Nombre d'avoirs"),
        ("{{avoirs_table}}", "Tableau des avoirs uniquement"),
        ("{{factures_table}}", "Tableau complet"),
        ("{{frequence_label}}", "Libellé de la fréquence"),
        ("{{periode_suffix}}", "Période en texte"),
        ("{{niveau_label}}", "Niveau de relance"),
        ("{{date}}", "Date du jour"),
        ("{{company_name}}", "Nom de la société"),
        ("{{company_address}}", "Adresse"),
        ("{{company_phone}}", "Téléphone"),
        ("{{company_email}}", "Email"),
        ("{{signature}}", "Bloc signature complet"),
    ]


# ═════════════════════════════════════════════════════════════════════
# PLANNING
# ═════════════════════════════════════════════════════════════════════
@app.route("/planning")
def planning():
    job = _job_courant()
    relances = _hydrate(job["relances"]) if job else []
    relances = _relances_avec_planning(relances)
    relances.sort(key=lambda r: (r.jours_restants, -r.solde_net))

    maintenant = [r for r in relances
                  if r.etat in ("now", "nouveau", "libre") and r.frequence]
    bientot = [r for r in relances if r.etat == "bientot"]
    a_jour = [r for r in relances if r.etat == "ok"]
    libres = [r for r in relances if r.etat == "libre"]
    nouveaux = [r for r in relances if r.etat == "nouveau"]

    stats = {
        "maintenant": len(maintenant),
        "bientot": len(bientot),
        "a_jour": len(a_jour),
        "libres": len(libres),
        "nouveaux": len(nouveaux),
        "montant_maintenant": sum(r.montant_filtre for r in maintenant),
        "montant_bientot": sum(r.montant_filtre for r in bientot),
    }

    return render_template("planning.html", cfg=Config,
                           maintenant=maintenant, bientot=bientot,
                           a_jour=a_jour, libres=libres,
                           nouveaux=nouveaux, stats=stats,
                           hist=core.charger_historique(Config.HISTORY_FILE))


# ═════════════════════════════════════════════════════════════════════
# FRÉQUENCE & TEMPLATE INLINE
# ═════════════════════════════════════════════════════════════════════
@app.route("/contact/<code>/frequence", methods=["POST"])
def update_frequence(code):
    if not request.is_json:
        return jsonify({"ok": False}), 400
    data = request.json or {}
    freq = (data.get("frequence") or "").strip().lower()
    if freq not in core.FREQUENCES:
        freq = ""

    contacts = core.charger_contacts(Config.CONTACTS_FILE)
    if code in contacts:
        nom = contacts[code].get("nom", "")
        email = contacts[code].get("email", "")
        tel = contacts[code].get("telephone", "")
        remarque = contacts[code].get("remarque", "")
        template_id = contacts[code].get("template_id", "")
    else:
        job = _job_courant()
        nom = email = tel = remarque = template_id = ""
        if job:
            for r in job["relances"]:
                if r["code"] == code:
                    nom = r.get("nom", "")
                    email = r.get("email", "")
                    tel = r.get("telephone", "")
                    remarque = r.get("remarque", "")
                    break

    core.mettre_a_jour_frequence(Config.CONTACTS_FILE, code, freq,
                                 nom, email, tel, remarque, template_id)
    log.info("✅ Fréquence : %s → '%s'", code, freq or "(vide)")
    return jsonify({"ok": True, "frequence": freq,
                    "label": core.FREQUENCES[freq]["label"]})


@app.route("/contact/<code>/template", methods=["POST"])
def update_template(code):
    if not request.is_json:
        return jsonify({"ok": False}), 400
    data = request.json or {}
    tid = (data.get("template_id") or "").strip()
    contacts = core.charger_contacts(Config.CONTACTS_FILE)
    if code in contacts:
        contacts[code]["template_id"] = tid
    else:
        contacts[code] = {"code": code, "nom": "", "email": "", "telephone": "",
                          "actif": "1", "remarque": "", "frequence": "",
                          "template_id": tid}
    core.sauver_contacts(Config.CONTACTS_FILE, list(contacts.values()))
    return jsonify({"ok": True, "template_id": tid})


@app.route("/api/contact/<code>")
def api_contact(code):
    contacts = core.charger_contacts(Config.CONTACTS_FILE)
    if code not in contacts:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "contact": contacts[code]})


# ═════════════════════════════════════════════════════════════════════
# FACTURES
# ═════════════════════════════════════════════════════════════════════
@app.route("/factures/<code>", methods=["POST"])
def upload_facture(code):
    dossier = os.path.join(Config.FACTURES_DIR, code)
    os.makedirs(dossier, exist_ok=True)
    n = 0
    for f in request.files.getlist("factures"):
        if not f or not f.filename:
            continue
        fname = secure_filename(f.filename)
        if not fname.lower().endswith(".pdf"):
            continue
        f.save(os.path.join(dossier, fname))
        n += 1
    return jsonify({"ok": True, "ajoutes": n, "liste": _list_joints(code)})


@app.route("/factures/<code>/<path:fname>", methods=["DELETE"])
def delete_facture(code, fname):
    p = os.path.join(Config.FACTURES_DIR, code, secure_filename(fname))
    if os.path.exists(p):
        os.remove(p)
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 404


@app.route("/factures/<code>/<path:fname>")
def view_facture(code, fname):
    return send_from_directory(os.path.join(Config.FACTURES_DIR, code), fname)


# ═════════════════════════════════════════════════════════════════════
# STATUT / KANBAN / STATS / CONTACTS / CLIENT
# ═════════════════════════════════════════════════════════════════════
@app.route("/statut/<code>", methods=["POST"])
def changer_statut(code):
    statut = ((request.json or {}).get("statut", "en_attente")
              if request.is_json else request.form.get("statut", "en_attente"))
    core.changer_statut(Config.HISTORY_FILE, code, statut)
    if request.is_json:
        return jsonify({"ok": True, "statut": statut})
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/kanban")
def kanban():
    job = _job_courant()
    relances = _hydrate(job["relances"]) if job else []
    relances = _relances_avec_planning(relances)
    hist = core.charger_historique(Config.HISTORY_FILE)
    colonnes = {"a_relancer": [], "envoye": [], "paye": [], "litige": []}
    for r in relances:
        statut = hist.get(r.code, {}).get("statut", "a_relancer")
        if statut == "en_attente":
            statut = "a_relancer"
        colonnes.setdefault(statut, []).append(r)
    sommes = {k: sum(x.solde_net for x in v) for k, v in colonnes.items()}
    couleurs = {"a_relancer": "#c08a1f", "envoye": "#1e5c3a",
                "paye": "#2a7a4a", "litige": "#b3453b"}
    return render_template("kanban.html", cfg=Config,
                           colonnes=colonnes, sommes=sommes, couleurs=couleurs)


@app.route("/stats")
def stats_page():
    hist = core.charger_historique(Config.HISTORY_FILE)
    tous = []
    if os.path.isdir(Config.JOBS_DIR):
        for jid in os.listdir(Config.JOBS_DIR):
            j = _charger_job(jid)
            if j:
                tous.extend(j["relances"])
    relances = _hydrate(tous)
    s = core.stats_globales(relances, hist)
    labels7, values7 = core.series_7j(hist)
    paiements = core.charger_paiements(Config.PAIEMENTS_FILE)
    return render_template("stats.html", cfg=Config, stats=s, hist=hist,
                           days_json=labels7, values_json=values7,
                           paiements=paiements)


@app.route("/contacts", methods=["GET", "POST"])
def contacts():
    if request.method == "POST":
        rows = []
        for code in request.form.getlist("code"):
            freq = request.form.get(f"freq_{code}", "").strip().lower()
            if freq not in core.FREQUENCES:
                freq = ""
            rows.append({
                "code": code,
                "nom": request.form.get(f"nom_{code}", ""),
                "email": request.form.get(f"email_{code}", ""),
                "telephone": request.form.get(f"tel_{code}", ""),
                "actif": "1",
                "remarque": request.form.get(f"remarque_{code}", ""),
                "frequence": freq,
                "template_id": request.form.get(f"tpl_{code}", ""),
            })
        core.sauver_contacts(Config.CONTACTS_FILE, rows)
        flash("Contacts enregistrés.", "ok")
        return redirect(url_for("contacts"))

    contacts = core.charger_contacts(Config.CONTACTS_FILE)
    job = _job_courant()
    connus_job = {}
    if job:
        for r in job["relances"]:
            connus_job[r["code"]] = r
    nouveaux_codes = core.detecter_nouveaux_contacts(
        list(connus_job.keys()), contacts)
    templates = core.charger_templates(TEMPLATES_FILE)
    return render_template("contacts.html", contacts=contacts,
                           nouveaux_codes=nouveaux_codes,
                           connus_job=connus_job,
                           frequences=core.FREQUENCES,
                           templates=templates, cfg=Config)


@app.route("/client/<code>")
def client(code):
    hist = core.charger_historique(Config.HISTORY_FILE)
    h = hist.get(code, {})
    nom = f"Client {code}"
    factures = []
    avoirs = []
    job = _job_courant()
    if job:
        for r in job["relances"]:
            if r["code"] == code:
                nom = r["nom"]
                factures = [Facture(**f) for f in r.get("factures", [])]
                avoirs = [f for f in factures if f.solde_du < -0.005]
                break
    contacts = core.charger_contacts(Config.CONTACTS_FILE)
    contact = contacts.get(code, {})
    frequence = contact.get("frequence", "")
    joints = _list_joints(code)
    evenements = []
    for e in h.get("envois", []):
        evenements.append({"date": e["date"], "type": "envoi",
                           "titre": f"📧 Relance {e['niveau']} → {e['email']}",
                           "montant": e["montant"]})
    for e in h.get("recaps", []):
        evenements.append({"date": e["date"], "type": "recap",
                           "titre": f"📋 Récap hebdo → {e['email']}",
                           "montant": e["montant"]})
    if h.get("paiement"):
        p = h["paiement"]
        evenements.append({"date": p["date"], "type": "paiement",
                           "titre": "💰 Paiement reçu", "montant": p["montant"]})
    evenements.sort(key=lambda x: x["date"], reverse=True)
    templates = core.charger_templates(TEMPLATES_FILE)
    return render_template("client.html", cfg=Config, code=code, nom=nom,
                           hist=h, evenements=evenements,
                           factures=factures, avoirs=avoirs, joints=joints,
                           frequence=frequence, frequences=core.FREQUENCES,
                           est_nouveau=(code not in contacts),
                           templates=templates, contact=contact,
                           stripe_actif=payments.stripe_active(Config))


# ═════════════════════════════════════════════════════════════════════
# NOUVELLE ANALYSE / SESSIONS
# ═════════════════════════════════════════════════════════════════════
@app.route("/nouvelle-analyse")
def index():
    session = core.charger_session_active(Config)
    smtp_ok, smtp_msg = (emailer.tester_smtp(Config)
                         if Config.SMTP_HOST else (False, "SMTP non configuré"))
    return render_template("index.html", cfg=Config, session=session,
                           smtp_ok=smtp_ok, smtp_msg=smtp_msg)


@app.route("/session/reprendre")
def session_reprendre():
    session = core.charger_session_active(Config)
    if not session:
        flash("Aucune session active.", "error")
        return redirect(url_for("dashboard"))
    return redirect(url_for("preview", job_id=session["job_id"]))


@app.route("/session/finaliser", methods=["POST"])
def session_finaliser():
    session = core.charger_session_active(Config)
    if not session:
        flash("Aucune session active.", "error")
        return redirect(url_for("dashboard"))
    job = _charger_job(session["job_id"])
    relances = _hydrate(job["relances"]) if job else []
    hist = core.charger_historique(Config.HISTORY_FILE)
    stats = core.stats_globales(relances, hist)
    note = request.form.get("note", "").strip()
    core.finaliser_session(Config, session["job_id"], stats, note)
    flash("🎉 Session finalisée et archivée.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/session/annuler", methods=["POST"])
def session_annuler():
    core.fermer_session_active(Config)
    flash("Session annulée.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/sessions")
def sessions_archivees():
    archives = core.lister_sessions_archivees(Config)
    return render_template("sessions.html", cfg=Config, archives=archives)


# ═════════════════════════════════════════════════════════════════════
# PAIEMENTS / WEBHOOK / HEALTH
# ═════════════════════════════════════════════════════════════════════
@app.route("/merci")
def merci():
    code = request.args.get("code", "")
    if code:
        core.changer_statut(Config.HISTORY_FILE, code, "paye")
    return render_template("merci.html", code=code, cfg=Config)


@app.route("/annule")
def annule():
    code = request.args.get("code", "")
    flash("Paiement annulé.", "error")
    return redirect(url_for("client", code=code) if code else url_for("dashboard"))


@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    signature = request.headers.get("Stripe-Signature", "")
    if not Config.STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "webhook secret missing"}), 500
    event = webhooks.verifier_signature(
        payload.encode("utf-8"), signature, Config.STRIPE_WEBHOOK_SECRET)
    if not event:
        return jsonify({"error": "invalid signature"}), 400
    try:
        ok, message = webhooks.traiter_evenement(event, Config)
        return jsonify({"received": True, "message": message}), 200
    except Exception as e:
        log.exception("Erreur webhook")
        return jsonify({"error": str(e)}), 500


@app.route("/healthz")
def healthz():
    session = core.charger_session_active(Config)
    job = _job_courant()
    relances = _hydrate(job["relances"]) if job else []
    relances = _relances_avec_planning(relances)
    nouveaux = _nouveaux_codes(relances)
    smtp_ok, smtp_msg = (emailer.tester_smtp(Config)
                         if Config.SMTP_HOST else (False, "SMTP non configuré"))
    contacts = core.charger_contacts(Config.CONTACTS_FILE)
    aujourd_hui = datetime.now().weekday()
    return jsonify({
        "status": "ok",
        "dry_run": Config.DRY_RUN,
        "auto_send": Config.AUTO_SEND,
        "smtp_ok": smtp_ok,
        "smtp_message": smtp_msg,
        "session_active": bool(session),
        "nouveaux_contacts": len(nouveaux),
        "contacts_enregistres": len(contacts),
        "clients_avec_avoirs": sum(1 for r in relances if r.avoirs),
        "jour": ["lundi","mardi","mercredi","jeudi","vendredi","samedi","dimanche"][aujourd_hui],
        "est_vendredi": aujourd_hui == 4,
    })


@app.errorhandler(404)
def page_not_found(e):
    return render_template("404.html", cfg=Config), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)