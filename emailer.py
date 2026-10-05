"""Génération et envoi des emails — templates, avoirs, mail de test."""
import csv
import logging
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from typing import List

from core import Relance, charger_templates
from payments import creer_lien_paiement, doit_avoir_lien

log = logging.getLogger("relances")

TYPE_RELANCE = "relance"
TYPE_RECAP   = "recap_hebdo"

PERIODES = {
    "hebdo": "de la semaine passée",
    "quinzaine": "des 2 dernières semaines",
    "mensuel": "du mois passé",
    "": "",
}


def _fmt_money(v):
    return f"{v:,.2f}".replace(",", " ").replace(".", ",") + " €"


def _table_factures_txt(factures, montant_total, titre_total="TOTAL À RÉGLER"):
    """Tableau texte des factures. Gère les avoirs (négatifs)."""
    L = []
    L.append("Détail des factures :")
    L.append(f"{'N° facture':<12}{'Date':<12}{'Échéance':<12}"
             f"{'Montant TTC':>16}{'Solde dû':>16}")
    L.append("-" * 68)
    for f in factures:
        ligne = (f"{f.numero:<12}{f.date:<12}{f.echeance:<12}"
                 f"{_fmt_money(f.ttc):>16}{_fmt_money(f.solde_du):>16}")
        # Marqueur visuel pour les avoirs
        if f.solde_du < -0.005:
            ligne = "AVOIR " + ligne[6:]
        L.append(ligne)
    L.append("-" * 68)
    L.append(f"{titre_total:<30}{_fmt_money(montant_total):>38}")
    return "\n".join(L)


def _table_recap_avec_net(rel, cfg):
    """Tableau récap complet : factures + avoirs + solde net."""
    L = []

    # Dettes (factures positives)
    dettes = [f for f in rel.factures if f.solde_du > 0.005]
    if dettes:
        L.append("FACTURES À RÉGLER :")
        L.append(f"{'N° facture':<12}{'Date':<12}{'Échéance':<12}"
                 f"{'Montant TTC':>16}{'Solde dû':>16}")
        L.append("-" * 68)
        for f in dettes:
            L.append(f"{f.numero:<12}{f.date:<12}{f.echeance:<12}"
                     f"{_fmt_money(f.ttc):>16}{_fmt_money(f.solde_du):>16}")
        L.append("-" * 68)
        L.append(f"{'TOTAL FACTURES':<30}{_fmt_money(rel.solde_du):>38}")
        L.append("")

    # Avoirs
    if rel.avoirs:
        L.append("AVOIRS (crédit en votre faveur) :")
        L.append(f"{'N° avoir':<12}{'Date':<12}{'Échéance':<12}"
                 f"{'Montant TTC':>16}{'Crédit':>16}")
        L.append("-" * 68)
        for f in rel.avoirs:
            L.append(f"{f.numero:<12}{f.date:<12}{f.echeance:<12}"
                     f"{_fmt_money(f.ttc):>16}{_fmt_money(abs(f.solde_du)):>16}")
        L.append("-" * 68)
        L.append(f"{'TOTAL AVOIRS':<30}{_fmt_money(rel.total_avoirs):>38}")
        L.append("")

    # Solde net
    L.append("=" * 68)
    if rel.solde_net >= 0:
        L.append(f"{'SOLDE NET À RÉGLER':<30}{_fmt_money(rel.solde_net):>38}")
    else:
        L.append(f"{'CRÉDIT EN VOTRE FAVEUR':<30}{_fmt_money(abs(rel.solde_net)):>38}")
    L.append("=" * 68)

    return "\n".join(L)


def _signature(cfg):
    return (f"{cfg.COMPANY_NAME}\n"
            f"{cfg.COMPANY_ADDRESS}\n"
            f"Tél. {cfg.COMPANY_PHONE} — {cfg.COMPANY_EMAIL}\n"
            f"SIRET {cfg.COMPANY_SIRET} — TVA {cfg.COMPANY_TVA}")


def _variables(rel, cfg, type_mail="relance"):
    """Dictionnaire des variables à injecter dans le template."""
    factures_a_montrer = (
        rel.factures if type_mail == "recap_hebdo"
        else (rel.factures_filtrees if rel.factures_filtrees else rel.factures)
    )
    montant = rel.solde_net if type_mail == "recap_hebdo" else rel.montant_filtre
    titre = "SOLDE NET" if type_mail == "recap_hebdo" else "TOTAL À RÉGLER"

    periode = PERIODES.get(rel.frequence, "")
    periode_suffix = f" ({periode})" if periode else ""

    # Tableau : pour le recap, on utilise le tableau complet avec net
    if type_mail == "recap_hebdo":
        factures_table = _table_recap_avec_net(rel, cfg)
    else:
        factures_table = _table_factures_txt(factures_a_montrer, montant, titre)

    return {
        "{{nom}}":             rel.nom,
        "{{code}}":            rel.code,
        "{{email}}":           rel.email or "",
        "{{telephone}}":       rel.telephone or "",
        "{{solde_du}}":        _fmt_money(rel.solde_du),
        "{{solde_net}}":       _fmt_money(rel.solde_net),
        "{{total_avoirs}}":    _fmt_money(rel.total_avoirs),
        "{{montant_filtre}}":  _fmt_money(rel.montant_filtre),
        "{{montant_total}}":   _fmt_money(montant),
        "{{nb_factures}}":     str(len(factures_a_montrer)),
        "{{nb_avoirs}}":       str(len(rel.avoirs)),
        "{{factures_table}}":  factures_table,
        "{{frequence_label}}": rel.frequence_label or "Toutes les factures",
        "{{periode_suffix}}":  periode_suffix,
        "{{niveau_label}}":    rel.niveau_label,
        "{{date}}":            datetime.now().strftime("%d/%m/%Y"),
        "{{company_name}}":    cfg.COMPANY_NAME,
        "{{company_address}}": cfg.COMPANY_ADDRESS,
        "{{company_phone}}":   cfg.COMPANY_PHONE,
        "{{company_email}}":   cfg.COMPANY_EMAIL,
        "{{company_siret}}":   cfg.COMPANY_SIRET,
        "{{company_tva}}":     cfg.COMPANY_TVA,
        "{{signature}}":       _signature(cfg),
    }


def _rendre(texte: str, variables: dict) -> str:
    if not texte:
        return ""
    for k, v in variables.items():
        texte = texte.replace(k, v)
    return texte


def rendre_message(rel, cfg, template: dict, type_mail="relance",
                   avec_lien=False, forcer_global=False, forcer_client=False):
    variables = _variables(rel, cfg, type_mail)
    sujet = _rendre(template.get("sujet", "Rappel de facture"), variables)
    corps = _rendre(template.get("corps", ""), variables)

    if avec_lien and doit_avoir_lien(rel, cfg, forcer_global, forcer_client):
        url = creer_lien_paiement(rel, cfg)
        if url:
            corps += ("\n\n💳 POUR RÉGLER EN LIGNE (carte bancaire ou SEPA) :\n"
                      f"{url}")

    return sujet, corps


def _template_pour(rel, templates: dict, type_mail: str, override_id: str = ""):
    if override_id and override_id in templates:
        return templates[override_id], override_id
    if rel.template_id and rel.template_id in templates:
        return templates[rel.template_id], rel.template_id
    if type_mail == "relance":
        for tid, t in templates.items():
            if t.get("type") == "relance" and t.get("niveau") == rel.niveau:
                return t, tid
    else:
        for tid, t in templates.items():
            if t.get("type") == "recap_hebdo":
                return t, tid
    fallback = {
        "sujet": "Rappel de facture — AVO GREEN",
        "corps": ("{{nom}},\n\nSauf erreur de notre part, il reste des factures "
                  "à régler :\n\n{{factures_table}}\n\nCordialement,\n\n{{signature}}"),
    }
    return fallback, ""


def _get_smtp(cfg):
    """Connexion SMTP avec gestion d'erreurs précise."""
    if not cfg.SMTP_HOST:
        raise RuntimeError("SMTP_HOST non configuré dans .env")
    if not cfg.SMTP_USER:
        raise RuntimeError("SMTP_USER non configuré dans .env")

    if cfg.SMTP_USE_TLS:
        s = smtplib.SMTP(cfg.SMTP_HOST, cfg.SMTP_PORT, timeout=30)
        s.ehlo()
        s.starttls()
        s.ehlo()
    else:
        s = smtplib.SMTP_SSL(cfg.SMTP_HOST, cfg.SMTP_PORT, timeout=30)

    try:
        s.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD)
    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(
            f"❌ Authentification SMTP échouée. Vérifie SMTP_USER et SMTP_PASSWORD. "
            f"Si tu es sur Gmail, il faut un « mot de passe d'application ». "
            f"Détail : {e}"
        )
    return s


def tester_smtp(cfg) -> tuple:
    """Teste la connexion SMTP. Retourne (ok, message)."""
    try:
        server = _get_smtp(cfg)
        server.quit()
        return True, f"✅ Connexion SMTP OK à {cfg.SMTP_HOST}:{cfg.SMTP_PORT} en tant que {cfg.SMTP_USER}"
    except RuntimeError as e:
        return False, str(e)
    except Exception as e:
        return False, f"❌ Erreur SMTP : {type(e).__name__} — {e}"


def envoyer_mail_test(cfg, destinataire: str) -> tuple:
    """Envoie un mail de test à un destinataire. Retourne (ok, message)."""
    if not destinataire or "@" not in destinataire:
        return False, "Adresse email invalide"

    # Test connexion d'abord
    ok, msg = tester_smtp(cfg)
    if not ok:
        return False, msg

    try:
        server = _get_smtp(cfg)
    except Exception as e:
        return False, f"Connexion impossible : {e}"

    msg_obj = EmailMessage()
    msg_obj["Subject"] = f"🧪 Test SMTP — {cfg.COMPANY_NAME}"
    msg_obj["From"] = formataddr((cfg.FROM_NAME, cfg.FROM_EMAIL))
    msg_obj["To"] = destinataire
    if cfg.REPLY_TO:
        msg_obj["Reply-To"] = cfg.REPLY_TO

    corps = f"""Bonjour,

Ceci est un mail de test envoyé depuis votre application de relances {cfg.COMPANY_NAME}.

Si vous recevez ce message, cela signifie que la configuration SMTP fonctionne correctement. ✅

Détails techniques :
- Serveur : {cfg.SMTP_HOST}:{cfg.SMTP_PORT}
- Expéditeur : {cfg.FROM_EMAIL}
- Destinataire : {destinataire}
- Date : {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}
- Mode : {'SIMULATION (DRY_RUN)' if cfg.DRY_RUN else 'ENVOI RÉEL'}

Cordialement,

{_signature(cfg)}
"""
    msg_obj.set_content(corps)

    try:
        server.send_message(msg_obj)
        server.quit()
        return True, f"✅ Mail de test envoyé à {destinataire}"
    except smtplib.SMTPRecipientsRefused as e:
        return False, f"❌ Destinataire refusé : {e}"
    except Exception as e:
        return False, f"❌ Erreur d'envoi : {type(e).__name__} — {e}"


def envoyer_relances(relances, cfg, factures_dir,
                     avec_lien_global=False, type_mail="relance",
                     templates_path=None, template_overrides=None,
                     forcer_envoi=False):
    """
    Envoie les relances ou récaps.
    `forcer_envoi=True` : envoie même si DRY_RUN (utile pour les tests).
    """
    if templates_path is None:
        templates_path = os.path.join(cfg.DATA_DIR, "templates_messages.json")
    templates = charger_templates(templates_path)
    template_overrides = template_overrides or {}

    resultats = []
    os.makedirs(os.path.dirname(cfg.SENT_LOG), exist_ok=True)
    log_f = open(cfg.SENT_LOG, "a", newline="", encoding="utf-8")
    writer = None
    server = None

    # Tentative de connexion SMTP seulement si on va vraiment envoyer
    if (not cfg.DRY_RUN or forcer_envoi) and cfg.SMTP_HOST:
        try:
            server = _get_smtp(cfg)
            log.info("✅ SMTP connecté — envoi réel")
        except Exception as e:
            log.error("❌ Connexion SMTP impossible : %s", e)
            log.error("⚠️  Tous les envois vont échouer. Vérifie ta config .env")

    for rel in relances:
        forcer_client = getattr(rel, "forcer_lien", False)
        override_id = template_overrides.get(rel.code, "")
        template, used_id = _template_pour(rel, templates, type_mail, override_id)

        sujet, corps = rendre_message(
            rel, cfg, template, type_mail=type_mail,
            avec_lien=avec_lien_global or forcer_client,
            forcer_global=avec_lien_global,
            forcer_client=forcer_client,
        )

        if not rel.email:
            resultats.append({"code": rel.code, "nom": rel.nom, "email": "",
                              "statut": "skipped", "message": "Pas d'email",
                              "objet": sujet, "montant": rel.solde_net,
                              "pieces": [], "type_mail": type_mail,
                              "template_id": used_id})
            continue

        msg = EmailMessage()
        msg["Subject"] = sujet
        msg["From"] = formataddr((cfg.FROM_NAME, cfg.FROM_EMAIL))
        msg["To"] = rel.email
        if cfg.REPLY_TO:
            msg["Reply-To"] = cfg.REPLY_TO
        msg.set_content(corps)

        # Pièces jointes
        dossier = os.path.join(factures_dir, rel.code)
        pieces = []
        if os.path.isdir(dossier):
            for fname in sorted(os.listdir(dossier)):
                if not fname.lower().endswith(".pdf"):
                    continue
                try:
                    with open(os.path.join(dossier, fname), "rb") as f:
                        msg.add_attachment(f.read(), maintype="application",
                                           subtype="pdf", filename=fname)
                    pieces.append(fname)
                except Exception as e:
                    log.warning("PJ ignorée %s : %s", fname, e)

        # Envoi
        statut, message = "sent", ""
        if (cfg.DRY_RUN and not forcer_envoi) or server is None:
            statut = "dry-run"
            message = f"Simulation ({len(pieces)} PJ)"
        else:
            try:
                server.send_message(msg)
                log.info("✅ Envoi %s <%s> [%s · %s] — %d PJ",
                         rel.nom, rel.email, type_mail, used_id, len(pieces))
            except Exception as e:
                statut, message = "error", f"{type(e).__name__}: {e}"
                log.error("❌ Échec envoi %s : %s", rel.email, e)

        montant_log = rel.solde_net if type_mail == "recap_hebdo" else rel.montant_filtre
        resultats.append({
            "code": rel.code, "nom": rel.nom, "email": rel.email,
            "statut": statut, "message": message, "objet": sujet,
            "montant": montant_log, "niveau": rel.niveau_label,
            "pieces": pieces, "frequence": rel.frequence,
            "type_mail": type_mail, "template_id": used_id,
        })

        if writer is None:
            writer = csv.writer(log_f)
            writer.writerow(["date", "code", "nom", "email", "niveau",
                             "montant", "frequence", "type_mail",
                             "template_id", "objet", "statut", "pieces"])
        writer.writerow([datetime.now().isoformat(timespec="seconds"),
                         rel.code, rel.nom, rel.email, rel.niveau,
                         f"{montant_log:.2f}", rel.frequence, type_mail,
                         used_id, sujet, statut, "|".join(pieces)])

    if server:
        try:
            server.quit()
        except Exception:
            pass
    log_f.close()
    return resultats