"""Croisement, relances, planning, historique, sessions, templates."""
import csv
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from parsers import ReleveClient, BalanceClient, Facture


# ─── Fréquences ─────────────────────────────────────────────────────
FREQUENCES = {
    "":         {"label": "Toutes les factures",   "delai": 0},
    "hebdo":    {"label": "Chaque semaine",         "delai": 7},
    "quinzaine":{"label": "Toutes les 2 semaines",  "delai": 14},
    "mensuel":  {"label": "Une fois par mois",      "delai": 28},
}

FREQUENCE_COLORS = {
    "":          "#9ca3af",
    "hebdo":     "#2a7a4a",
    "quinzaine": "#c08a1f",
    "mensuel":   "#1e5c3a",
}

TYPES_MAIL = {
    "relance":     "Relance",
    "recap_hebdo": "Récap hebdomadaire",
}


# ─── Templates ─────────────────────────────────────────────────────
def charger_templates(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def sauver_templates(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def slugifier(s):
    import re, unicodedata
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
    s = re.sub(r'[^a-zA-Z0-9]+', '_', s).strip('_').lower()
    return s or "template"


def creer_template(path, nom, type_mail="relance", niveau="", sujet="", corps=""):
    templates = charger_templates(path)
    base = slugifier(nom)
    tid = base
    i = 2
    while tid in templates:
        tid = f"{base}_{i}"
        i += 1
    templates[tid] = {
        "nom": nom, "type": type_mail, "niveau": niveau,
        "sujet": sujet or f"{nom} — AVO GREEN",
        "corps": corps or "{{nom}},\n\n[Votre message ici]\n\n{{factures_table}}\n\nCordialement,\n\n{{signature}}",
    }
    sauver_templates(path, templates)
    return tid


def modifier_template(path, tid, **champs):
    templates = charger_templates(path)
    if tid not in templates:
        return False
    for k in ("nom", "type", "niveau", "sujet", "corps"):
        if k in champs and champs[k] is not None:
            templates[tid][k] = champs[k]
    sauver_templates(path, templates)
    return True


def supprimer_template(path, tid):
    templates = charger_templates(path)
    if tid not in templates:
        return False
    del templates[tid]
    sauver_templates(path, templates)
    return True


def dupliquer_template(path, tid):
    templates = charger_templates(path)
    if tid not in templates:
        return None
    src = templates[tid]
    return creer_template(path, f"{src.get('nom', tid)} (copie)",
                          src.get("type", "relance"), src.get("niveau", ""),
                          src.get("sujet", ""), src.get("corps", ""))


def parse_date_fr(s):
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


# ═══════════════════════════════════════════════════════════════════
# ⭐ NOUVELLE LOGIQUE DE RELANCE
# ═══════════════════════════════════════════════════════════════════
#
# Principe :
# 1. On identifie toutes les factures IMPAYÉES (solde > 0) du client
# 2. On prend la PLUS ANCIENNE → c'est elle qui déclenche
# 3. On regarde si le délai de la fréquence est écoulé depuis :
#    - soit sa date d'échéance (jamais relancé)
#    - soit la dernière relance (déjà relancé une fois)
# 4. Si oui → on relance en listant TOUTES les factures impayées
# 5. Si non → pas de relance pour ce client cette fois
#
# ═══════════════════════════════════════════════════════════════════

def _plus_ancienne_facture_impayee(factures):
    """Retourne la date de la plus ancienne facture non payée (solde > 0)."""
    dettes = [f for f in factures if f.solde_du > 0.005]
    if not dettes:
        return None
    dates = []
    for f in dettes:
        d = parse_date_fr(f.echeance) or parse_date_fr(f.date)
        if d:
            dates.append(d)
    return min(dates) if dates else None


def calculer_derniere_relance(relance, hist):
    """Retourne la date de la dernière relance ou None."""
    h = hist.get(relance.code, {})
    derniere = h.get("derniere_relance", "")
    if not derniere:
        return None
    try:
        return datetime.fromisoformat(derniere)
    except Exception:
        return None


def doit_relancer(relance, hist) -> bool:
    """
    Décide si ce client doit être relancé.
    
    Critères :
    - Il a au moins une facture impayée
    - ET le délai de sa fréquence est écoulé depuis :
      * la date de la plus ancienne facture impayée (si jamais relancé)
      * la date de la dernière relance (sinon)
    """
    if not relance.email:
        return False
    if not relance.frequence:
        # Pas de fréquence → on relance tous les clients qui doivent de l'argent
        return relance.solde_du > 0.005

    # Trouver la plus ancienne facture impayée
    plus_ancienne = _plus_ancienne_facture_impayee(relance.factures)
    if not plus_ancienne:
        return False

    delai = FREQUENCES[relance.frequence]["delai"]
    maintenant = datetime.now()

    # Date de référence : la plus tardive entre (plus ancienne facture) et (dernière relance)
    derniere = calculer_derniere_relance(relance, hist)
    if derniere and derniere > plus_ancienne:
        ref = derniere
    else:
        ref = plus_ancienne

    return (maintenant - ref).days >= delai


def calculer_planning(relance, hist):
    """Remplit derniere_relance, prochaine_relance, jours_restants."""
    relance.derniere_relance = hist.get(relance.code, {}).get("derniere_relance", "")

    if not relance.frequence:
        relance.prochaine_relance = ""
        relance.jours_restants = 0
        return relance

    plus_ancienne = _plus_ancienne_facture_impayee(relance.factures)
    if not plus_ancienne:
        relance.prochaine_relance = ""
        relance.jours_restants = 0
        return relance

    delai = FREQUENCES[relance.frequence]["delai"]
    derniere = calculer_derniere_relance(relance, hist)

    if derniere and derniere > plus_ancienne:
        ref = derniere
    else:
        ref = plus_ancienne

    prochaine = ref + timedelta(days=delai)
    relance.prochaine_relance = prochaine.isoformat(timespec="seconds")
    relance.jours_restants = (prochaine.date() - datetime.now().date()).days
    return relance


@dataclass
class Relance:
    code: str
    nom: str
    email: str = ""
    telephone: str = ""
    solde_du: float = 0.0
    total_avoirs: float = 0.0
    solde_net: float = 0.0
    montant_filtre: float = 0.0
    anciennete_max: int = 0
    niveau: str = "rappel_1"
    niveau_label: str = "Rappel amiable"
    template: str = "rappel_1"
    template_id: str = ""
    factures: List[Facture] = field(default_factory=list)
    factures_filtrees: List[Facture] = field(default_factory=list)
    avoirs: List[Facture] = field(default_factory=list)
    buckets: Dict[str, float] = field(default_factory=dict)
    remarque: str = ""
    frequence: str = ""
    frequence_label: str = ""
    est_nouveau: bool = False
    forcer_lien: bool = False
    lien_paiement: str = ""
    derniere_relance: str = ""
    prochaine_relance: str = ""
    jours_restants: int = 0
    doit_relancer: bool = False           # ⭐ nouvelle clé
    motif_relance: str = ""               # ⭐ pourquoi on relance

    @property
    def sans_email(self):
        return not self.email

    @property
    def etat(self):
        if self.est_nouveau and not self.derniere_relance:
            return "nouveau"
        if not self.frequence:
            return "libre"
        if self.doit_relancer:
            return "now"
        if self.jours_restants <= 3:
            return "bientot"
        return "ok"


def charger_contacts(path):
    if not os.path.exists(path):
        return {}
    contacts = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            code = (row.get("code") or "").strip()
            if code:
                contacts[code] = {k: (v or "").strip() for k, v in row.items()}
    return contacts


def sauver_contacts(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["code", "nom", "email", "telephone",
                                          "actif", "remarque", "frequence",
                                          "template_id"])
        w.writeheader()
        for r in rows:
            w.writerow({
                "code": r.get("code", ""), "nom": r.get("nom", ""),
                "email": r.get("email", ""), "telephone": r.get("telephone", ""),
                "actif": r.get("actif", "1"), "remarque": r.get("remarque", ""),
                "frequence": r.get("frequence", ""),
                "template_id": r.get("template_id", ""),
            })


def mettre_a_jour_frequence(path, code, frequence, nom="", email="",
                            telephone="", remarque="", template_id=None):
    contacts = charger_contacts(path)
    if code in contacts:
        contacts[code]["frequence"] = frequence
        if nom and not contacts[code].get("nom"):
            contacts[code]["nom"] = nom
        if email and not contacts[code].get("email"):
            contacts[code]["email"] = email
        if telephone and not contacts[code].get("telephone"):
            contacts[code]["telephone"] = telephone
        if template_id is not None:
            contacts[code]["template_id"] = template_id
    else:
        contacts[code] = {"code": code, "nom": nom, "email": email,
                          "telephone": telephone, "actif": "1",
                          "remarque": remarque, "frequence": frequence,
                          "template_id": template_id or ""}
    sauver_contacts(path, list(contacts.values()))


def detecter_nouveaux_contacts(codes_analyse, contacts):
    return sorted(set(codes_analyse) - set(contacts.keys()))


def charger_historique(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def sauver_historique(path, hist):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)


def marquer_envoye(path, code, niveau, montant, email,
                   statut="envoye", type_mail="relance", template_id=""):
    hist = charger_historique(path)
    hist.setdefault(code, {"envois": []})
    hist[code]["envois"].append({
        "date": datetime.now().isoformat(timespec="seconds"),
        "niveau": niveau, "montant": round(montant, 2),
        "email": email, "statut": statut,
        "type_mail": type_mail, "template_id": template_id,
    })
    hist[code]["statut"] = statut
    hist[code]["derniere_relance"] = datetime.now().isoformat(timespec="seconds")
    sauver_historique(path, hist)


def marquer_recap_hebdo(path, code, montant, email):
    hist = charger_historique(path)
    hist.setdefault(code, {"envois": []})
    hist[code].setdefault("recaps", [])
    hist[code]["recaps"].append({
        "date": datetime.now().isoformat(timespec="seconds"),
        "montant": round(montant, 2), "email": email,
    })
    hist[code]["dernier_recap"] = datetime.now().isoformat(timespec="seconds")
    sauver_historique(path, hist)


def changer_statut(path, code, statut):
    hist = charger_historique(path)
    hist.setdefault(code, {"envois": []})
    hist[code]["statut"] = statut
    hist[code]["maj"] = datetime.now().isoformat(timespec="seconds")
    sauver_historique(path, hist)


def charger_paiements(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def sauver_paiements(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def enregistrer_paiement(path, code, montant, session_id, email="",
                        statut_paiement="paye"):
    data = charger_paiements(path)
    if session_id in data:
        return False
    data[session_id] = {"code_client": code, "montant": round(montant, 2),
                        "email": email,
                        "date": datetime.now().isoformat(timespec="seconds")}
    sauver_paiements(path, data)
    hist_path = os.path.join(os.path.dirname(path), "history.json")
    hist = charger_historique(hist_path)
    hist.setdefault(code, {"envois": []})
    hist[code]["statut"] = statut_paiement
    hist[code]["paiement"] = {"montant": round(montant, 2),
                              "date": datetime.now().isoformat(timespec="seconds"),
                              "session_id": session_id}
    sauver_historique(hist_path, hist)
    return True


def _niveau_from_buckets(buckets, seuils):
    ordre = [("9999", 9999), ("365", 365), ("90", 90),
             ("45", 45), ("30", 30), ("15", 15), ("7", 7)]
    for key, days in ordre:
        if abs(buckets.get(key, 0)) > 0.5:
            if days <= seuils["r1"]:  return "rappel_1", "Rappel amiable", days
            if days <= seuils["r2"]:  return "rappel_2", "Rappel", days
            if days <= seuils["r3"]:  return "rappel_3", "Relance ferme", days
            if days <= seuils["med"]: return "med", "Mise en demeure amiable", days
            return "contentieux", "Dernier avis avant contentieux", days
    return "rappel_1", "Rappel amiable", 7


def seuils_from_config(cfg):
    return {"r1": cfg.SEUIL_RAPPEL_1, "r2": cfg.SEUIL_RAPPEL_2,
            "r3": cfg.SEUIL_RAPPEL_3, "med": cfg.SEUIL_MED,
            "contentieux": cfg.SEUIL_CONTENTIEUX}


def croiser(releves, balances, contacts, seuils, hist=None):
    """
    Croise les données et calcule pour chaque client :
    - solde_du (dettes réelles = factures positives)
    - total_avoirs (factures négatives)
    - solde_net (dettes - avoirs)
    - doit_relancer (basé sur fréquence + plus ancienne facture impayée)
    """
    relances = []
    hist = hist or {}

    for code, bal in balances.items():
        solde_brut = -bal.total
        dettes = solde_brut if solde_brut > 0 else 0.0
        avoirs_balance = -solde_brut if solde_brut < 0 else 0.0

        if dettes < 0.5 and avoirs_balance < 0.5:
            continue

        niveau, label, age = _niveau_from_buckets(bal.buckets, seuils)
        contact = contacts.get(code)
        est_nouveau = contact is None
        contact = contact or {}

        frequence = (contact.get("frequence") or "").strip().lower()
        if frequence not in FREQUENCES:
            frequence = ""

        template_id = contact.get("template_id", "") or ""

        factures = []
        if code in releves:
            factures = list(releves[code].factures)

        avoirs = [f for f in factures if f.solde_du < -0.005]
        total_avoirs = round(sum(abs(f.solde_du) for f in avoirs), 2)

        dettes_reelles = round(sum(f.solde_du for f in factures if f.solde_du > 0.005), 2)
        if dettes_reelles < 0.5 and dettes > 0.5:
            dettes_reelles = round(dettes, 2)
        solde_du = round(dettes_reelles, 2)
        solde_net = round(solde_du - total_avoirs, 2)

        rel = Relance(
            code=code,
            nom=contact.get("nom") or bal.nom or f"Client {code}",
            email=contact.get("email", ""),
            telephone=contact.get("telephone") or bal.telephone,
            solde_du=solde_du,
            total_avoirs=total_avoirs,
            solde_net=solde_net,
            montant_filtre=solde_du,     # montant à relancer = TOUTES les dettes
            anciennete_max=age,
            niveau=niveau, niveau_label=label, template=niveau,
            template_id=template_id,
            buckets=dict(bal.buckets),
            remarque=contact.get("remarque", ""),
            frequence=frequence,
            frequence_label=FREQUENCES[frequence]["label"],
            est_nouveau=est_nouveau,
            factures=factures,
            factures_filtrees=list(factures),   # ⭐ TOUTES les factures
            avoirs=avoirs,
        )

        # Calcul du planning et de la décision de relance
        calculer_planning(rel, hist)
        rel.doit_relancer = doit_relancer(rel, hist)

        if rel.doit_relancer:
            rel.motif_relance = (
                f"Délai {FREQUENCES[rel.frequence]['delai']}j écoulé "
                f"depuis la 1ère facture impayée"
            )
        elif not frequence:
            rel.motif_relance = "Pas de fréquence définie"
        else:
            rel.motif_relance = f"Dans {rel.jours_restants} jour(s)"

        relances.append(rel)

    relances.sort(key=lambda r: (-r.solde_net, r.nom))
    return relances


def relances_a_envoyer(relances, seulement_dues=True):
    """Clients à relancer : ceux qui doivent être relancés ET ont un email."""
    if seulement_dues:
        return [r for r in relances if r.doit_relancer and r.email]
    return [r for r in relances if r.email]


def clients_pour_recap(relances):
    """Clients pour le récap hebdo : tout le monde avec solde non nul."""
    return [r for r in relances if abs(r.solde_net) > 0.5 and r.email]


def stats_globales(relances, hist):
    a_relancer = sum(1 for r in relances if r.email and r.doit_relancer)
    sans_email = sum(1 for r in relances if not r.email)
    nouveaux = sum(1 for r in relances if r.est_nouveau)
    deja = sum(1 for h in hist.values() if h.get("statut") == "envoye")
    payes = sum(1 for h in hist.values() if h.get("statut") == "paye")
    total_du = sum(r.solde_du for r in relances if r.doit_relancer)
    total_du_global = sum(r.solde_du for r in relances)
    total_avoirs = sum(r.total_avoirs for r in relances)
    total_paye = sum(h["envois"][-1]["montant"] for h in hist.values()
                     if h.get("envois") and h.get("statut") == "paye")
    total_envoye = sum(h["envois"][-1]["montant"] for h in hist.values()
                       if h.get("envois") and h.get("statut") == "envoye")

    now_count = sum(1 for r in relances if r.doit_relancer)
    bientot_count = sum(1 for r in relances
                        if not r.doit_relancer and 0 < r.jours_restants <= 3)
    ok_count = sum(1 for r in relances
                   if not r.doit_relancer and r.jours_restants > 3)

    return {
        "a_relancer": a_relancer,
        "sans_email": sans_email,
        "nouveaux": nouveaux,
        "deja_relances": deja,
        "payes": payes,
        "total_clients": len(relances),
        "total_du": round(total_du, 2),
        "total_du_global": round(total_du_global, 2),
        "total_avoirs": round(total_avoirs, 2),
        "total_envoye": round(total_envoye, 2),
        "total_paye": round(total_paye, 2),
        "taux_recouvrement": round(100 * total_paye / total_du, 1) if total_du else 0,
        "now_count": now_count,
        "bientot_count": bientot_count,
        "ok_count": ok_count,
    }


def series_7j(hist):
    today = datetime.now().date()
    labels, values = [], []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        labels.append(d.strftime("%d/%m"))
        total = sum(e.get("montant", 0) for h in hist.values()
                    for e in h.get("envois", [])
                    if e.get("date", "")[:10] == d.isoformat()
                    and h.get("statut") == "paye")
        values.append(round(total, 2))
    return labels, values


# ─── Sessions ──────────────────────────────────────────────────────
ACTIVE_SESSION_FILE = "active_session.json"
SESSIONS_DIR = "sessions"


def chemin_session_actif(cfg):
    return os.path.join(cfg.DATA_DIR, ACTIVE_SESSION_FILE)


def chemin_sessions_archivees(cfg):
    p = os.path.join(cfg.DATA_DIR, SESSIONS_DIR)
    os.makedirs(p, exist_ok=True)
    return p


def charger_session_active(cfg):
    p = chemin_session_actif(cfg)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def sauver_session_active(cfg, data):
    p = chemin_session_actif(cfg)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fermer_session_active(cfg):
    p = chemin_session_actif(cfg)
    if os.path.exists(p):
        os.remove(p)


def finaliser_session(cfg, job_id, stats, note=""):
    archive_dir = chemin_sessions_archivees(cfg)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(archive_dir, f"session_{ts}_{job_id}.json")
    data = {"job_id": job_id,
            "finalise_le": datetime.now().isoformat(timespec="seconds"),
            "note": note, "stats": stats}
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    fermer_session_active(cfg)
    return dest


def lister_sessions_archivees(cfg):
    d = chemin_sessions_archivees(cfg)
    out = []
    for fname in sorted(os.listdir(d), reverse=True):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fname), encoding="utf-8") as f:
                data = json.load(f)
            data["_fichier"] = fname
            out.append(data)
        except Exception:
            pass
    return out