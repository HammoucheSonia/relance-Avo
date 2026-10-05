"""Extraction des 2 PDF : Relevé des factures + Balance âgée."""
import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional
import pdfplumber

DATE  = r"\d{2}/\d{2}/\d{4}"
MONEY = r"-?\d[\d\s\u00a0]*,\d{2}"


def to_float(s) -> float:
    if s is None or s == "":
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = (str(s).replace("\u00a0", "").replace(" ", "")
         .replace("€", "").replace(",", ".").strip())
    try:
        return float(s)
    except ValueError:
        return 0.0


# ─── Relevé des factures ────────────────────────────────────────────
@dataclass
class Facture:
    numero: str
    date: str
    echeance: str
    ht: float
    tva: float
    ttc: float
    reglement: str = ""
    solde_du: float = 0.0


@dataclass
class ReleveClient:
    code: str
    echeance: str = ""
    factures: List[Facture] = field(default_factory=list)
    total_ttc: float = 0.0
    total_solde_du: float = 0.0


INVOICE_RE = re.compile(
    r"(\d{6})\s*"
    rf"({DATE})\s*"
    rf"({DATE})\s*"
    rf"({MONEY})\s*€\s*"
    rf"({MONEY})\s*€\s*"
    rf"({MONEY})\s*€\s*"
    r"(.*)$"
)
MONEY_RE = re.compile(rf"({MONEY})\s*€")


def _parse_invoice_line(line: str) -> Optional[Facture]:
    m = INVOICE_RE.match(line.strip())
    if not m:
        return None
    numero, date, ech, ht, tva, ttc, tail = m.groups()
    amounts = MONEY_RE.findall(tail)
    reglement, solde_du = "", 0.0
    if len(amounts) >= 2:
        solde_du = to_float(amounts[-1])
        reglement = re.sub(r"\s+", " ", tail[:tail.rfind(amounts[-1])]).strip(" -")
    elif len(amounts) == 1:
        solde_du = to_float(amounts[0])
    return Facture(numero=numero, date=date, echeance=ech,
                   ht=to_float(ht), tva=to_float(tva), ttc=to_float(ttc),
                   reglement=reglement, solde_du=solde_du)


def parse_releve(path: str) -> Dict[str, ReleveClient]:
    result: Dict[str, ReleveClient] = {}
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            m = re.search(r"Client\s*N[°o]\s*:\s*(\d+)", text)
            if not m:
                continue
            code = m.group(1)
            em = re.search(rf"Échéance\s*:\s*({DATE})", text)
            echeance = em.group(1) if em else ""
            client = result.setdefault(code, ReleveClient(code=code, echeance=echeance))

            for line in text.split("\n"):
                stripped = line.strip()
                if stripped.startswith("Total"):
                    amts = MONEY_RE.findall(stripped)
                    if amts:
                        if len(amts) >= 2:
                            client.total_ttc += to_float(amts[-2])
                        client.total_solde_du += to_float(amts[-1])
                    continue
                inv = _parse_invoice_line(stripped)
                if inv:
                    client.factures.append(inv)
    return result


# ─── Balance âgée ────────────────────────────────────────────────────
@dataclass
class BalanceClient:
    code: str
    nom: str
    telephone: str = ""
    buckets: Dict[str, float] = field(default_factory=dict)
    total: float = 0.0
    autorise: float = 0.0
    delai: int = 0

    @property
    def solde_du(self) -> float:
        return -self.total if self.total < 0 else 0.0


BUCKET_KEYS = ["7", "15", "30", "45", "90", "365", "9999"]

BAL_LINE_RE = re.compile(
    r"^(\d{1,6})"
    r"(.+?)"
    r"\s*\(([^)]*)\)"
    r"\s*(-?\d[\d\s\u00a0]*,\d{2})\s*€"
    r"\s*-\s*(\d+)\s*j"
    r"(.*)$"
)


def _parse_balance_text(path: str) -> Dict[str, BalanceClient]:
    result: Dict[str, BalanceClient] = {}
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.split("\n"):
                line = line.strip()
                if not line or line.startswith(("Balance", "Edité", "Par :",
                                                "AVO", "Code", "16/")):
                    continue
                if line.lower().startswith("total"):
                    continue
                m = BAL_LINE_RE.match(line)
                if not m:
                    continue
                code, nom, tel, aut, delai, reste = m.groups()
                amounts = [to_float(a) for a in MONEY_RE.findall(reste)]
                if not amounts:
                    continue
                total = amounts[-1]
                buckets_list = amounts[:-1]
                padded = [0.0] * (len(BUCKET_KEYS) - len(buckets_list)) + buckets_list
                buckets = dict(zip(BUCKET_KEYS, padded))
                result[code] = BalanceClient(
                    code=code, nom=nom.strip(), telephone=tel.strip(),
                    buckets=buckets, total=total,
                    autorise=to_float(aut), delai=int(delai),
                )
    return result


def parse_balance_agee(path: str) -> Dict[str, BalanceClient]:
    result = _parse_balance_text(path)
    if result:
        return result
    # Fallback : extraction par tableaux
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    if not row or len(row) < 11:
                        continue
                    code = (row[0] or "").strip()
                    if not code.isdigit():
                        continue
                    nom_tel = (row[1] or "").strip()
                    tm = re.search(r"\(([^)]*)\)", nom_tel)
                    result[code] = BalanceClient(
                        code=code,
                        nom=re.sub(r"\([^)]*\)", "", nom_tel).strip(),
                        telephone=tm.group(1).strip() if tm else "",
                        buckets={k: to_float(row[3 + i])
                                 for i, k in enumerate(BUCKET_KEYS)},
                        total=to_float(row[10]),
                    )
    return result