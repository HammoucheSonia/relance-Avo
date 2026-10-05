# AVO GREEN — Relances clients automatiques

Application Flask qui croise le **Relevé des factures non soldées** et la
**Balance âgée** pour préparer, prévisualiser et envoyer les relances clients.

## 🚀 Installation

```bash
cp .env.example .env
# Éditer .env avec tes identifiants SMTP (et Stripe si tu veux)
chmod +x run.sh
./run.sh# relance-Avo
