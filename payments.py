"""Création de liens de paiement Stripe."""
import logging
from datetime import datetime

log = logging.getLogger("relances")

try:
    import stripe
    STRIPE_DISPONIBLE = True
except ImportError:
    STRIPE_DISPONIBLE = False
    log.warning("Module 'stripe' absent — liens de paiement désactivés.")

_CACHE = {}


def stripe_active(cfg) -> bool:
    return cfg.STRIPE_ENABLED and bool(cfg.STRIPE_SECRET_KEY) and STRIPE_DISPONIBLE


def creer_lien_paiement(relance, cfg):
    if not stripe_active(cfg):
        return None

    cached = _CACHE.get(relance.code)
    if cached:
        age = (datetime.now() - cached["date"]).total_seconds()
        if age < 300:
            return cached["url"]

    stripe.api_key = cfg.STRIPE_SECRET_KEY

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card", "sepa_debit"],
            line_items=[{
                "price_data": {
                    "currency": "eur",
                    "product_data": {
                        "name": f"Règlement factures — {relance.nom}",
                        "description": f"Client {relance.code} · "
                                       f"{len(relance.factures)} facture(s)",
                    },
                    "unit_amount": int(round(relance.solde_du * 100)),
                },
                "quantity": 1,
            }],
            mode="payment",
            customer_email=relance.email or None,
            success_url=f"{cfg.BASE_URL}/merci?code={relance.code}",
            cancel_url=f"{cfg.BASE_URL}/annule?code={relance.code}",
            metadata={
                "code_client": relance.code,
                "nom": relance.nom,
                "niveau_relance": relance.niveau,
            },
            invoice_creation={"enabled": True},
        )
        _CACHE[relance.code] = {"url": session.url, "date": datetime.now()}
        log.info("Lien Stripe créé pour %s : %.2f €", relance.code, relance.solde_du)
        return session.url
    except Exception as e:
        log.error("Erreur Stripe pour %s : %s", relance.code, e)
        return None


def doit_avoir_lien(relance, cfg, forcer_global=False, forcer_client=False) -> bool:
    if not stripe_active(cfg):
        return False
    if forcer_client:
        return True
    if not forcer_global:
        return False
    petit = relance.solde_du <= cfg.STRIPE_SEUIL_PETIT
    difficile = "difficile" in (relance.remarque or "").lower()
    return petit or difficile