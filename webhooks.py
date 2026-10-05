"""Traitement des webhooks Stripe."""
import logging

log = logging.getLogger("relances")

try:
    import stripe
except ImportError:
    stripe = None


def verifier_signature(payload: bytes, signature: str, secret: str):
    if not stripe or not secret:
        return None
    try:
        return stripe.Webhook.construct_event(payload, signature, secret)
    except ValueError as e:
        log.error("Webhook payload invalide : %s", e)
        return None
    except stripe.error.SignatureVerificationError as e:
        log.error("Signature webhook invalide : %s", e)
        return None


def traiter_evenement(event, cfg):
    from core import enregistrer_paiement

    etype = event["type"]
    obj = event["data"]["object"]

    log.info("Webhook reçu : %s (id=%s)", etype, event.get("id"))

    if etype == "checkout.session.completed":
        code = (obj.get("metadata") or {}).get("code_client", "")
        montant_total = (obj.get("amount_total") or 0) / 100
        session_id = obj.get("id", "")
        email = (obj.get("customer_details", {}).get("email", "")
                 or obj.get("customer_email", ""))

        if not code:
            return False, "metadata.code_client manquant"

        nouveau = enregistrer_paiement(
            cfg.PAIEMENTS_FILE, code, montant_total, session_id, email, "paye"
        )
        if nouveau:
            log.info("Paiement enregistré : client %s · %.2f € · session %s",
                     code, montant_total, session_id)
            return True, f"Paiement client {code} enregistré ({montant_total:.2f} €)"
        return True, f"Paiement déjà enregistré (session {session_id})"

    if etype in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        code = (obj.get("metadata") or {}).get("code_client", "")
        if code:
            log.info("Paiement non abouti pour %s (type=%s)", code, etype)
        return True, f"Événement {etype} traité"

    if etype == "charge.refunded":
        log.info("Remboursement détecté : %s", obj.get("id", ""))
        return True, "Remboursement enregistré"

    return True, f"Événement ignoré : {etype}"